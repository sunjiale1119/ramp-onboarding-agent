"""LangSmith 接入。

## 为什么这个模块不是三行环境变量

LangSmith 的标准接法确实只要三个环境变量——打开 `LANGCHAIN_TRACING_V2`，
LangGraph 的每个节点、每次状态流转、每层子图都会自动上报。接上就能用。

**但这个产品接不了标准接法。**

Ramp 的核心承诺是一句话：*新人的提问原文，除了他自己没有人能看到。*
Mentor 看不到、HR 看不到、连管理员都看不到——可见性矩阵是产品设计，
不是权限配置，为此还专门在 `auth.ROLE_VIEWS` 里把 admin 排除在 newbie 之外。

标准接法会把**每一条提问原文和每一句回答**上传到 LangChain 的云端。
那里的访问控制归 LangSmith 账号管，跟我们的可见性矩阵没有任何关系。
一个买了工位的运营同学拿到 LangSmith 只读权限，就能看到全公司新人
私下问过什么——而产品界面上，这件事我们说"做不到"。

> **在接口上拦住 admin，却把同一份数据整包发给第三方 SaaS，
> 那道边界就只是个界面效果。**

所以这里的接法是：**结构全传，原文默认脱敏。**

    传：路由走向、子图层级、每步耗时、token 数、检索命中的知识 id 与分数、
        用了哪个工具、有没有升级、命中哪条红线
    不传：question / answer 原文、history 消息体、记忆内容、员工姓名

调 Agent 需要看的东西——"为什么走了 escalate 而不是 answer"、
"哪一步慢"、"哪个子图重复调了两次"——**全在结构里，不在原文里**。
脱敏之后 LangSmith 该有的排查能力一点没少。

真要看原文的时候（比如本地复现一个 bad case），把
`RAMP_LANGSMITH_RAW=1` 打开，它会在 /api/health 和管理后台上
显著地标出来——**开着不要紧，不知道自己开着才要紧**。

## 和自建 trace 的关系

不是二选一，两边看的东西不同：

    自建 traces 表   成本模型（含缓存命中折价）、给运营看的瀑布图、按会话归档
    LangSmith       LangGraph 原生视角：节点树、子图嵌套、每步的状态 diff

成本那部分 LangSmith 算不出来——我们的 LLM 客户端不是 LangChain 的
chat model，它拿不到 DeepSeek 的 `prompt_cache_hit_tokens`，
而缓存命中便宜 30 倍，不算这个的账单是错的。所以自建那套留着。

## 用法

    .env 里加：
        LANGSMITH_API_KEY=lsv2_...
        RAMP_LANGSMITH_PROJECT=ramp        # 可选，默认 ramp
        RAMP_LANGSMITH_RAW=0               # 可选，1 = 上传原文（慎用）

    没配 key 就整个关掉，不报错也不拖慢——`status()` 会说明原因。
"""

from __future__ import annotations

import os
from typing import Any

from . import config

# ------------------------------------------------------------------ 开关
API_KEY = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
PROJECT = os.getenv("RAMP_LANGSMITH_PROJECT", "ramp")
ENDPOINT = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

# 上传原文。默认关——见模块开头。
SEND_RAW = os.getenv("RAMP_LANGSMITH_RAW", "0").lower() in ("1", "true", "yes")

ENABLED = bool(API_KEY)

# ------------------------------------------------------------------ 脱敏
#
# **默认拒绝，白名单放行。**
#
# 第一版反着写：列一张"要隐藏的字段"黑名单（question / answer / content …）。
# 单测全绿，端到端一跑就漏了——DeepSeek 思考模式的 `reasoning_content`
# 不在名单里，而模型恰恰在思维链里把用户的问题和查到的社保数据
# 复述了一遍，整段原样上了云。
#
# 漏的不是那一个字段，是**方向反了**：黑名单要求我提前知道所有会带
# 隐私的字段名，而字段名归模型供应商定，他们随时能加。
# 追不上的清单等于没有清单。
#
# 所以反过来：**字符串一律脱敏，除非 key 在这张白名单上。**
# 数字、布尔、None 一律放行——它们本身就是结构。
#
# 失败模式也跟着反过来了：新字段出现时，最坏是多脱敏了一个本来无害的
# 东西（看到 `[已脱敏 · 12 字]`，加进白名单就行），
# 而不是把用户的原话发出去。**多脱敏能补救，泄漏不能。**
_SAFE_STR_KEYS = {
    # 路由与判断
    "route", "kind", "domain", "rule_id", "rule_name", "tier", "tier_used",
    "status", "state", "verdict", "level", "source_level",
    # 标识
    "id", "uid", "session_id", "employee_id", "mentor_id", "thread_id",
    "checkpoint_ns", "run_id", "trace_id",
    # 模型与调用
    "model", "role", "finish_reason", "object", "type", "run_type",
    "span", "name", "tool", "node", "event",
}


