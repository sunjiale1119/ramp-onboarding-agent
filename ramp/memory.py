"""三层记忆 + 可见性矩阵。

    情景 episodic    提问原文与卡点时刻     30 天，到期降解为主题聚合
    语义 semantic    岗位、团队、mentor      在职期间
    程序 procedural  路径成功率与工具失败率  长期，脱敏聚合

**可见性不是权限配置，是产品设计。** 提问原文对 mentor 和 HR 都不可见——
因为如果新人知道每句话都会被 leader 看到，他就不问了，而"不敢问"正是
这个产品要解决的原始问题。产品会死于信任崩塌，不是死于功能不足。

所以可见性写死在字段上（visible_to_*），由 for_viewer() 强制过滤，
而不是留给调用方自觉。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any, Literal

from . import config, db

Viewer = Literal["self", "mentor", "hr", "admin"]
Kind = Literal["episodic", "semantic", "procedural"]


# ------------------------------------------------------------------ 写入
def remember_question(
    session,
    *,
    employee_id: str,
    question: str,
    topic: str,
    route: str,
) -> db.Memory:
    """记一次提问。原文只有本人可见，主题会在聚合时暴露给 mentor。"""
    row = db.Memory(
        subject_id=employee_id,
        kind="episodic",
        topic=topic,
        content=question.strip(),
        visible_to_self=True,
        visible_to_mentor=False,  # ← 关键：原文不给 mentor
        visible_to_hr=False,
        weight=1.0,
        expires_on=date.today() + timedelta(days=config.EPISODIC_RETENTION_DAYS),
    )
    session.add(row)
    session.commit()
    return row


def remember_fact(session, *, employee_id: str, topic: str, content: str) -> db.Memory:
    """语义记忆：稳定事实。本人可见可编辑，他人不可见。"""
    existing = (
        session.query(db.Memory)
        .filter_by(subject_id=employee_id, kind="semantic", topic=topic)
        .first()
    )
    if existing:
        existing.content = content
        session.commit()
        return existing
    row = db.Memory(
        subject_id=employee_id, kind="semantic", topic=topic, content=content,
        visible_to_self=True, visible_to_mentor=False, visible_to_hr=False,
    )
    session.add(row)
    session.commit()
    return row


def record_outcome(session, *, employee_id: str, route: str, domain: str, ok: bool) -> None:
    """程序记忆：这条路径成不成。脱敏聚合，用来做运营优化。"""
    topic = f"{domain}/{route}"
    row = (
        session.query(db.Memory)
        .filter_by(subject_id=employee_id, kind="procedural", topic=topic)
        .first()
    )
    if row is None:
        row = db.Memory(
            subject_id=employee_id, kind="procedural", topic=topic,
            content="成功 0 / 共 0", weight=0.0,
            visible_to_self=False, visible_to_mentor=False, visible_to_hr=False,
        )
        session.add(row)
        session.flush()
    try:
        good, total = (int(x) for x in row.content.replace("成功", "").replace("共", "").split("/"))
    except Exception:  # noqa: BLE001
        good, total = 0, 0
    total += 1
    good += 1 if ok else 0
    row.content = f"成功 {good} / 共 {total}"
    row.weight = good / total if total else 0.0
    session.commit()


# ------------------------------------------------------------------ 读取
def recall(session, employee_id: str, *, kinds: tuple[str, ...] = ("semantic",), limit: int = 8) -> list[str]:
    """喂给 prompt 的记忆行。默认只取语义层——
    把 30 天的提问原文全塞进上下文既贵又没用，那是 compact 要解决的问题。"""
    rows = (
        session.query(db.Memory)
        .filter(db.Memory.subject_id == employee_id, db.Memory.kind.in_(kinds))
        .order_by(db.Memory.id.desc())
        .limit(limit)
        .all()
    )
    return [f"{r.topic}：{r.content}" for r in reversed(rows)]


def for_viewer(session, employee_id: str, viewer: Viewer) -> dict[str, Any]:
    """**可见性矩阵的唯一执行点。** 任何角色想看记忆都必须走这里。"""
    rows = session.query(db.Memory).filter_by(subject_id=employee_id).all()

    if viewer == "self":
        return {
            "viewer": "self",
            "raw_questions": [
                {"id": r.id, "topic": r.topic, "content": r.content, "kind": r.kind}
                for r in rows
                if r.visible_to_self and r.kind in ("episodic", "semantic")
            ],
            "note": "只有你能看到这一栏。提问原文不会展示给 mentor 或 HR。",
        }

    # mentor / hr / admin 一律只拿主题聚合，拿不到原文
    topics = Counter(r.topic for r in rows if r.kind == "episodic")
    payload: dict[str, Any] = {
        "viewer": viewer,
        "topic_counts": topics.most_common(),
        "raw_questions": None,
        "note": "提问原文对你不可见——这不是权限不足，是产品设计。你拿到的是主题聚合。",
    }
    if viewer == "mentor":
        payload["stuck_signal"] = stuck_signal(session, employee_id)
    elif viewer in ("hr", "admin"):
        # HR 只拿部门级汇总，连主题的绝对次数都收敛成占比
        total = sum(topics.values()) or 1
        payload["topic_counts"] = [(t, round(c / total, 3)) for t, c in topics.most_common()]
        payload["stuck_signal"] = None
        payload["note"] += " HR 视角进一步收敛为占比，不含个人绝对值。"
    return payload


def stuck_signal(session, employee_id: str, *, threshold: int = 3) -> dict[str, Any] | None:
    """掉队信号：同一主题反复追问。

    产品决策：**只推给 mentor，不推给新人本人。**
    直接对新人说"你落后了"产生的不是紧迫感，是被监视感——
    他会立刻减少使用，信号源随之枯竭。
    """
    rows = (
        session.query(db.Memory)
        .filter_by(subject_id=employee_id, kind="episodic")
        .all()
    )
    topics = Counter(r.topic for r in rows)
    if not topics:
        return None
    top, count = topics.most_common(1)[0]
    if count < threshold:
        return None
    return {
        "topic": top,
        "count": count,
        "advice": (
            f"「{top}」连续被追问 {count} 次——通常意味着流程本身有问题，"
            "而不是新人没学会。建议在 1:1 里主动问一次。"
        ),
        "audience": "mentor_only",
    }


# ------------------------------------------------------------------ 维护
def decay(session, *, today: date | None = None) -> int:
    """到期的情景记忆降解为主题聚合：删原文，留一条计数。"""
    today = today or date.today()
    expired = (
        session.query(db.Memory)
        .filter(db.Memory.kind == "episodic", db.Memory.expires_on < today)
        .all()
    )
    if not expired:
        return 0
    by_subject_topic: Counter[tuple[str, str]] = Counter()
    for r in expired:
        by_subject_topic[(r.subject_id, r.topic)] += 1
        session.delete(r)
    for (sid, topic), n in by_subject_topic.items():
        session.add(
            db.Memory(
                subject_id=sid, kind="procedural", topic=f"历史卡点/{topic}",
                content=f"过去 30 天共 {n} 次", weight=float(n),
                visible_to_self=True, visible_to_mentor=True, visible_to_hr=False,
            )
        )
    session.commit()
    return len(expired)


def forget(session, memory_id: int, employee_id: str) -> bool:
    """用户删除自己的一条记忆——记忆可读可删是信任设计的一部分。"""
    row = session.get(db.Memory, memory_id)
    if row is None or row.subject_id != employee_id:
        return False
    session.delete(row)
    session.commit()
    return True
