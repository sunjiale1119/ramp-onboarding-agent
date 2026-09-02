"""初始化：建库、建表、灌种子数据。

embedding 只在这里和「知识沉淀」时调用一次，算完存进 MySQL 的 JSON 列。
索引载入时直接读列，不再打 API——这是检索侧成本接近零的原因。
"""

from __future__ import annotations

import json
from datetime import date

from . import config, db, embeddings, knowledge


def _mock() -> dict:
    return json.loads((config.SEED_DIR / "mock_systems.json").read_text(encoding="utf-8"))


def seed_employees(session) -> int:
    data = _mock()
    n = 0
    for e in data["employees"]:
        if session.get(db.Employee, e["id"]):
            continue
        session.add(
            db.Employee(
                id=e["id"],
                name=e["name"],
                team=e["team"],
                role=e["role"],
                domain=e.get("domain", "biz"),
                mentor_id=e.get("mentor_id"),
                mentor_name=e.get("mentor_name"),
                onboard_date=date.fromisoformat(e["onboard_date"]),
            )
        )
        n += 1
    session.commit()
    return n


def seed_semantic_memory(session) -> int:
    """把新人档案里稳定的事实写成语义记忆——
    这样 Agent 不必每轮都重新自报家门，也演示了「语义记忆」这一层。"""
    n = 0
    for emp in session.query(db.Employee).all():
        facts = [
            ("岗位", f"{emp.team} · {emp.role}"),
            ("Mentor", emp.mentor_name or "未指定"),
            ("入职日期", emp.onboard_date.isoformat()),
        ]
        for topic, content in facts:
            exists = (
                session.query(db.Memory)
                .filter_by(subject_id=emp.id, kind="semantic", topic=topic)
                .first()
            )
            if exists:
                continue
            session.add(
                db.Memory(
                    subject_id=emp.id,
                    kind="semantic",
                    topic=topic,
                    content=content,
                    # 可见性矩阵：语义画像只有本人可见
                    visible_to_self=True,
                    visible_to_mentor=False,
                    visible_to_hr=False,
                    expires_on=None,
                )
            )
            n += 1
    session.commit()
    return n


def run(*, reset: bool = False, verbose: bool = True) -> dict[str, int]:
    def say(*a):
        if verbose:
            print(*a)

    say("→ 建库建表 ...")
    db.create_database()
    if reset:
        say("→ 清空重建 ...")
        db.reset_database()

    session = db.get_session()
    stats: dict[str, int] = {}
    try:
        if session.query(db.Knowledge).count() == 0:
            say(f"→ 灌知识库（embedding 后端：{embeddings.backend_name()}）...")
            stats["knowledge"] = knowledge.seed_from_file(session)
        else:
            stats["knowledge"] = session.query(db.Knowledge).count()
            say(f"→ 知识库已有 {stats['knowledge']} 条，跳过")

        stats["employees"] = seed_employees(session)
        stats["memories"] = seed_semantic_memory(session)
        say(f"→ 新人 {stats['employees']} 位，语义记忆 {stats['memories']} 条")
    finally:
        session.close()

    knowledge.reload_index()
    say(f"✓ 完成。索引 {knowledge.index().size} 条。")
    return stats


if __name__ == "__main__":
    import sys

    run(reset="--reset" in sys.argv)
