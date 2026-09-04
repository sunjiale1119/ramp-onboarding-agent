"""初始化：建库、建表、灌知识库、建管理员。

**不再造任何虚构的人。** 员工、社保记录、组织架构、审批人以前都在种子文件里，
现在清空了 —— 除知识库外，系统里的每一条数据都由使用者自己录入。

知识库仍然是虚构的（云启科技），这是刻意的：只有自建才能精确控制
L1/L2/L3 分级与有效期，用来验证分级降权是否真的生效。真实制度文件
不会主动给你一条"已过期的 L3 传言"来做测试。


embedding 只在这里和「知识沉淀」时调用一次，算完存进 MySQL 的 JSON 列。
索引载入时直接读列，不再打 API——这是检索侧成本接近零的原因。
"""

from __future__ import annotations

import json
from datetime import date

from . import config, db, embeddings, knowledge


def _mock() -> dict:
    return json.loads((config.SEED_DIR / "mock_systems.json").read_text(encoding="utf-8"))


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

    finally:
        session.close()

    # 只种一个管理员。其余账号由人自助注册、管理员激活。
    #
    # 以前这里种五个演示账号，配一整套虚构的社保记录、权限清单、组织架构。
    # 演示效果好，代价是**系统里没有一条数据是真的** ——
    # 看的人分不清哪些是产品能力，哪些是编的剧本。
    from . import auth

    stats["users"] = auth.seed_users()
    if stats["users"]:
        say(f"→ 已创建管理员 {auth.ADMIN_USERNAME}（密码 {auth.ADMIN_PASSWORD}）")
    else:
        say(f"→ 管理员 {auth.ADMIN_USERNAME} 已存在，跳过")

    knowledge.reload_index()
    say(f"✓ 完成。索引 {knowledge.index().size} 条。")
    return stats


if __name__ == "__main__":
    import sys

    run(reset="--reset" in sys.argv)
