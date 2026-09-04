"""五个内置工具。

按域和读写分：
    knowledge_search   全域   只读
    hr_query           hr     只读
    org_lookup         全域   只读
    it_entitlements    it     只读
    it_create_ticket   it     **写入** → 强制人工确认

外部系统全部走 seed/mock_systems.json 的假数据，并保留了一个
超时分支（salary-system），用来验证工具失败后的降级文案。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .. import config
from . import ToolError, registry


def _not_connected(system: str, what: str) -> ToolError:
    """外部系统未接入时的统一回应。

    ## 为什么不编一个

    原来 mock_systems.json 里有一整套虚构记录：某人的社保状态、
    某人的权限清单、一个叫「李敏」的审批人。Agent 答得很流畅，
    直到管理员去后台核对，发现**系统里根本没有李敏这个人**。

    现在数据清空了，工具的正确行为不是报一个技术错误，
    也不是退回去猜，而是**说清楚三件事**：查的是哪个系统、
    为什么查不到、接下来该找谁。

    一个诚实的"我查不到"，比一个编出来的答案有用得多 ——
    前者用户知道下一步做什么，后者用户会照着错的信息去办事。
    """
    return ToolError(
        f"{system} 未接入",
        user_message=(f"{what}需要查{system}，这个系统当前**未接入**，"
                      f"所以我查不到你的具体数据。\n\n"
                      f"这不是权限问题，也不是我不愿意查 —— "
                      f"是这套演示环境没有连接企业的真实系统。"
                      f"建议直接联系对应的负责部门确认。"),
    )


_MOCK: dict[str, Any] | None = None


def mock() -> dict[str, Any]:
    global _MOCK
    if _MOCK is None:
        _MOCK = json.loads((config.SEED_DIR / "mock_systems.json").read_text(encoding="utf-8"))
    return _MOCK


def _emp_id(ctx: dict[str, Any]) -> str:
    eid = ctx.get("employee_id")
    if not eid:
        raise ToolError("上下文缺少 employee_id", user_message="我没能确认你的身份，请重新进入会话。")
    return eid


# ------------------------------------------------------------------ 知识检索
@registry.add(
    name="knowledge_search",
    description="在公司知识库中检索制度、流程、规范。适用于'规则是什么'类问题；不要用它查个人档案数据。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题，用完整的自然语言句子，不要只给关键词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 3，最多 5", "default": 3},
        },
        "required": ["query"],
    },
    domains=("hr", "it", "biz"),
)
def knowledge_search(query: str, top_k: int = 3, *, _context: dict[str, Any]) -> dict[str, Any]:
    from .. import knowledge

    r = knowledge.search(query, domain=_context.get("domain"), top_k=min(max(top_k, 1), 5))
    return {
        "best_score": round(r.best_score, 4),
        "confident": r.confident,
        "threshold": config.CONFIDENCE_THRESHOLD,
        "hits": [h.to_dict() for h in r.hits],
    }


# ------------------------------------------------------------------ HR 只读
@registry.add(
    name="hr_query",
    description=(
        "查询**当前用户本人**的 HR 档案数据：社保、公积金、入职材料、试用期与转正日期、假期余额。"
        "只能查本人，不接受员工姓名或工号参数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "enum": ["social_insurance", "housing_fund", "onboarding_docs", "probation", "leave_balance"],
                "description": "要查的字段。social_insurance=社保, housing_fund=公积金, onboarding_docs=入职材料, probation=试用期与转正, leave_balance=假期余额",
            }
        },
        "required": ["field"],
    },
    domains=("hr",),
)
def hr_query(field: str, *, _context: dict[str, Any]) -> dict[str, Any]:
    eid = _emp_id(_context)
    rec = mock()["hr_system"].get(eid)
    if rec is None:
        raise _not_connected("HR 档案系统", "社保、公积金、入职材料、转正日期这类信息")
    if field not in rec:
        raise ToolError(f"未知字段: {field}")
    return {"field": field, "value": rec[field], "source": "hr_system（只读）"}


# ------------------------------------------------------------------ 组织架构
@registry.add(
    name="org_lookup",
    description="查询组织架构：本人的团队、汇报线、mentor，以及 HRBP / IT 服务台 / 行政的联系方式。不返回薪酬与绩效。",
    parameters={
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "enum": ["me", "contacts"],
                "description": "me=查本人的团队与汇报线；contacts=查 HRBP/IT/行政的联系方式",
            }
        },
        "required": ["what"],
    },
    domains=("hr", "it", "biz"),
)
def org_lookup(what: str, *, _context: dict[str, Any]) -> dict[str, Any]:
    d = mock()["org_directory"]
    if what == "contacts":
        c = d.get("contacts") or {}
        if not c:
            raise _not_connected("组织架构", "HRBP、IT 服务台、行政的联系方式")
        return {"contacts": c}
    eid = _emp_id(_context)
    rec = d.get(eid)
    if rec is None:
        raise _not_connected("组织架构", "汇报线、团队、带教关系")
    return {"me": rec}


# ------------------------------------------------------------------ IT 权限
@registry.add(
    name="it_entitlements",
    description="查询当前用户已开通的权限、岗位应有的权限，以及两者的差额（还缺哪些）。Day 3 的主动提醒就基于这个差额。",
    parameters={
        "type": "object",
        "properties": {
            "resource": {
                "type": "string",
                "description": "可选。给出资源名（如 prod-db:ro）则同时返回该资源的审批人与预计时长；不给则返回全量差额。",
            }
        },
    },
    domains=("it",),
)
def it_entitlements(resource: str | None = None, *, _context: dict[str, Any]) -> dict[str, Any]:
    eid = _emp_id(_context)
    desk = mock()["it_servicedesk"]

    # 故障演练：查 salary-system 时模拟超时，用来验证降级路径
    if resource == desk.get("failure_simulation", {}).get("timeout_on_resource"):
        raise ToolError(
            "上游系统超时",
            user_message="薪资系统这会儿没响应。这类信息我也不提供查询，请直接联系 HRBP 王倩。",
        )

    ent = desk["entitlements"].get(eid)
    if ent is None:
        raise _not_connected("IT 权限系统", "已开通账号、岗位应有权限、待审批项")

    granted = set(ent["granted"])
    required = set(ent["required_by_role"])
    missing = sorted(required - granted)

    out: dict[str, Any] = {
        "granted": sorted(granted),
        "missing": missing,
        "pending": ent.get("pending", []),
    }
    if resource:
        ap = desk["approvers"].get(resource)
        out["resource"] = resource
        out["already_granted"] = resource in granted
        out["approver"] = ap or {"name": "未知", "title": "请咨询 IT 服务台"}
    return out


# ------------------------------------------------------------------ IT 工单（写入）
@registry.add(
    name="it_create_ticket",
    description=(
        "向 IT 服务台提交权限申请工单。"
        "**用户提出申请意图时，直接调用本工具，不要在文字里请示。**"
        "确认环节由系统自动完成：调用后不会立即落库，系统会把将要写入的完整字段"
        "展示给用户并等待点头。你的职责是把参数填对，不是替系统去问。"
        "只有当 resource 或 reason 确实无法从对话中确定时，才回文字向用户追问。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "resource": {"type": "string", "description": "申请的权限资源名，如 prod-db:ro、vpn、ga-dashboard"},
            "reason": {"type": "string", "description": "业务理由，一句话说明为什么需要。审批人主要看这一栏，不要写空话。"},
            "duration_days": {"type": "integer", "description": "申请时长（天），默认 90，到期自动回收", "default": 90},
        },
        "required": ["resource", "reason"],
    },
    domains=("it",),
    writes=True,
)
def it_create_ticket(
    resource: str,
    reason: str,
    duration_days: int = 90,
    *,
    _preview: bool = True,
    _context: dict[str, Any],
) -> dict[str, Any]:
    eid = _emp_id(_context)
    desk = mock()["it_servicedesk"]
    org = mock()["org_directory"].get(eid, {})

    # 审批人来自外部工单系统的配置。**未接入时不能瞎填一个名字。**
    #
    # 这一行以前的默认值会造出一个具体的人：`{"name": "李敏", ...}`
    # 来自虚构配置，于是 Agent 回答"审批人李敏（数据平台负责人）"，
    # 用户去后台核对 —— 系统里根本没有李敏这个人。
    #
    # 提交一个工单却说不清谁来批，本身是可接受的（真实工单系统会自动分派）；
    # 说出一个不存在的人名，不可接受。**宁可说"待分派"，不可以编一个人。**
    ap = (desk.get("approvers") or {}).get(resource)
    approver = (f"{ap['name']}（{ap.get('title', '')}）" if ap
                else "由 IT 服务台按资源自动分派")
    sla = ap.get("sla_days", 2) if ap else None

    who = _context.get("employee_name") or eid
    team_role = " · ".join(x for x in (org.get("team"), org.get("role")) if x)
    fields = {
        "系统": "IT 服务台 · 权限申请",
        "申请人": f"{who}（{team_role}）" if team_role else str(who),
        "权限项": resource,
        "理由": reason,
        "审批人": approver,
        "时长": f"{duration_days} 天，到期自动回收",
        "预计时长": f"{sla} 个工作日" if sla else "以 IT 服务台的分派结果为准",
    }

    if _preview:
        # 只回显，不落库——真正的提交在用户确认之后
        return {"preview": True, "fields": fields}

    no = desk.get("next_ticket_no", 10001)
    desk["next_ticket_no"] = no + 1
    ticket = {
        "ticket_id": f"IT-{no}",
        "fields": fields,
        "status": "pending_approval",
        "submitted_on": date.today().isoformat(),
        "revocable_until_minutes": 5,
    }
    if sla:
        ticket["expected_by"] = (date.today() + timedelta(days=sla)).isoformat()
    desk.setdefault("tickets", []).append(ticket)
    return ticket
