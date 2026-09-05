"""外部系统适配层：HR 档案 / 组织架构 / IT 权限 / IT 工单。

## 为什么有这一层

这四个系统在真实企业里都是现成的，接口各不相同。演示环境接不到，
但产品不能因此就没有这部分能力 —— 于是做成**可插拔**：

    off       永远返回"未接入"。保留这一档，是因为
              「外部系统没接时诚实说查不到、绝不编值」本身是一项能力，
              评测里 cross_system 那 12 题考的就是它。
              加了内置实现就把这条路径丢掉，是亏的。
    builtin   内置实现（本模块）。数据来源见下。
    live      真 HTTP 接入。接口契约就是本模块的函数签名，实现待补。

切换靠 `RAMP_EXTERNAL_MODE`，不改调用方代码。

## 数据从哪来 —— 这是本模块唯一重要的设计

上一版这些数据全在 `seed/mock_systems.json` 里，包括一整套编出来的人：
审批人「李敏」、Mentor「陈昊」、HRBP「王倩」。Agent 答得很流畅，
直到管理员登录后台核对，发现**系统里根本没有李敏这个人**。
那一刻整个系统的可信度就塌了 —— 一个员工服务产品，最不能出错的就是「人」。

所以现在按数据的性质分四类，处理方式完全不同：

    人            Mentor / HRBP / 审批人      **必须是真实账号**，存 username，
                                              显示时去 users 表解析
    能算的        转正日期 / 试用期剩余 /     **不存**，每次算。存一份算得出的值
                  年假额度 / 社保起缴月        就是制造一个会和事实不一致的副本
    制度性映射    岗位→应有权限 /             管理员维护，和知识库同级
                  资源→审批账号
    个人业务状态  社保交没交 / 材料齐没齐     管理员录入；**没录过就是 unknown，
                                              工具诚实说查不到**，不是"未参保"

最后那条特别重要：`unknown` 和 `not_started` 必须分开。
把"我不知道"显示成"未参保"，是另一种形式的编造。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import config, db

# ---- 可调规则 ----------------------------------------------------------
PROBATION_MONTHS = 3       # 试用期，与知识库《员工手册》一致
SOCIAL_CUTOFF_DAY = 20     # 社保每月申报截止日：当月 20 号前入职算当月，否则次月
ANNUAL_LEAVE_BASE = 5      # 全年年假基数（天）
TICKET_START_NO = 10001


class NotConnected(Exception):
    """这个系统（或这个字段）没有可用数据。

    调用方负责翻译成给用户看的话。**不要在这里编一个默认值** ——
    "查不到"和"查到了是空的"是两件事。
    """

    def __init__(self, system: str, what: str):
        self.system = system
        self.what = what
        super().__init__(f"{system} 未接入：{what}")


def mode() -> str:
    return (config.EXTERNAL_MODE or "builtin").strip().lower()


# ---- 能算的：一律现算，不存 -------------------------------------------
def _add_months(d: date, n: int) -> date:
    """加 n 个月。落到不存在的日期（1/31 + 1 月）时退到当月最后一天。"""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    # 从原日期往前退，退到该月存在的那一天为止。
    # 28 号及以下任何月份都存在，所以循环一定收敛。
    day = d.day
    while day > 28:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1
    return date(y, m, day)


def probation_of(onboard: date, today: date | None = None) -> dict[str, Any]:
    """试用期与转正日期 —— 全部算出来，一个字段都不用录。"""
    today = today or date.today()
    end = _add_months(onboard, PROBATION_MONTHS)
    return {
        "onboard_date": onboard.isoformat(),
        "probation_months": PROBATION_MONTHS,
        "probation_end": end.isoformat(),
        "days_left": (end - today).days,
        "day_index": (today - onboard).days + 1,
        "passed": today >= end,
    }


def annual_leave_of(onboard: date, used: float = 0.0, today: date | None = None) -> dict[str, Any]:
    """年假额度，按《职工带薪年休假条例》折算。

    新进职工当年度年休假 = 当年剩余日历天数 ÷ 365 × 全年应享天数，
    **不足 1 整天的部分不计**。这是法规里的算法，不是我编的系数 ——
    所以它属于"能算的"，不该进数据库。
    """
    today = today or date.today()
    if onboard.year < today.year:
        total = float(ANNUAL_LEAVE_BASE)
        basis = "已满一个完整年度，按全年基数"
    else:
        remain = (date(onboard.year, 12, 31) - onboard).days + 1
        total = float(int(remain / 365 * ANNUAL_LEAVE_BASE))   # 向下取整
        basis = f"入职当年折算：{remain} 天 ÷ 365 × {ANNUAL_LEAVE_BASE} 天，不足 1 天不计"
    return {"total": total, "used": float(used), "remaining": total - float(used), "basis": basis}


def social_start_month(onboard: date) -> dict[str, Any]:
    """社保起缴月：当月 20 号前入职算当月，否则次月。

    这是社保申报的月度节点决定的，不是随便定的 —— 所以同样能算。
    """
    if onboard.day <= SOCIAL_CUTOFF_DAY:
        m = date(onboard.year, onboard.month, 1)
        why = f"入职日 {onboard.day} 号 ≤ 申报截止 {SOCIAL_CUTOFF_DAY} 号，当月起缴"
    else:
        m = _add_months(date(onboard.year, onboard.month, 1), 1)
        why = f"入职日 {onboard.day} 号 > 申报截止 {SOCIAL_CUTOFF_DAY} 号，次月起缴"
    return {"month": f"{m.year}-{m.month:02d}", "reason": why}


# ---- 人：一律去 users 表解析，绝不存名字 ------------------------------
@dataclass(frozen=True)
class Person:
    username: str
    name: str
    title: str
    team: str

    def label(self) -> str:
        bits = [b for b in (self.title, self.team) if b]
        return f"{self.name}（{' · '.join(bits)}）" if bits else self.name


def resolve(session, username: str | None) -> Person | None:
    """username → 真人。**解析不出来就返回 None，不造一个占位的人。**"""
    if not username:
        return None
    from .auth import User

    u = session.get(User, username)
    if u is None or not u.active:
        return None
    return Person(u.username, u.display_name, u.title or "", u.team or "")


# ---- 配置读写 ----------------------------------------------------------
DEFAULT_CONFIG: dict[str, Any] = {
    # 权限项目录：key → 显示名
    "entitlement_catalog": {},
    # 岗位 → 应有权限项。键优先匹配 title，其次 role
    "role_entitlements": {},
    # 资源 → {"username": 审批账号, "sla_days": 审批时长}
    "resource_approvers": {},
    # 联系人 → username
    "contacts": {},
    # 入职材料清单：key → 显示名
    "doc_catalog": {},
}


def get_config(session, key: str) -> Any:
    row = session.get(db.ExtConfig, key)
    if row is None:
        return DEFAULT_CONFIG.get(key)
    return row.value


def set_config(session, key: str, value: Any) -> None:
    row = session.get(db.ExtConfig, key)
    if row is None:
        session.add(db.ExtConfig(key=key, value=value))
    else:
        row.value = value


def all_config(session) -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    for row in session.query(db.ExtConfig).all():
        out[row.key] = row.value
    return out


def profile(session, employee_id: str) -> db.ExtProfile | None:
    return session.get(db.ExtProfile, employee_id)


# ---- HR 档案系统 -------------------------------------------------------
def hr_field(session, employee_id: str, field: str) -> dict[str, Any]:
    """查 HR 档案的一个字段。

    **按字段判断能不能答，而不是整个系统一刀切。**
    probation / leave_balance 只要有入职日期就能算，永远可用；
    social_insurance / housing_fund / onboarding_docs 需要管理员录过，
    没录过就诚实说查不到。
    """
    if mode() == "off":
        raise NotConnected("HR 档案系统", "社保、公积金、入职材料、转正日期这类信息")

    emp = session.get(db.Employee, employee_id)
    if emp is None or emp.onboard_date is None:
        raise NotConnected("HR 档案系统", "你的档案信息")

    # ---- 算得出来的两个字段 ----
    if field == "probation":
        return {"field": field, "value": probation_of(emp.onboard_date),
                "source": "按入职日期与试用期规则推算"}
    if field == "leave_balance":
        p = profile(session, employee_id)
        return {"field": field,
                "value": annual_leave_of(emp.onboard_date, p.leave_used if p else 0.0),
                "source": "按《职工带薪年休假条例》折算"}

    # ---- 需要录入的三个字段 ----
    p = profile(session, employee_id)
    if p is None:
        raise NotConnected("HR 档案系统", "社保、公积金、入职材料这类信息")

    if field == "social_insurance":
        if p.social_status == "unknown":
            raise NotConnected("HR 档案系统", "你的社保参保状态")
        v: dict[str, Any] = {"status": p.social_status,
                             "start_month": social_start_month(emp.onboard_date)}
        if p.social_from:
            v["actual_from"] = p.social_from.isoformat()
        return {"field": field, "value": v, "source": "HR 档案系统（只读）"}

    if field == "housing_fund":
        if p.fund_status == "unknown":
            raise NotConnected("HR 档案系统", "你的公积金缴存状态")
        return {"field": field,
                "value": {"status": p.fund_status, "base": p.fund_base},
                "source": "HR 档案系统（只读）"}

    if field == "onboarding_docs":
        catalog = get_config(session, "doc_catalog") or {}
        if not catalog:
            raise NotConnected("HR 档案系统", "入职材料清单")
        done = set(p.docs or [])
        return {"field": field, "source": "HR 档案系统（只读）", "value": {
            "submitted": [catalog.get(k, k) for k in catalog if k in done],
            "missing": [catalog.get(k, k) for k in catalog if k not in done],
        }}

    raise NotConnected("HR 档案系统", f"字段 {field}")


# ---- 组织架构 ----------------------------------------------------------
def org_me(session, employee_id: str) -> dict[str, Any]:
    """本人的团队与汇报线。**全部来自 users 表，一条都不用编。**"""
    if mode() == "off":
        raise NotConnected("组织架构", "汇报线、团队、带教关系")

    me = resolve(session, employee_id)
    if me is None:
        raise NotConnected("组织架构", "你的组织信息")
    emp = session.get(db.Employee, employee_id)
    mentor = resolve(session, emp.mentor_id) if emp else None
    return {"me": {
        "name": me.name, "team": me.team, "title": me.title,
        "mentor": mentor.label() if mentor else None,
        "day_index": emp.day_index() if emp else None,
    }}


def org_contacts(session) -> dict[str, Any]:
    """HRBP / IT 服务台 / 行政的联系方式。

    配置里存的是 username。**解析不出真人的条目直接丢掉**，
    宁可少给一个联系人，也不给一个不存在的人。
    """
    if mode() == "off":
        raise NotConnected("组织架构", "HRBP、IT 服务台、行政的联系方式")

    labels = {"hrbp": "HRBP", "it_desk": "IT 服务台", "admin_office": "行政"}
    conf = get_config(session, "contacts") or {}
    out = {}
    for key, username in conf.items():
        who = resolve(session, username)
        if who is not None:
            out[labels.get(key, key)] = who.label()
    if not out:
        raise NotConnected("组织架构", "HRBP、IT 服务台、行政的联系方式")
    return {"contacts": out}


# ---- IT 权限系统 -------------------------------------------------------
def entitlements(session, employee_id: str, resource: str | None = None) -> dict[str, Any]:
    if mode() == "off":
        raise NotConnected("IT 权限系统", "已开通账号、岗位应有权限、待审批项")

    conf = all_config(session)
    role_map = conf.get("role_entitlements") or {}
    if not role_map:
        raise NotConnected("IT 权限系统", "岗位权限清单")

    emp = session.get(db.Employee, employee_id)
    p = profile(session, employee_id)
    if emp is None:
        raise NotConnected("IT 权限系统", "你的权限信息")

    # 岗位优先按 title 匹配，退到 role，再退到通配
    required = role_map.get(emp.role) or role_map.get(emp.domain) or role_map.get("*") or []
    granted = set(p.granted or []) if p else set()
    catalog = conf.get("entitlement_catalog") or {}
    pending = [t.resource for t in session.query(db.Ticket).filter_by(
        employee_id=employee_id, status="pending_approval").all()]

    def label(k: str) -> str:
        return catalog.get(k, k)

    out: dict[str, Any] = {
        "granted": [label(k) for k in sorted(granted)],
        "missing": [label(k) for k in required if k not in granted and k not in pending],
        "pending": [label(k) for k in pending],
    }
    if resource:
        ap = (conf.get("resource_approvers") or {}).get(resource) or {}
        who = resolve(session, ap.get("username"))
        out["resource"] = resource
        out["already_granted"] = resource in granted
        # 解析不出审批人时说"按资源自动分派"，**不编名字**
        out["approver"] = who.label() if who else "由 IT 服务台按资源自动分派"
        out["sla_days"] = ap.get("sla_days")
    return out


# ---- IT 工单 -----------------------------------------------------------
def ticket_fields(session, employee_id: str, resource: str,
                  reason: str, duration_days: int) -> dict[str, Any]:
    """确认卡片上展示的字段。写操作前先给人看的就是这个。"""
    conf = all_config(session)
    ap = (conf.get("resource_approvers") or {}).get(resource) or {}
    who = resolve(session, ap.get("username"))
    me = resolve(session, employee_id)
    sla = ap.get("sla_days")
    return {
        "系统": "IT 服务台 · 权限申请",
        "申请人": me.label() if me else employee_id,
        "权限项": resource,
        "理由": reason,
        "审批人": who.label() if who else "由 IT 服务台按资源自动分派",
        "时长": f"{duration_days} 天，到期自动回收",
        "预计时长": f"{sla} 个工作日" if sla else "以 IT 服务台的分派结果为准",
    }


def create_ticket(session, employee_id: str, resource: str,
                  reason: str, duration_days: int = 90) -> dict[str, Any]:
    if mode() == "off":
        raise NotConnected("IT 工单系统", "提交权限申请工单")

    conf = all_config(session)
    ap = (conf.get("resource_approvers") or {}).get(resource) or {}
    sla = ap.get("sla_days")
    last = session.query(db.Ticket).order_by(db.Ticket.id.desc()).first()
    no = (int(last.ticket_id.split("-")[-1]) + 1) if last else TICKET_START_NO

    t = db.Ticket(
        ticket_id=f"IT-{no}",
        employee_id=employee_id,
        resource=resource,
        reason=reason,
        duration_days=duration_days,
        submitted_on=date.today(),
        approver=ap.get("username"),
        expected_by=(date.today() + timedelta(days=sla)) if sla else None,
    )
    session.add(t)
    session.commit()
    return {
        "ticket_id": t.ticket_id,
        "fields": ticket_fields(session, employee_id, resource, reason, duration_days),
        "status": t.status,
        "submitted_on": t.submitted_on.isoformat(),
        "expected_by": t.expected_by.isoformat() if t.expected_by else None,
        "revocable_until_minutes": 5,
    }


# ---- 给后台看的接入状态 ------------------------------------------------
def status(session) -> list[dict[str, Any]]:
    """四个系统当前各自能不能用，以及缺什么。管理后台「外部系统」页签读它。"""
    conf = all_config(session)
    n_profiles = session.query(db.ExtProfile).count()
    contacts = {k: resolve(session, v) for k, v in (conf.get("contacts") or {}).items()}
    live_contacts = [k for k, v in contacts.items() if v is not None]
    dangling = [k for k, v in contacts.items() if v is None]

    return [
        {"system": "HR 档案系统", "tool": "hr_query",
         "always": "转正日期 / 试用期剩余 / 年假额度（算出来的）",
         "needs": "社保 / 公积金 / 入职材料（需录入）",
         "ready": n_profiles > 0, "detail": f"已录入 {n_profiles} 人"},
        {"system": "组织架构", "tool": "org_lookup",
         "always": "汇报线 / 团队 / Mentor（来自账号表）",
         "needs": "HRBP / IT 服务台 / 行政（需指定账号）",
         "ready": bool(live_contacts),
         "detail": (f"已配 {len(live_contacts)} 个联系人"
                    + (f"；⚠ {len(dangling)} 个指向了不存在的账号" if dangling else ""))},
        {"system": "IT 权限系统", "tool": "it_entitlements",
         "always": "—", "needs": "岗位权限清单 + 个人已开通项",
         "ready": bool(conf.get("role_entitlements")),
         "detail": f"{len(conf.get('role_entitlements') or {})} 个岗位有清单"},
        {"system": "IT 工单系统", "tool": "it_create_ticket",
         "always": "工单落库（本项目自己的表）",
         "needs": "资源→审批账号映射",
         "ready": bool(conf.get("resource_approvers")),
         "detail": f"{len(conf.get('resource_approvers') or {})} 个资源已配审批人"},
    ]
