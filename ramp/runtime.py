"""运行时门面：红线 → 图 → 落库。

红线跑在图之前，所以命中时**一次模型调用都没有**，成本为零。
这是"图外前置拦截"在代码上的样子。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from langgraph.types import Command

from . import config, db, graph, guardrails, memory, prompts, trace, tracing
from .state import new_state, summarize


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:8]}"


def _load_context(session, employee_id: str) -> dict[str, Any]:
    emp = session.get(db.Employee, employee_id)
    if emp is None:
        raise ValueError(f"未知员工: {employee_id}")
    mem_lines = memory.recall(session, employee_id, kinds=("semantic",))
    block = prompts.session_layer(
        name=emp.name,
        team=emp.team,
        role=emp.role,
        day_index=emp.day_index(),
        mentor=emp.mentor_name,
        memory_lines=mem_lines,
    )
    return {
        "_session_block": block,
        "_employee_name": emp.name,
        "_mentor_id": emp.mentor_id,
        "_mentor_name": emp.mentor_name,
        "_day_index": emp.day_index(),
    }


def _load_history(session, session_id: str) -> list[dict[str, str]]:
    """把这个会话已有的对话读回来。

    ## 为什么这一步以前是空的

    `RampState.history` 这个字段一直在，`prompts.build(history=...)` 也支持，
    `compact.py` 整套压缩逻辑都写好了 —— **只是从来没有人往里灌数据**。
    每一轮 `new_state()` 都把 history 置成 []，于是每次提问都是全新开始。

    症状很隐蔽：单轮问答完全正常，只有多轮指代才会露馅。实测

        第 1 轮  社保什么时候开始交？    → 正常作答
        第 2 轮  那我的有没有交？        → 不知道"交"指什么，
                                          把 hr_query 打了三次（社保 / 公积金 /
                                          入职材料）+ org_lookup，
                                          最后回一句"你想确认的可能是以下之一"

    **四次工具调用本来一次就够。** 而且答案是模糊的，因为它在猜。

    ## 保留多少

    只取最近 KEEP_RECENT_TURNS 轮的原文。再往前的由 compact 压成摘要 ——
    对话越长带的历史越多，token 成本是线性涨上去的，而 30 天的会话
    不可能全塞进上下文。
    """
    keep = config.KEEP_RECENT_TURNS * 2  # 一轮 = 用户 + 助手两条
    rows = (session.query(db.Message)
            .filter_by(session_id=session_id)
            .order_by(db.Message.id.desc())
            .limit(keep)
            .all())
    return [{"role": m.role, "content": m.content}
            for m in reversed(rows) if m.role in ("user", "assistant")]


def _topic_of(question: str, domain: str) -> str:
    """给情景记忆打的主题标签。用规则而不是模型——
    这个标签只用于聚合统计，不值得为它多花一次调用。"""
    table = [
        ("权限申请", ("权限", "工单", "账号", "开通", "vpn", "数据库", "prod-db")),
        ("评审流程", ("评审", "review", "codeowner", "合并", "mr", "pr")),
        ("报销制度", ("报销", "发票", "差旅", "补贴")),
        ("考勤假期", ("打卡", "考勤", "请假", "年假", "病假", "事假")),
        ("薪酬社保", ("社保", "公积金", "工资", "发薪", "薪酬")),
        ("环境搭建", ("环境", "本地", "部署", "bootstrap", "依赖")),
        ("转正材料", ("转正", "试用期", "材料", "评审")),
    ]
    q = question.lower()
    for topic, kws in table:
        if any(k in q for k in kws):
            return topic
    return {"hr": "HR 其他", "it": "IT 其他", "biz": "业务其他"}.get(domain, "其他")


def ask(
    question: str,
    *,
    employee_id: str = "e_linxy",
    session_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """一次完整提问。返回 summarize(state) 的字典。

    如果结果里 pending_action 不为 None，说明图停在了确认点，
    需要调 resume() 才能继续。
    """
    sid = session_id or new_session_id()

    # ---- 图外前置拦截：命中红线，一次模型调用都没有 ----
    verdict = guardrails.check(question)
    if verdict.blocked:
        with trace.span("guardrail", sid, 0, rule_id=verdict.rule_id, blocked=True) as sp:
            sp.detail["rule"] = verdict.rule_name
        out = {
            "session_id": sid, "route": "blocked", "domain": None, "blocked": True,
            "rule_id": verdict.rule_id, "rule_name": verdict.rule_name,
            "answer": verdict.reply, "citations": "", "best_score": 0.0,
            "confident": False, "steps": 0, "tools": [], "escalation_id": None,
            "pending_action": None, "cost": 0.0, "tokens": (0, 0),
            "persist_memory": verdict.persist_memory,
        }
        if persist:
            trace.persist([sp.to_dict()], sid, 0)
            # H3-03 劳动纠纷：明确不写入任何记忆
            if verdict.persist_memory:
                _persist_turn(sid, employee_id, question, out, write_memory=False)
        return out

    session = db.get_session()
    try:
        ctx = _load_context(session, employee_id)
        history = _load_history(session, sid)
    finally:
        session.close()

    state = new_state(sid, employee_id, question)
    state.update(ctx)
    # **无条件赋值**，即使是空列表。
    # turn_scoped reducer 把"收到空列表"当作新一轮开始的信号去清掉旧值；
    # 写成 `if history:` 的话，第一轮（历史为空）就不会触发重置，
    # checkpointer 里上一个会话残留的历史会一直留着。
    state["history"] = history

    cfg = {"configurable": {"thread_id": sid}, **tracing.run_config(sid, employee_id)}
    result = graph.compiled().invoke(state, cfg)

    # 图停在 interrupt 时，invoke() 只回传部分状态——子图那一段的
    # tool_calls 与 spans 不在里面。直接从 checkpoint 快照取全量，
    # 否则中断路径的成本和工具调用会被漏报，成本模型跟着失真。
    pending = _pending_from(result, sid)
    if pending:
        result = _full_state(sid) or result
    out = summarize(result)
    out["pending_action"] = pending
    if pending:
        # 显式标注：这一轮的用量还没结算完，别拿它当完整成本用
        out["partial"] = True
        out["note"] = ("已停在确认点。本轮完整用量以 resume 的返回为准，"
                       "不要与这里的数字相加——resume 报的是全量。")
    if persist:
        trace.persist(result.get("spans", []), sid, result.get("step", 0))
        _persist_turn(sid, employee_id, question, out, write_memory=True)
    return out


def _full_state(sid: str) -> dict[str, Any] | None:
    """从 checkpoint 快照读 state。

    ⚠️ 中断时这里拿到的仍是**父图**的状态。LangGraph 的语义是：
    子图的写入要等子图节点跑完才合并回父图，而 interrupt 恰恰发生在
    子图内部——所以这一轮的 tool_calls / spans / cost 此刻还在子图的
    命名空间里，取不到。

    这不是 bug，是状态所有权的边界。产品上的处理是：中断这一轮标
    partial=True，完整用量以 resume 那一次为准。

    ⚠️ **两次的成本不要相加**：resume() 读的是合并后的全量状态，
    它报的数字已经包含了 ask() 那一段。相加会重复计费。
    唯一可信的口径是 trace 表按 session_id 求和。
    """
    try:
        snap = graph.compiled().get_state({"configurable": {"thread_id": sid}})
        vals = getattr(snap, "values", None)
        return dict(vals) if vals else None
    except Exception:  # noqa: BLE001
        return None


def _pending_from(result: dict[str, Any], sid: str) -> dict[str, Any] | None:
    """图停在 interrupt 时，把待确认内容取出来交给前端。"""
    pending = result.get("pending_action")
    if pending:
        return pending
    try:
        snap = graph.compiled().get_state({"configurable": {"thread_id": sid}})
        for task in getattr(snap, "tasks", ()) or ():
            for itr in getattr(task, "interrupts", ()) or ():
                return getattr(itr, "value", None)
    except Exception:  # noqa: BLE001
        pass
    return None


def resume(session_id: str, *, confirmed: bool) -> dict[str, Any]:
    """用户在确认框上点了确认或取消——从 checkpoint 继续跑。"""
    cfg = {"configurable": {"thread_id": session_id},
           **tracing.run_config(session_id, "", resumed=True)}
    result = graph.compiled().invoke(Command(resume={"confirmed": confirmed}), cfg)
    out = summarize(result)
    out["pending_action"] = None
    trace.persist(result.get("spans", []), session_id, result.get("step", 0))
    _sync_session_cost(session_id)
    return out


def _sync_session_cost(session_id: str) -> None:
    """把会话成本重算成 trace 之和。

    resume() 之前不更新它，导致每一次 HITL 的会话成本都少算——
    而 HITL 恰恰是这个产品最贵的一条路径。以 trace 为唯一真相来源重算，
    比在两处各自累加更不容易错。
    """
    from sqlalchemy import func as F

    ses = db.get_session()
    try:
        total = ses.query(F.coalesce(F.sum(db.Trace.cost), 0.0)).filter(
            db.Trace.session_id == session_id
        ).scalar() or 0.0
        row = ses.get(db.Session, session_id)
        if row is not None:
            row.total_cost = float(total)
            ses.commit()
    except Exception:  # noqa: BLE001
        ses.rollback()
    finally:
        ses.close()


def _persist_turn(
    sid: str, employee_id: str, question: str, out: dict[str, Any], *, write_memory: bool
) -> None:
    session = db.get_session()
    try:
        s = session.get(db.Session, sid)
        if s is None:
            s = db.Session(id=sid, employee_id=employee_id)
            session.add(s)
            # 必须先 flush：messages.session_id 有外键指向 sessions.id，
            # 而两张表之间没有声明 relationship，SQLAlchemy 不知道插入先后。
            session.flush()
        s.turns = (s.turns or 0) + 1
        s.total_cost = (s.total_cost or 0.0) + float(out.get("cost", 0.0))

        session.add(db.Message(session_id=sid, role="user", content=question,
                               route=out.get("route"), domain=out.get("domain")))
        session.add(db.Message(session_id=sid, role="assistant", content=out.get("answer", ""),
                               route=out.get("route"), domain=out.get("domain")))
        session.commit()

        if write_memory and out.get("domain"):
            topic = _topic_of(question, out["domain"])
            memory.remember_question(
                session, employee_id=employee_id, question=question,
                topic=topic, route=out.get("route", ""),
            )
            memory.record_outcome(
                session, employee_id=employee_id, route=out.get("route", ""),
                domain=out["domain"], ok=out.get("route") != "escalate",
            )
    except Exception as exc:  # noqa: BLE001
        # 落库失败不能拖垮回答，但**必须出声**——
        # 这个 except 曾经静默吞掉了一个外键顺序 bug，
        # 表现是"回答正常但会话一条没存"，查了很久。
        session.rollback()
        import logging

        logging.getLogger("ramp.runtime").warning("落库失败 %s: %s", type(exc).__name__, exc)
    finally:
        session.close()


def health() -> dict[str, Any]:
    from . import embeddings, knowledge, llm

    ok_db, msg_db = db.ping()
    ok_llm, msg_llm = llm.health()
    return {
        "db": (ok_db, msg_db),
        "llm": (ok_llm, msg_llm),
        "embedding": embeddings.backend_name(),
        "knowledge": knowledge.index().size if ok_db else 0,
        "tools": len(__import__("ramp.tools", fromlist=["registry"]).registry),
        "pricing_placeholder": config.PRICING_IS_PLACEHOLDER,
        "langsmith": tracing.status(),
    }
