"""清空并重建：只留知识库和一个管理员。

## 这个脚本做什么

    删除   所有账号（管理员除外）、员工档案、三层记忆、会话、消息、
           升级卡片、调用留痕、推送记录
    保留   知识库（53 条，虚构的云启科技）
    重建   users 表结构（合并了入职字段之后列变了）
    创建   一个管理员账号

## 为什么要有这一步

原来系统里种了五个演示账号，配一整套虚构的社保记录、权限清单、
组织架构和审批人。演示效果好，代价是**除知识库外没有一条数据是真的** ——
看的人分不清哪些是产品能力，哪些是编好的剧本；
而且 Agent 会说出"审批人李敏"这种系统里查不到的人名。

现在只留管理员。团队成员自助注册、管理员在后台激活并填入职信息，
产生的每一条数据都是真的。

知识库仍然虚构，这是**刻意保留的例外**：只有自建才能精确控制
L1/L2/L3 分级与有效期，用来验证分级降权是否真的生效 ——
真实制度文件不会主动给你一条"已过期的 L3 传言"来做测试。

用法：
    uv run python scripts/reset_all.py            # 交互确认
    uv run python scripts/reset_all.py --yes      # 直接执行
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import inspect, text  # noqa: E402

from ramp import auth, db, knowledge  # noqa: E402


def main() -> None:
    print()
    print("=" * 66)
    print("  清空并重建")
    print("=" * 66)

    # 用原生 SQL 统计，**不能用 ORM** —— users 表的列已经变了，
    # ORM 会按新模型去 SELECT 旧表，直接 Unknown column 崩掉。
    # 迁移脚本读旧结构时必须绕开模型层，这是个通用陷阱。
    with db.engine().connect() as c:
        try:
            n_users = c.execute(text("SELECT COUNT(*) FROM users")).scalar()
        except Exception:
            n_users = 0

    ses = db.get_session()
    try:
        before = {
            "账号": n_users,
            "员工档案": ses.query(db.Employee).count(),
            "记忆": ses.query(db.Memory).count(),
            "会话": ses.query(db.Session).count(),
            "升级卡片": ses.query(db.Escalation).count(),
            "调用留痕": ses.query(db.Trace).count(),
            "知识条目": ses.query(db.Knowledge).count(),
        }
    finally:
        ses.close()

    print("\n  当前数据")
    for k, v in before.items():
        keep = "  ← 保留" if k == "知识条目" else ""
        print(f"    {k:<8} {v:>5}{keep}")

    if "--yes" not in sys.argv:
        print()
        ans = input("  确认清空？除知识库外全部删除，不可恢复 [yes/N] ").strip()
        if ans != "yes":
            print("  已取消。")
            return

    # ---- users 表结构变了（合并入职字段、删掉 employee_id），直接重建 ----
    print("\n  重建 users / auth_sessions 表结构 …")
    insp = inspect(db.engine())
    with db.engine().connect() as c:
        for t in ("auth_sessions", "users"):
            if insp.has_table(t):
                c.execute(text(f"DROP TABLE {t}"))
        c.commit()
    auth.User.__table__.create(db.engine(), checkfirst=True)
    auth.Session_.__table__.create(db.engine(), checkfirst=True)

    # ---- 清掉挂在人身上的业务数据 ----
    print("  清空业务数据 …")
    ses = db.get_session()
    try:
        wiped = {}
        for name, M in (("员工档案", db.Employee), ("记忆", db.Memory),
                        ("消息", db.Message), ("会话", db.Session),
                        ("升级卡片", db.Escalation), ("调用留痕", db.Trace),
                        ("推送记录", db.PushLog)):
            wiped[name] = ses.query(M).delete()
        ses.commit()
    finally:
        ses.close()
    for k, v in wiped.items():
        print(f"    删除 {k:<8} {v:>5}")

    # ---- 建管理员 ----
    n = auth.seed_users()
    print()
    if n:
        print(f"  已创建管理员 {auth.ADMIN_USERNAME} / {auth.ADMIN_PASSWORD}")
    else:
        print(f"  管理员 {auth.ADMIN_USERNAME} 已存在")

    knowledge.reload_index()
    print(f"  知识库 {knowledge.index().size} 条，索引已重建")

    print()
    print("=" * 66)
    print("  接下来")
    print("=" * 66)
    print("    1. 用管理员登录")
    print("    2. 让团队成员在登录页自助注册")
    print("    3. 在「成员」页点「设置」，填角色 / 团队 / 岗位 / 入职日期 / Mentor")
    print("    4. 点「激活」")
    print()
    print("    先建 Mentor 再建新人 —— 新人的 Mentor 下拉只列已激活的 Mentor 账号。")
    print()


if __name__ == "__main__":
    main()
