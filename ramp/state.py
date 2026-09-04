"""图上流动的状态。

LangGraph 的 State 不是"随便放个 dict"——它决定了：
  · 哪些字段在并行分支合并时会冲突（用 reducer 解决）
  · checkpoint 恢复时能拿回什么（不在 State 里的东西，恢复后就没了）

所以 pending_action 必须在 State 里：用户点确认可能是 5 分钟后，
也可能是第二天，中间进程都重启过——它得能从 checkpoint 里被还原出来。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Route = Literal["blocked", "retrieve", "act", "escalate", "answer"]


def _last(a: Any, b: Any) -> Any:
    """后写覆盖先写。标量字段的默认 reducer。"""
    return b if b is not None else a


def turn_scoped(a: list | None, b: list | None) -> list:
    """本轮内累加，**跨轮重置**。

    ## 为什么需要它

    checkpointer 按 thread_id（= session_id）保留状态，所以同一个会话的
    第二轮 invoke() 拿到的是**第一轮结束时的 state**，新值是"加"上去的。
    对 `tool_calls` / `observations` / `history` 这类**每轮独立**的字段，
    operator.add 的后果是：

        第 2 轮  工具 ['hr_query']                    1 个
        第 3 轮  工具 ['hr_query'×3]                  3 个
        第 4 轮  工具 ['hr_query'×6]                  6 个   ← 而且这轮是
        第 5 轮  工具 ['hr_query'×12]                12 个     纯检索路径，
                                                              一个工具都没调

    数字在翻倍，而且**报出了根本没发生过的调用**。
    成本、耗时、工具成功率全部跟着失真。

    ## 怎么区分"新一轮"

    每轮开始时 runtime 会把这些字段显式置空（new_state 里就是 []）。
    约定：**收到空列表 = 新一轮开始，丢掉旧的**；收到非空 = 本轮内追加。

    这个约定成立的前提是节点永远不会"追加一个空列表"——
    实际代码里节点要么不返回这个字段（不触发 reducer），
    要么返回真实有内容的列表，所以安全。
    """
    if b is None:
        return list(a or [])
    if not b:                      # 显式传空 = 新一轮，旧的作废
        return []
    return [*(a or []), *b]


def merge_spans(a: list[dict[str, Any]] | None,
                b: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按 uid 去重的 span 累加器。

    LangGraph 的子图会**继承父图 state 再整体回传**。用 operator.add
    的话，父图在进子图之前记的那几条（classify / dispatch）会被再加一遍——
    表现是调用链里 classify 出现两次，成本凭空翻倍。

    这个 bug 很隐蔽：它不报错，只是让所有成本数字偏高。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*(a or []), *(b or [])]:
        uid = item.get("uid")
        key = uid or f"{item.get('span')}|{item.get('duration_ms')}|{item.get('cost')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


class RampState(TypedDict, total=False):
    # ---- 输入 ----
    session_id: str
    employee_id: str
    question: str

    # ---- 会话 ----
    history: Annotated[list[dict[str, str]], turn_scoped]
    """本轮带进上下文的对话。**每轮由 runtime 从数据库重新加载**，
    不靠 state 累积——否则同一会话跑两轮，历史会被拼两遍。"""
    summary: str
    """压缩后的历史摘要，替代被丢弃的中段。"""

    # ---- 分流 ----
    route: Route
    kind: str
    """fact / instance / procedure / action / advice —— 决定拒答阈值是否适用。"""
    domain: str
    classify_reason: str

    # ---- 红线 ----
    blocked: bool
    rule_id: str
    persist_memory: bool

    # ---- 检索 ----
    hits: list[dict[str, Any]]
    best_score: float
    confident: bool
    hints: list[str]

    # ---- Agent 循环 ----
    step: int
    tool_calls: Annotated[list[dict[str, Any]], turn_scoped]
    observations: Annotated[list[dict[str, Any]], turn_scoped]

    # ---- 人机协同 ----
    pending_action: dict[str, Any] | None
    """等待确认的写入操作。必须在 State 里，才能跨 checkpoint 恢复。"""
    action_result: dict[str, Any] | None
    confirmed: bool | None

    # ---- 升级 ----
    escalation_id: int | None

    # ---- 输出 ----
    answer: str
    citations: str

    # ---- 运行期注入（不来自用户输入，但必须声明，否则 LangGraph 会丢掉）----
    _session_block: str
    _employee_name: str
    _mentor_id: str | None
    _mentor_name: str | None
    _day_index: int

    # ---- 观测 ----
    spans: Annotated[list[dict[str, Any]], merge_spans]
    """用去重累加器而不是 operator.add——原因见 merge_spans 的注释。"""
    cost: Annotated[float, operator.add]
    tokens_in: Annotated[int, operator.add]
    tokens_out: Annotated[int, operator.add]
    """这三个仍按节点累加，但**最终数字以 spans 为准**（见 summarize）：
    同样受子图重复回传影响，不能直接信。"""
    tier_used: str


def new_state(session_id: str, employee_id: str, question: str) -> RampState:
    return RampState(
        session_id=session_id,
        employee_id=employee_id,
        question=question,
        history=[],
        route="retrieve",
        kind="fact",
        domain="hr",
        blocked=False,
        persist_memory=True,
        hits=[],
        best_score=0.0,
        confident=False,
        hints=[],
        step=0,
        tool_calls=[],
        observations=[],
        pending_action=None,
        action_result=None,
        confirmed=None,
        escalation_id=None,
        answer="",
        citations="",
        spans=[],
        cost=0.0,
        tokens_in=0,
        tokens_out=0,
        tier_used="",
    )


def summarize(state: RampState) -> dict[str, Any]:
    """把一次运行压成给前端 / trace 看的摘要。"""
    return {
        "session_id": state.get("session_id"),
        "route": state.get("route"),
        "kind": state.get("kind"),
        "domain": state.get("domain"),
        "blocked": state.get("blocked", False),
        "rule_id": state.get("rule_id"),
        "hits": state.get("hits", []),
        # act 路由不走检索，它的"证据"是工具返回值。复核界面要能看到
        # 回答到底基于什么——没有这个就没法判"有没有编造"。
        "observations": state.get("observations", []),
        "best_score": round(state.get("best_score", 0.0), 4),
        "confident": state.get("confident", False),
        "steps": state.get("step", 0),
        "tools": [t.get("name") for t in state.get("tool_calls", [])],
        "escalation_id": state.get("escalation_id"),
        "pending_action": state.get("pending_action"),
        # 从**去重后的 spans** 派生，而不是信 state 里累加的 cost——
        # 后者会把父图节点算两遍。一个真相来源。
        "cost": round(sum(s.get("cost", 0.0) for s in state.get("spans", [])), 6),
        "tokens": (
            sum(x.get("tokens_in", 0) for x in state.get("spans", [])),
            sum(x.get("tokens_out", 0) for x in state.get("spans", [])),
        ),
        "answer": state.get("answer", ""),
        "citations": state.get("citations", ""),
    }
