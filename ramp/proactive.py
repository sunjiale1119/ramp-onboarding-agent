"""主动推送：Day 1 / 3 / 5 / 14 / 21 / 30 的节点提醒。

**最高价值的动作是"你还没问，我就提醒你"** ——因为新人根本不知道
自己该问什么。这也是这个场景必须是 Agent 而不是 RAG 问答的第四条理由。

两个产品约束写死在代码里：

1. **打扰预算**：每周最多 2 次。超了就不推，宁可漏也不烦——
   一个让人想关掉通知的产品，后面所有价值都归零。

2. **掉队信号只推给 mentor，不推给新人本人**（见 memory.stuck_signal）。
   直接对新人说"你落后了"产生的不是紧迫感，是被监视感。

成本上还有一条：推送安排在**空闲时段**跑，单价是高峰的一半。
模板化内容 + tier2 + 空闲时段，单次成本可以忽略不计。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from . import config, db, memory


def _timeline() -> list[dict[str, Any]]:
    data = json.loads((config.SEED_DIR / "mock_systems.json").read_text(encoding="utf-8"))
    return data["onboarding_timeline"]


def pushes_this_week(session, employee_id: str, today: date | None = None) -> int:
    today = today or date.today()
    since = today - timedelta(days=7)
    return (
        session.query(db.PushLog)
        .filter(db.PushLog.employee_id == employee_id, db.PushLog.created_at >= since)
        .count()
    )


def due_for(session, employee_id: str, today: date | None = None) -> dict[str, Any] | None:
    """今天该不该推、推什么。返回 None 表示今天不打扰。"""
    emp = session.get(db.Employee, employee_id)
    if emp is None:
        return None
    today = today or date.today()
    day = emp.day_index(today)

    node = next((n for n in _timeline() if n["day"] == day), None)
    if node is None:
        return None

    already = (
        session.query(db.PushLog)
        .filter_by(employee_id=employee_id, day_index=day, kind=node["kind"])
        .first()
    )
    if already:
        return None

    used = pushes_this_week(session, employee_id, today)
    if used >= config.WEEKLY_PUSH_BUDGET:
        return {
            "skipped": True,
            "reason": f"本周打扰预算已用满（{used}/{config.WEEKLY_PUSH_BUDGET}）",
            "day": day,
            "kind": node["kind"],
        }

    return {
        "skipped": False,
        "day": day,
        "kind": node["kind"],
        "title": node["title"],
        "content": node["content"],
        "budget": f"{used + 1}/{config.WEEKLY_PUSH_BUDGET}",
        "off_peak": not config.is_peak(),
    }


def enrich(session, employee_id: str, node: dict[str, Any]) -> dict[str, Any]:
    """给推送补上个性化事实。

    Day 3 的权限提醒必须带**具体缺哪几项**，否则就是一句正确的废话——
    "记得申请权限"不如"你还差 prod-db:ro，这项要 2 天审批"。
    """
    from .tools import registry

    ctx = {"employee_id": employee_id, "domain": "it"}
    extra: dict[str, Any] = {}

    if node["kind"] == "entitlement":
        r = registry.execute("it_entitlements", {}, domain="it", context=ctx)
        if r.ok and r.data.get("missing"):
            extra["missing"] = r.data["missing"]
            node["content"] += "\n\n还缺：" + "、".join(r.data["missing"])
        else:
            return {**node, "skipped": True, "reason": "权限已齐，无需提醒"}

    elif node["kind"] == "docs":
        r = registry.execute("hr_query", {"field": "onboarding_docs"}, domain="hr", context=ctx)
        if r.ok:
            v = r.data["value"]
            if v.get("all_clear"):
                return {**node, "skipped": True, "reason": "材料已齐，无需提醒"}
            extra["missing_docs"] = v.get("missing", [])
            node["content"] += "\n\n还缺：" + "、".join(v.get("missing", []))

    elif node["kind"] == "checkpoint":
        sig = memory.stuck_signal(session, employee_id)
        extra["stuck_signal"] = sig
        # 关键：卡点汇总推给 mentor，给新人的只是一句中性的知会
        node["audience"] = "mentor" if sig else "self"

    return {**node, **extra}


def run_for(session, employee_id: str, today: date | None = None) -> dict[str, Any] | None:
    """跑一次某位新人的推送检查。cron 每天调一次。"""
    node = due_for(session, employee_id, today)
    if node is None or node.get("skipped"):
        return node

    node = enrich(session, employee_id, node)
    if node.get("skipped"):
        return node

    emp = session.get(db.Employee, employee_id)
    session.add(
        db.PushLog(
            employee_id=employee_id,
            day_index=node["day"],
            kind=node["kind"],
            content=node["content"],
        )
    )
    session.commit()

    node["to"] = emp.mentor_name if node.get("audience") == "mentor" else emp.name
    return node


def run_all(today: date | None = None) -> list[dict[str, Any]]:
    """所有在职新人跑一遍。这是 cron 的入口。"""
    session = db.get_session()
    out = []
    try:
        for emp in session.query(db.Employee).all():
            r = run_for(session, emp.id, today)
            if r:
                out.append({"employee": emp.name, **r})
    finally:
        session.close()
    return out


def preview_timeline(employee_id: str) -> list[dict[str, Any]]:
    """把整条 30 天时间线展开，给前端右栏用。"""
    session = db.get_session()
    try:
        emp = session.get(db.Employee, employee_id)
        if emp is None:
            return []
        today_idx = emp.day_index()
        done = {
            (p.day_index, p.kind)
            for p in session.query(db.PushLog).filter_by(employee_id=employee_id).all()
        }
        out = []
        for n in _timeline():
            state = (
                "done" if n["day"] < today_idx or (n["day"], n["kind"]) in done
                else "now" if n["day"] == today_idx
                else "next"
            )
            out.append({"day": n["day"], "title": n["title"], "state": state})
        return out
    finally:
        session.close()
