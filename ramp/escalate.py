"""升级与知识飞轮。

    01 未命中   置信度低于阈值，不允许硬答
    02 升级     生成结构化卡片推给 Mentor
    03 回答     Mentor 一键回复，成本低于当面解释
    04 沉淀     审核后入库为 L2，标注来源与有效期
    05 复用     下一位新人直接命中

这条回路是产品的复利：升级率随批次下降的曲线就是它产生的，
也是"客户用得越久、迁移成本越高"的来源。把它删掉，Ramp 就
退化成一个普通问答机器人。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from . import db, knowledge


def open_escalation(
    session,
    *,
    session_id: str,
    employee_id: str,
    domain: str,
    question: str,
    best_score: float,
    hints: list[str],
    mentor_id: str | None = None,
) -> db.Escalation:
    row = db.Escalation(
        session_id=session_id,
        employee_id=employee_id,
        mentor_id=mentor_id,
        domain=domain,
        question=question.strip(),
        best_score=best_score,
        hints=hints or [],
        status="open",
    )
    session.add(row)
    session.commit()
    return row


def pending_for_mentor(session, mentor_id: str) -> list[dict[str, Any]]:
    """Mentor 看板上的待回答卡片。注意这里返回的是**问题本身**，
    不是新人的完整提问记录——卡片是被授权外泄的那一条，
    其余提问原文仍然不可见（见 memory.for_viewer）。"""
    rows = (
        session.query(db.Escalation)
        .filter(db.Escalation.mentor_id == mentor_id, db.Escalation.status == "open")
        .order_by(db.Escalation.id.desc())
        .all()
    )
    out = []
    for r in rows:
        emp = session.get(db.Employee, r.employee_id)
        out.append({
            "id": r.id,
            "question": r.question,
            "domain": r.domain,
            "best_score": round(r.best_score, 3),
            "hints": r.hints or [],
            "from": emp.name if emp else r.employee_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "context": (
                f"Ramp 检索到最高置信度 {r.best_score:.2f}，低于阈值，已转人工。"
            ),
        })
    return out


def answer_and_sink(
    session,
    escalation_id: int,
    *,
    answer: str,
    confirmed_by: str,
    sink: bool = True,
    valid_months: int = 12,
) -> dict[str, Any]:
    """Mentor 回答 → 审核沉淀为 L2 → 重建索引。

    sink=False 时只回复不沉淀（有些答案是一次性的，不该进知识库）——
    这个开关本身是产品设计：不是所有回答都值得成为组织资产。
    """
    esc = session.get(db.Escalation, escalation_id)
    if esc is None:
        return {"ok": False, "error": "升级卡片不存在"}
    if esc.status != "open":
        return {"ok": False, "error": f"该卡片已处理（{esc.status}）"}

    esc.answer = answer.strip()
    esc.answered_at = datetime.now()
    esc.status = "answered"
    session.commit()

    result: dict[str, Any] = {
        "ok": True,
        "escalation_id": esc.id,
        "answered": True,
        "sunk": False,
        "knowledge_id": None,
    }

    if not sink:
        return result

    row = knowledge.add_knowledge(
        session,
        domain=esc.domain,
        question=esc.question,
        answer=answer,
        source_level="L2",
        source_name="历史问答沉淀",
        confirmed_by=confirmed_by,
        effective_from=date.today(),
        expires_on=date.today() + timedelta(days=30 * valid_months),
    )
    esc.status = "sunk"
    esc.knowledge_id = row.id
    session.commit()

    knowledge.reload_index()  # 飞轮的最后一步：新知识立刻可被检索到

    result.update({
        "sunk": True,
        "knowledge_id": row.id,
        "citation": row.cite(),
        "kb_size": knowledge.index().size,
    })
    return result


def stats(session) -> dict[str, Any]:
    """升级率与飞轮效果——HR 看板上那条下降曲线的数据源。"""
    total = session.query(db.Escalation).count()
    sunk = session.query(db.Escalation).filter_by(status="sunk").count()
    open_ = session.query(db.Escalation).filter_by(status="open").count()
    kb = session.query(db.Knowledge).count()
    l2 = session.query(db.Knowledge).filter_by(source_level="L2").count()
    return {
        "escalations_total": total,
        "escalations_open": open_,
        "escalations_sunk": sunk,
        "knowledge_total": kb,
        "knowledge_l2_from_flywheel": l2,
        "sink_rate": round(sunk / total, 3) if total else 0.0,
    }
