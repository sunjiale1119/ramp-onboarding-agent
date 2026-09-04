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


def refresh_demo_dates(target_days: tuple[int, ...] = (20, 14)) -> int:
    """把演示员工的入职日期滚回 30 天窗口内。**只在 DEMO_MODE 下调用。**

    种子里的入职日期是写死的（2026-08-14 / 08-21），跑一段时间之后
    新人就"毕业"了：实测再过 20 天林小雨到第 40 天，
    时间线全部变成已完成、主动推送不再触发——
    **一个演示站在面试当天打不出效果，等于没部署。**

    做法是按顺序把每位员工放到指定的天数上（默认第 20 天、第 14 天），
    一个正处在中段、一个刚过半，两条时间线都还有未完成节点。

    改的是入职日期这种**业务事实**，所以必须显式、必须只在演示模式下发生。
    真实系统里一个会自己改员工入职日期的程序是灾难。
    """
    from datetime import timedelta

    session = db.get_session()
    try:
        emps = session.query(db.Employee).order_by(db.Employee.id).all()
        today = date.today()
        n = 0
        for emp, want in zip(emps, target_days):
            new_date = today - timedelta(days=want - 1)
            if emp.onboard_date != new_date:
                emp.onboard_date = new_date
                n += 1

            # 语义记忆里也存了一份「入职日期」。这里**无条件对账**，
            # 不能写成"日期变了才同步"——我第一版就是那么写的，
            # 结果日期恰好没变的那位员工，记忆里还留着上一轮的旧值，
            # 页头算出"第 20 天"而右栏记忆写着另一个日期：
            # **同一个事实在界面上出现了两个值。**
            #
            # 教训：同一份事实存了两处时，对账要看"是否一致"，
            # 不能看"这一次有没有改过"。
            row = (session.query(db.Memory)
                   .filter_by(subject_id=emp.id, kind="semantic", topic="入职日期")
                   .first())
            if row is not None and row.content != new_date.isoformat():
                row.content = new_date.isoformat()
                n += 1
        if n:
            session.commit()
        return n
    finally:
        session.close()


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

    # 演示账号。**以前这一步是我在本机手动跑的**，没进 bootstrap ——
    # 于是任何人 clone 下来照 README 跑一遍，服务起得来、页面打得开、
    # 就是一个都登不进去。部署到服务器时就这么栽了一次。
    #
    # 教训：**"我本地手动做过一次"的步骤，等于没做。**
    # 它不在代码里，就不会在别人的机器上发生。
    from . import auth

    stats["users"] = auth.seed_users()
    say(f"→ 演示账号 {stats['users']} 个（密码 {auth.DEMO_PASSWORD}）")

    knowledge.reload_index()
    say(f"✓ 完成。索引 {knowledge.index().size} 条。")
    return stats


if __name__ == "__main__":
    import sys

    run(reset="--reset" in sys.argv)
