"""外部系统内置实现的专项自检。

## 为什么单独一个脚本

黄金集评测钉死跑 `off` 模式 —— 它守的是「没接入时诚实说查不到」。
内置实现那条路径因此不在它的覆盖范围里，需要这个脚本补上。

分开还有一个好处：这里全部是**确定性断言**，一次模型调用都不需要，
跑完不到一秒、零成本，可以在每次改动后随手跑。

## 这里重点验什么

不是"功能能不能用"，是**「人」会不会又变成幽灵**：

    · 配置里指派的账号被删掉之后，Agent 还报不报得出这个人
    · 没录过的字段会不会被填成一个看似合理的默认值
    · 算出来的字段和录入的字段有没有混在一起

前两条正是上一版出「李敏」和「未参保」两类错误的地方。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ramp.config as C  # noqa: E402
from ramp import auth, db, demo, external  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def expect_blocked(name: str, fn) -> None:
    """这个调用必须拒答，不能返回一个编出来的值。"""
    try:
        got = fn()
        check(name, False, f"竟然返回了 {got}")
    except external.NotConnected:
        check(name, True)


def main() -> int:
    U = "extcheck_subject"
    C.EXTERNAL_MODE = "builtin"
    print("外部系统内置实现自检\n" + "-" * 60)

    # ---- 1. 纯算法，不碰数据库 ----
    print("\n[1] 能算的部分（不该进数据库）")
    check("试用期 3 个月", external.probation_of(date(2026, 8, 17))["probation_end"] == "2026-11-17")
    check("月末进位 1/31+1月=2/28", external._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28))
    check("闰年 1/31+1月=2/29", external._add_months(date(2028, 1, 31), 1) == date(2028, 2, 29))
    check("社保 17 号入职当月起缴",
          external.social_start_month(date(2026, 8, 17))["month"] == "2026-08")
    check("社保 25 号入职次月起缴",
          external.social_start_month(date(2026, 8, 25))["month"] == "2026-09")
    check("年假折算不足 1 天不计",
          external.annual_leave_of(date(2026, 8, 17))["total"] == 1.0)
    check("满一年按全年基数",
          external.annual_leave_of(date(2020, 3, 1))["total"] == float(external.ANNUAL_LEAVE_BASE))

    was_loaded = False
    try:
        ses = db.get_session()
        was_loaded = demo.is_loaded(ses)
        ses.close()

        auth.delete_user(U)
        auth.register(U, "extcheck-not-real", "自检主体")
        auth.update_user(U, role="newbie", display_name="自检主体",
                         team="数据组", title="后端工程师", domain="it",
                         onboard_date=(date.today() - timedelta(days=19)).isoformat(),
                         active=True)

        # ---- 2. 没配任何东西时必须拒答 ----
        print("\n[2] 没配过的字段必须拒答，不能填默认值")
        if was_loaded:
            demo.clear()
        ses = db.get_session()
        expect_blocked("社保未录入 → 拒答",
                       lambda: external.hr_field(ses, U, "social_insurance"))
        expect_blocked("公积金未录入 → 拒答",
                       lambda: external.hr_field(ses, U, "housing_fund"))
        expect_blocked("岗位权限清单未配 → 拒答",
                       lambda: external.entitlements(ses, U))
        expect_blocked("联系人未配 → 拒答", lambda: external.org_contacts(ses))
        # 但算得出来的照样能答
        check("转正日期不用配就能答",
              external.hr_field(ses, U, "probation")["value"]["probation_months"] == 3)
        check("汇报线不用配就能答（来自账号表）",
              external.org_me(ses, U)["me"]["team"] == "数据组")
        ses.close()

        # ---- 3. 装载演示数据后逐项解锁 ----
        print("\n[3] 装载后逐项解锁")
        demo.load()
        ses = db.get_session()
        st = {x["system"]: x["ready"] for x in external.status(ses)}
        for name in ("HR 档案系统", "组织架构", "IT 权限系统", "IT 工单系统"):
            check(f"{name} 就绪", st.get(name) is True)
        ent = external.entitlements(ses, "demo_newbie")
        check("已开通/缺失分得开",
              "VPN 接入" in ent["granted"] and "GitLab 代码库" in ent["missing"],
              str(ent))
        ses.close()

        # ---- 4. 人被删之后不能再冒出来 ----
        print("\n[4] 悬空引用：被指派的账号删掉之后")
        auth.delete_user("demo_hr")
        ses = db.get_session()
        contacts = external.org_contacts(ses)["contacts"]
        check("已删账号不出现在联系人里",
              not any("HRBP" in k for k in contacts), str(contacts))
        detail = next(x["detail"] for x in external.status(ses) if x["system"] == "组织架构")
        check("后台报出悬空引用", "不存在的账号" in detail, detail)

        f = external.ticket_fields(ses, "demo_newbie", "prod-db:ro", "自检", 90)
        check("审批人是真人", "演示-数据平台负责人" in f["审批人"], f["审批人"])
        auth.delete_user("demo_dba")
        ses.close()
        ses = db.get_session()
        f = external.ticket_fields(ses, "demo_newbie", "prod-db:ro", "自检", 90)
        check("审批账号删掉后退回「自动分派」，不留名字",
              f["审批人"] == "由 IT 服务台按资源自动分派", f["审批人"])
        ses.close()

        # ---- 5. off 模式一律拒答 ----
        print("\n[5] off 模式：所有系统一律拒答")
        C.EXTERNAL_MODE = "off"
        ses = db.get_session()
        expect_blocked("hr_field", lambda: external.hr_field(ses, "demo_newbie", "probation"))
        expect_blocked("org_me", lambda: external.org_me(ses, "demo_newbie"))
        expect_blocked("org_contacts", lambda: external.org_contacts(ses))
        expect_blocked("entitlements", lambda: external.entitlements(ses, "demo_newbie"))
        ses.close()
        C.EXTERNAL_MODE = "builtin"

    finally:
        auth.delete_user(U)
        demo.clear()
        if was_loaded:
            demo.load()

    print("-" * 60)
    if FAILED:
        print(f"  失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("  通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
