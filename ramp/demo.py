"""演示数据：一键装载 / 一键清空。

## 和上一版的根本区别

上一版的演示数据是 `seed/mock_systems.json` 里一堆 JSON 记录，里面的人
（审批人「李敏」、Mentor「陈昊」、HRBP「王倩」）**只存在于那个文件里**。
Agent 张口就来，管理员去成员列表一找 —— 没有这个人。

现在装载器做的第一件事是**创建真账号**：它们会出现在成员列表里、
能登录、能被改被删。Agent 说"你的 Mentor 是 X"，你去成员列表一定找得到 X。

## 清空的边界

只删装载器自己建的东西，靠 `_demo_manifest` 记录。
**你自己注册的账号一律不碰** —— 上次我写测试脚本时用了一个真实演示账号
做销毁测试，把它删了。清理动作必须精确到"我建的"，不能是"看起来像演示的"。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import auth, db, external

MANIFEST_KEY = "_demo_manifest"
DEMO_PASSWORD = "ramp2026"

# 权限项目录：key → 显示名
ENTITLEMENTS = {
    "vpn": "VPN 接入",
    "gitlab": "GitLab 代码库",
    "wiki": "内部 Wiki",
    "ga-dashboard": "数据看板（只读）",
    "prod-db:ro": "生产库只读",
    "log-platform": "日志平台",
}

# 岗位 → 应有权限。键匹配 Employee.role（即账号的 title）
ROLE_ENTITLEMENTS = {
    "后端工程师": ["vpn", "gitlab", "wiki", "log-platform"],
    "数据分析师": ["vpn", "wiki", "ga-dashboard", "prod-db:ro"],
    "产品经理": ["vpn", "wiki", "ga-dashboard"],
    "*": ["vpn", "wiki"],
}

# 入职材料清单
DOCS = {
    "id_card": "身份证复印件",
    "diploma": "学历证书",
    "bank_card": "工资卡信息",
    "photo": "一寸照片",
    "prev_cert": "离职证明",
}

# 账号蓝图。username → 属性
ACCOUNTS: list[dict[str, Any]] = [
    {"username": "demo_hr", "name": "演示-HRBP", "role": "hr",
     "team": "人力资源部", "title": "HRBP"},
    {"username": "demo_itdesk", "name": "演示-IT服务台", "role": "ops",
     "team": "信息技术部", "title": "IT 服务台"},
    {"username": "demo_dba", "name": "演示-数据平台负责人", "role": "ops",
     "team": "数据平台组", "title": "数据平台负责人"},
    {"username": "demo_mentor", "name": "演示-带教导师", "role": "mentor",
     "team": "数据组", "title": "高级后端工程师", "onboard_days_ago": 900},
    {"username": "demo_newbie", "name": "演示-新人", "role": "newbie",
     "team": "数据组", "title": "后端工程师",
     "onboard_days_ago": 19, "mentor": "demo_mentor"},
]

# 每个演示新人的业务状态
PROFILES = {
    "demo_newbie": {
        "social_status": "paid",
        "fund_status": "pending",
        "fund_base": 12000,
        "docs": ["id_card", "diploma", "bank_card"],   # 缺照片和离职证明
        "granted": ["vpn", "wiki"],                    # 缺 gitlab / 日志平台
        "leave_used": 0.0,
    },
}

APPROVERS = {
    "prod-db:ro": {"username": "demo_dba", "sla_days": 2},
    "gitlab": {"username": "demo_itdesk", "sla_days": 1},
    "log-platform": {"username": "demo_itdesk", "sla_days": 1},
    "ga-dashboard": {"username": "demo_dba", "sla_days": 2},
}

CONTACTS = {"hrbp": "demo_hr", "it_desk": "demo_itdesk"}


def is_loaded(session) -> bool:
    return bool(external.get_config(session, MANIFEST_KEY))


def manifest(session) -> dict[str, Any]:
    return external.get_config(session, MANIFEST_KEY) or {}


def load() -> dict[str, Any]:
    """装载演示数据。已装载则先清空再装，保证幂等。"""
    ses = db.get_session()
    try:
        if is_loaded(ses):
            ses.close()
            clear()
            ses = db.get_session()

        created: list[str] = []
        for spec in ACCOUNTS:
            un = spec["username"]
            auth.delete_user(un)                     # 同名残留先清掉
            ok, msg = auth.register(un, DEMO_PASSWORD, spec["name"])
            if not ok:
                raise RuntimeError(f"创建 {un} 失败：{msg}")
            kw: dict[str, Any] = {
                "role": spec["role"], "display_name": spec["name"],
                "team": spec["team"], "title": spec["title"], "active": True,
            }
            if "onboard_days_ago" in spec:
                kw["onboard_date"] = (
                    date.today() - timedelta(days=spec["onboard_days_ago"])
                ).isoformat()
            if "mentor" in spec:
                kw["mentor"] = spec["mentor"]
            ok, msg = auth.update_user(un, **kw)
            if not ok:
                raise RuntimeError(f"配置 {un} 失败：{msg}")
            created.append(un)

        for key, value in (
            ("entitlement_catalog", ENTITLEMENTS),
            ("role_entitlements", ROLE_ENTITLEMENTS),
            ("resource_approvers", APPROVERS),
            ("contacts", CONTACTS),
            ("doc_catalog", DOCS),
        ):
            external.set_config(ses, key, value)

        for eid, p in PROFILES.items():
            row = db.ExtProfile(employee_id=eid, **p)
            ses.merge(row)

        external.set_config(ses, MANIFEST_KEY, {
            "accounts": created,
            "config_keys": ["entitlement_catalog", "role_entitlements",
                            "resource_approvers", "contacts", "doc_catalog"],
            "profiles": list(PROFILES),
            "loaded_on": date.today().isoformat(),
        })
        ses.commit()
        return {"ok": True, "accounts": created,
                "password": DEMO_PASSWORD,
                "message": f"已创建 {len(created)} 个演示账号并配好四个外部系统"}
    finally:
        ses.close()


def clear() -> dict[str, Any]:
    """只删装载器建的东西。自己注册的账号一律不碰。"""
    ses = db.get_session()
    try:
        m = manifest(ses)
        if not m:
            return {"ok": True, "message": "本来就没有演示数据"}

        for eid in m.get("profiles", []):
            row = ses.get(db.ExtProfile, eid)
            if row is not None:
                ses.delete(row)
        for t in ses.query(db.Ticket).filter(
                db.Ticket.employee_id.in_(m.get("accounts") or ["\x00"])).all():
            ses.delete(t)
        for key in [*m.get("config_keys", []), MANIFEST_KEY]:
            row = ses.get(db.ExtConfig, key)
            if row is not None:
                ses.delete(row)
        ses.commit()

        for un in m.get("accounts", []):
            auth.delete_user(un)
        return {"ok": True, "removed": m.get("accounts", []),
                "message": f"已删除 {len(m.get('accounts', []))} 个演示账号与相关配置"}
    finally:
        ses.close()
