"""主图：意图分类 → 域分派 → 三选一进入子图。

红线检查**不在这张图里**——它在 runtime.py 里，跑在图之前。
这是刻意的：拦截发生在模型调用之前，所以命中红线的请求成本为零，
也不存在被 prompt 注入撬开的空间。图外的东西比图内的更难绕过。

checkpointer 挂在这里。它让 interrupt() 变得有意义：用户点确认
可能是 5 分钟后也可能是第二天，中间进程都重启过，状态得能捞回来。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import config, llm, prompts, subagents, trace
from .state import RampState

_FALLBACK = {"kind": "fact", "route": "retrieve", "domain": "hr", "reason": "分类失败，回落到检索"}

# ------------------------------------------------------------------ 确定性兜底
#
# 分类器是模型判断，会错。而"我的 X 是多少"这类问题一旦被误判成 fact，
# 后果很具体：检索到一条制度文档，置信度 0.85，于是自信地把**通用规则**
# 当成**这位用户的值**答了出去——比答不上来更糟。
#
# 所以加一道规则兜底：命中即强制走 act，不管模型怎么说。
# 和 guardrails 同一个思路——可审计的规则优先于不可审计的模型判断。
_INSTANCE_PAT = re.compile(
    r"我(的)?.{0,6}(社保|公积金|试用期|转正|合同|材料|余额|年假|考勤|绩效|工资条"
    r"|权限|账号|设备|工单|邮箱|leader|mentor|导师|上级|评审|入职)"
    r"|(我|本人).{0,4}(开通|申请|提交|交齐|办好)了"
    r"|(谁|哪位).{0,4}(审批|负责|批准|签字)"
    r"|(审批人|负责人|对接人|接口人)是谁"
    r"|(怎么|如何).{0,3}联系"
    r"|(联系方式|工号|分机)",
)


# 域关键词兜底。分类器把「报销怎么提交」判给 biz 过两次——
# 大概是被 biz 列表里的"周报"带偏了。这类高信号词不该交给模型猜。
_DOMAIN_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hr", re.compile(
        r"报销|发票|差旅|住宿|餐补|社保|公积金|薪酬|工资|年假|病假|事假|考勤|打卡"
        r"|试用期|转正|劳动合同|离职|绩效|体检|工牌|福利|学习基金")),
    ("it", re.compile(
        r"账号|密码|权限|VPN|邮箱|网络|WiFi|打印|设备|电脑|软件安装|白名单"
        r"|服务台|工单|SSO|数据库权限|生产库")),
    ("biz", re.compile(
        r"代码评审|CODEOWNERS|发布|上线|分支|提交信息|环境|staging|故障|值班"
        r"|周报|迭代|需求|联调|技术文档|站会")),
)


def hint_domain(question: str) -> str | None:
    """按命中数选域。没有明确信号就返回 None，交回给模型判断。"""
    q = question or ""
    scores = {d: len(p.findall(q)) for d, p in _DOMAIN_HINTS}
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else None


def force_instance(question: str) -> bool:
    return bool(_INSTANCE_PAT.search(question or ""))


# ------------------------------------------------------------------ 节点
def classify(state: RampState) -> dict[str, Any]:
    """意图分类 + 定域。用 tier2 快档，12ms 级别的开销换掉后面
    58% 请求进 Agent 循环的浪费——这是分层响应的收益来源。"""
    sid = state["session_id"]
    task = prompts.CLASSIFY.format(question=state["question"])

    # 分类必须看历史，否则指代句无从判断。
    # 「那我的有没有交？」单独看是一句没有主语的话 —— 模型只能瞎猜，
    # 猜错了后面整条链路都错。带上最近两轮就够：再多既没用也在烧 token。
    msgs: list[dict[str, str]] = []
    for m in (state.get("history") or [])[-4:]:
        msgs.append({"role": m["role"], "content": m["content"][:400]})
    msgs.append({"role": "user", "content": task})

    with trace.span("classify", sid, 0) as sp:
        sp.detail["history_turns"] = len(msgs) - 1
        data, res = llm.chat_json(
            msgs,
            tier="tier2",
            task="classify",   # 关闭思考：结构化 JSON 不需要推理，更快更便宜
            fallback=_FALLBACK,
            max_tokens=200,
        )
        trace.record_llm(sp, res)
        sp.detail.update(route=data.get("route"), domain=data.get("domain"))

    kind = data.get("kind") if data.get("kind") in (
        "fact", "instance", "procedure", "action", "advice") else "fact"
    route = data.get("route") if data.get("route") in ("retrieve", "act") else "retrieve"
    domain = data.get("domain") if data.get("domain") in config.DOMAINS else "hr"

    # 域兜底：高信号关键词优先于模型判断
    hinted = hint_domain(state["question"])
    domain_overridden = bool(hinted and hinted != domain)
    if hinted:
        domain = hinted

    # 规则兜底：实例类问题必须查系统，模型说什么都不算数
    overridden = False
    if force_instance(state["question"]) and kind not in ("instance", "action"):
        kind, route, overridden = "instance", "act", True
    if kind in ("instance", "action"):
        route = "act"
    sp.detail.update(kind=kind, route=route, domain=domain,
                     rule_override=overridden, domain_override=domain_overridden)

    return {
        "route": route,
        "kind": kind,
        "domain": domain,
        "classify_reason": str(data.get("reason", ""))[:60],
        "spans": [sp.to_dict()],
        "cost": res.cost,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
    }


def dispatch(state: RampState) -> dict[str, Any]:
    """域分派 Supervisor。

    它自己不做业务，只做一件事：**选定一个域，让工具集与知识范围随之收窄。**
    收窄这件事发生在下游——knowledge.search(domain=) 与 registry.for_domain()。
    """
    sid = state["session_id"]
    with trace.span("dispatch", sid, 0, domain=state.get("domain")) as sp:
        sp.detail["scope"] = {
            "knowledge": f"domain={state.get('domain')}",
            "tools": _tool_names(state.get("domain")),
        }
    return {"spans": [sp.to_dict()]}


def _tool_names(domain: str | None) -> list[str]:
    from .tools import registry

    return registry.names(domain)


def _pick_domain(state: RampState) -> str:
    d = state.get("domain")
    return d if d in config.DOMAINS else "hr"


# ------------------------------------------------------------------ 组装
def build(checkpointer: Any = None):
    g = StateGraph(RampState)
    g.add_node("classify", classify)
    g.add_node("dispatch", dispatch)
    for d in config.DOMAINS:
        g.add_node(f"domain_{d}", subagents.subgraph(d))

    g.add_edge(START, "classify")
    g.add_edge("classify", "dispatch")
    g.add_conditional_edges(
        "dispatch",
        _pick_domain,
        {d: f"domain_{d}" for d in config.DOMAINS},
    )
    for d in config.DOMAINS:
        g.add_edge(f"domain_{d}", END)

    return g.compile(checkpointer=checkpointer)


_compiled: Any = None
_saver_cm: Any = None


def compiled():
    """全局单例。checkpointer 默认用自研 MySQLSaver（见 ramp/checkpointer.py），
    设 RAMP_CHECKPOINT_BACKEND=sqlite 可回退到官方实现。"""
    global _compiled, _saver_cm
    if _compiled is None:
        if config.CHECKPOINT_BACKEND == "mysql":
            from .checkpointer import MySQLSaver

            saver = MySQLSaver()
        else:
            from langgraph.checkpoint.sqlite import SqliteSaver

            _saver_cm = SqliteSaver.from_conn_string(config.CHECKPOINT_DB)
            saver = _saver_cm.__enter__()
        _compiled = build(checkpointer=saver)
    return _compiled


def ascii_graph() -> str:
    """给 README 用的 ASCII 图。"""
    try:
        return build().get_graph(xray=1).draw_ascii()
    except Exception as exc:  # noqa: BLE001
        return f"(绘图失败: {exc})"