def _safe_str(key: str, value: str) -> bool:
    """这个字符串能原样上传吗。

    key 在白名单上，且**长度像个标签不像句话**——
    `name` 是安全 key，但如果哪天有人往 name 里塞一整段话，
    长度这一层还能兜住。
    """
    return key in _SAFE_STR_KEYS and len(value) <= 120

_client = None
_wrapped: dict[int, Any] = {}


def _mask(value: Any, key: str = "") -> Any:
    """递归脱敏。**保留形状，只挖掉内容**。

    直接把整个 payload 换成 "[redacted]" 也能保护隐私，但那样 LangSmith
    上就只剩一串空节点，等于白接。所以这里保留所有 key、保留嵌套结构、
    保留全部数值字段，只把自由文本换成长度标记——
    `[已脱敏 · 37 字]` 至少还能告诉你"这一步确实拿到了输入"。
    """
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, key) for v in value]
    if isinstance(value, str):
        if not value or _safe_str(key, value):
            return value
        return f"[已脱敏 · {len(value)} 字]"
    # 数字 / 布尔 / None —— 本身就是结构，放行
    return value


def _hide(payload: Any) -> Any:
    return payload if SEND_RAW else _mask(payload)


def client():
    """带脱敏钩子的 LangSmith 客户端。没配 key 就返回 None。"""
    global _client
    if not ENABLED:
        return None
    if _client is None:
        from langsmith import Client

        _client = Client(
            api_key=API_KEY,
            api_url=ENDPOINT,
            # hide_inputs / hide_outputs 是**上传前**调用的，
            # 所以原文根本不会离开这台机器，不是"上传了再隐藏"。
            hide_inputs=_hide,
            hide_outputs=_hide,
        )
    return _client


def wrap(openai_client):
    """包一层，让每次 DeepSeek 调用都变成 LangSmith 上的一个 LLM span。

    没开就原样返回——**调用方不需要知道 tracing 开没开**。
    """
    if not ENABLED:
        return openai_client
    key = id(openai_client)
    if key not in _wrapped:
        from langsmith.wrappers import wrap_openai

        _wrapped[key] = wrap_openai(openai_client, tracing_extra={"client": client()})
    return _wrapped[key]


def callbacks() -> list:
    """给 graph.invoke(config=...) 用的回调。

    显式传 tracer 而不是靠 `LANGCHAIN_TRACING_V2` 环境变量——
    环境变量那条路会走 langsmith 自己的默认 client，
    **绕过我们的脱敏钩子**。隐私保护不能依赖"别人正好没设那个变量"。
    """
    if not ENABLED:
        return []
    try:
        from langchain_core.tracers import LangChainTracer

        return [LangChainTracer(project_name=PROJECT, client=client())]
    except Exception:  # noqa: BLE001
        return []


def run_config(session_id: str, employee_id: str, **extra: Any) -> dict[str, Any]:
    """一次调用的 LangSmith 元数据。

    metadata 里**不放姓名**，只放 id——LangSmith 上能按 employee_id
    串起同一个人的所有会话来排查，但看不出那是谁。
    """
    if not ENABLED:
        return {}
    return {
        "callbacks": callbacks(),
        "run_name": f"ramp-turn-{session_id[:8]}",
        "metadata": {
            "session_id": session_id,
            "employee_id": employee_id,
            "tier1": config.TIER1,
            "tier2": config.TIER2,
            "redacted": not SEND_RAW,
            **extra,
        },
        "tags": ["ramp", "peak" if config.is_peak() else "offpeak"],
    }


def status() -> dict[str, Any]:
    """给 /api/health 和管理后台。

    **"以为开着其实没开"和"以为脱敏了其实在传原文"，
    这两件事都必须能一眼看出来。**
    """
    return {
        "enabled": ENABLED,
        "project": PROJECT if ENABLED else None,
        "endpoint": ENDPOINT if ENABLED else None,
        "send_raw": SEND_RAW,
        "reason": (
            "未配置 LANGSMITH_API_KEY —— 追踪关闭" if not ENABLED
            else "⚠ 正在上传提问原文到 LangSmith 云端（RAMP_LANGSMITH_RAW=1）"
            if SEND_RAW
            else "已启用 · 结构上传、提问原文本地脱敏"
        ),
    }
