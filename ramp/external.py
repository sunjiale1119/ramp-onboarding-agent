"""业务查询边界：组织关系来自有效账号，个人状态来自带有效期的确认快照。

builtin 支持人工维护与受限入站同步，不代表已连接企业 HR/IT 系统。
off 明确拒绝业务查询；live 尚未实现，不能用配置项冒充真实接入。
已移除硬编码的人事推算；审批、交付、权限快照分别记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import config, db



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
    chosen = (config.EXTERNAL_MODE or "builtin").strip().lower()
    # An unimplemented "live" mode must never quietly use the built-in simulator.
    if chosen not in ("off", "builtin"):
        raise NotConnected("外部系统", "live 适配器尚未实现")
    return chosen




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


def validate_config(session, key, value):
    if not isinstance(value, dict) or len(value) > 300:
        raise ValueError('配置必须为不超过300项的对象')
    if any(not isinstance(k, str) or not 1 <= len(k) <= 64 for k in value):
        raise ValueError('配置键须为1至64字')
    catalog = get_config(session, 'entitlement_catalog') or {}
    for k, v in value.items():
        if key in ('entitlement_catalog', 'doc_catalog'):
            if not isinstance(v, str) or not 1 <= len(v) <= 200:
                raise ValueError('目录名称须为1至200字')
        elif key == 'contacts':
            if not isinstance(v, str) or resolve(session, v) is None:
                raise ValueError('联系人必须是有效账号')
        elif key == 'role_entitlements':
            if not isinstance(v, list) or any(not isinstance(item, str) or item not in catalog for item in v):
                raise ValueError('岗位权限必须引用已配置资源')
        elif key == 'resource_approvers':
            if k not in catalog or not isinstance(v, dict) or set(v) - {'username', 'sla_days'}:
                raise ValueError('审批配置必须引用已配置资源')
            if v.get('username') and (not isinstance(v['username'], str) or resolve(session, v['username']) is None):
                raise ValueError('审批人必须是有效账号')
            if 'sla_days' in v and (type(v['sla_days']) is not int or not 1 <= v['sla_days'] <= 365):
                raise ValueError('审批预计时间须为1至365天')


def all_config(session) -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    for row in session.query(db.ExtConfig).all():
        out[row.key] = row.value
    return out


def profile(session, employee_id: str) -> db.ExtProfile | None:
    return session.get(db.ExtProfile, employee_id)


# ---- HR 档案系统 -------------------------------------------------------
def hr_field(session, employee_id: str, field: str) -> dict[str, Any]:
    """Only an attributable, unexpired business record is a personal fact."""
    if mode() == "off":
        raise NotConnected("HR 档案系统", "社保、公积金、入职材料、转正日期这类信息")
    from . import enterprise
    if field not in enterprise.FIELDS or field == 'entitlements':
        raise NotConnected('HR 业务记录', f'不支持字段 {field}')
    result = enterprise.read(session, employee_id, field)
    if field == 'onboarding_docs':
        catalog = get_config(session, 'doc_catalog') or {}
        done = set(result['value']['submitted'])
        result['value'] = {'submitted': [catalog.get(k, k) for k in done],
                           'missing': [catalog[k] for k in catalog if k not in done]}
    return result


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
    if emp is None:
        raise NotConnected("IT 权限系统", "你的权限信息")

    # 岗位优先按 title 匹配，退到 role，再退到通配
    required = next((role_map[k] for k in (emp.role, emp.domain, '*') if k in role_map), [])
    from . import enterprise
    snapshot = enterprise.read(session, employee_id, 'entitlements')
    granted = set(snapshot['value']['granted'])
    catalog = conf.get("entitlement_catalog") or {}
    pending = [t.resource for t in session.query(db.Ticket).filter_by(
        employee_id=employee_id).filter(db.Ticket.status.in_(('pending_approval', 'approved', 'fulfilling', 'delivery_failed', 'delivered'))).all()]

    def label(k: str) -> str:
        return catalog.get(k, k)

    out: dict[str, Any] = {
        "granted": [label(k) for k in sorted(granted)],
        "missing": [label(k) for k in required if k not in granted and k not in pending],
        "pending": [label(k) for k in pending],
        "source": snapshot['source'], "as_of": snapshot['as_of'], "valid_until": snapshot['valid_until'],
        "notice": '已开通项来自确认快照；工单交付不会自动改写权限清单，需负责人或企业接口同步',
    }
    if resource:
        ap = (conf.get("resource_approvers") or {}).get(resource) or {}
        who = resolve(session, ap.get("username"))
        out["resource"] = resource
        out["already_granted"] = resource in granted
        # 未配置即明确缺失，不能暗示系统已自动分派。
        out["approver"] = who.label() if who else "尚未配置审批人，请联系管理员"
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
        "审批人": who.label() if who else "尚未配置审批人，请联系管理员",
        "时长": f"申请 {duration_days} 天；真实开通及回收由 IT 执行",
        "预计时长": f"{sla} 个自然日" if sla else "尚未配置预计时间",
    }


def create_ticket(session, employee_id: str, resource: str,
                  reason: str, duration_days: int = 90, *, action_id=None, expected_fields=None) -> dict[str, Any]:
    if mode() == "off":
        raise NotConnected("IT 工单系统", "提交权限申请工单")

    from . import ticketing
    return ticketing.create(session, employee_id, resource, reason, duration_days, action_id, expected_fields)


# ---- 给后台看的接入状态 ------------------------------------------------
def status(session) -> list[dict[str, Any]]:
    """四个系统当前各自能不能用，以及缺什么。管理后台「外部系统」页签读它。"""
    conf = all_config(session)
    from .enterprise import BusinessRecord, serialize
    current = {}
    for record in session.query(BusinessRecord).order_by(BusinessRecord.revision.desc()).all():
        current.setdefault((record.employee_id, record.field), record)
    n_profiles = len({r.employee_id for r in current.values() if r.field != 'entitlements' and serialize(r)['usable']})
    contacts = {k: resolve(session, v) for k, v in (conf.get("contacts") or {}).items()}
    live_contacts = [k for k, v in contacts.items() if v is not None]
    dangling = [k for k, v in contacts.items() if v is None]

    return [
        {"system": "HR 档案系统", "tool": "hr_query",
         "always": "不自动推算个人社保、年假或转正结论",
         "needs": "HR确认记录：来源编号、快照日期和有效期",
         "ready": n_profiles > 0, "detail": f"{n_profiles} 人有有效期内的快照；以各字段最新版本为准"},
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
