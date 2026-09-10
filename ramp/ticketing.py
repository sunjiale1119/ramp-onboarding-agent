"""Transactional, idempotent built-in ticket operations; not a real IT connector."""
from datetime import date, timedelta
import uuid

from fastapi import HTTPException

from . import auth, db, external, reliability as rel


def validate(session, employee_id, resource, reason, duration_days):
    user = session.get(auth.User, employee_id)
    if user is None or not user.active:
        raise ValueError("申请账号不存在或已停用")
    if type(duration_days) is not int or not 1 <= duration_days <= 365:
        raise ValueError("申请时长须为 1–365 天")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError("请填写 1–2000 字的业务理由")
    if not isinstance(resource, str) or resource not in (external.all_config(session).get("entitlement_catalog") or {}):
        raise ValueError("资源不在可申请目录中，请联系管理员配置")
    approver = (external.all_config(session).get('resource_approvers') or {}).get(resource) or {}
    person = session.get(auth.User, approver.get('username')) if approver.get('username') else None
    if not person or not person.active or person.username == employee_id:
        raise ValueError('请管理员先配置有效且不是申请人本人的审批账号')


def create(session, employee_id, resource, reason, duration_days, action_id, expected_fields=None):
    if not action_id:
        raise ValueError("缺少操作编号，请重新发起申请")
    key_hash = rel.digest([employee_id, resource, reason, duration_days])
    with rel.named_lock("ticket:" + employee_id):
        old = session.get(rel.TicketReceipt, action_id)
        if old:
            if old.owner != employee_id or old.digest != key_hash:
                raise ValueError("操作编号与申请内容不一致")
            return old.response
        validate(session, employee_id, resource, reason, duration_days)
        fields = external.ticket_fields(session, employee_id, resource, reason, duration_days)
        if expected_fields is not None and fields != expected_fields:
            raise ValueError("审批配置已变化，请重新生成确认卡片")
        pending = session.query(db.Ticket).filter_by(employee_id=employee_id, resource=resource).filter(
            db.Ticket.status.in_(('pending_approval', 'approved', 'fulfilling', 'delivery_failed', 'delivered'))).first()
        if pending:
            raise ValueError(f"该资源已有未完成工单 {pending.ticket_id}，请勿重复申请")
        ap = (external.all_config(session).get("resource_approvers") or {}).get(resource) or {}
        t = db.Ticket(ticket_id="IT-" + uuid.uuid4().hex[:20], employee_id=employee_id,
            resource=resource, reason=reason.strip(), duration_days=duration_days,
            submitted_on=date.today(), approver=ap.get("username"),
            expected_by=(date.today() + timedelta(days=int(ap["sla_days"]))) if ap.get("sla_days") else None,
            status="pending_approval")
        session.add(t)
        session.flush()
        result = {"ticket_id": t.ticket_id, "action_id": action_id, "fields": fields,
            "status": t.status, "submitted_on": t.submitted_on.isoformat(),
            "expected_by": t.expected_by.isoformat() if t.expected_by else None,
            "notice": "内置工单记录，不代表企业 IT 系统已开通权限"}
        session.add(rel.TicketReceipt(action_id=action_id, owner=employee_id,
            digest=key_hash, ticket_id=t.ticket_id, response=result))
        rel.audit(session, employee_id, "ticket:created", t.ticket_id, {"action_id": action_id})
        session.commit()
        return result


def list_for(p):
    from .enterprise import Delivery
    with db.get_session() as session:
        q = session.query(db.Ticket)
        if p.role != "admin":
            assigned = session.query(Delivery.ticket_id).filter(Delivery.executor == p.username)
            q = q.filter((db.Ticket.employee_id == p.username) | (db.Ticket.approver == p.username) | db.Ticket.ticket_id.in_(assigned))
        deliveries = {d.ticket_id: d for d in session.query(Delivery).filter(Delivery.ticket_id.in_(q.with_entities(db.Ticket.ticket_id))).all()}
        return [{"ticket_id": t.ticket_id, "employee_id": t.employee_id, "resource": t.resource,
            "reason": t.reason, "status": t.status, "approver": t.approver,
            "submitted_on": str(t.submitted_on), "duration_days": t.duration_days,
            "can_cancel": t.employee_id == p.username and t.status == "pending_approval",
            "can_decide": t.approver == p.username and t.employee_id != p.username and t.status == "pending_approval",
            "executor": deliveries[t.ticket_id].executor if t.ticket_id in deliveries else None,
            "delivery_revision": deliveries[t.ticket_id].revision if t.ticket_id in deliveries else 0,
            "delivery_reference": deliveries[t.ticket_id].reference if t.ticket_id in deliveries else '',
            "delivery_note": deliveries[t.ticket_id].note if t.ticket_id in deliveries else '',
            "can_assign": (p.role == 'admin' or t.approver == p.username) and t.status in ('approved', 'fulfilling', 'delivery_failed'),
            "can_deliver": t.ticket_id in deliveries and deliveries[t.ticket_id].executor == p.username and t.employee_id != p.username and t.status == 'fulfilling',
            "can_accept": t.employee_id == p.username and t.status == 'delivered'}
            for t in q.order_by(db.Ticket.id.desc()).limit(100)]


def decide(p, ticket_id, action, note):
    if action not in ("cancel", "approve", "reject") or not note.strip() or len(note) > 1000:
        raise HTTPException(400, "请选择操作并填写 1–1000 字处理说明")
    with db.get_session() as session:
        t = session.query(db.Ticket).filter_by(ticket_id=ticket_id).with_for_update().first()
        if t is None:
            raise HTTPException(404, "工单不存在")
        allowed = t.employee_id == p.username if action == "cancel" else t.approver == p.username and t.employee_id != p.username
        if not allowed:
            raise HTTPException(403, "只能取消自己的申请，或审批明确分配给自己的他人工单")
        target = {"cancel": "cancelled", "approve": "approved", "reject": "rejected"}[action]
        if t.status == target:
            return {"ok": True, "status": target, "replayed": True}
        if t.status != "pending_approval":
            raise HTTPException(409, "工单已处理，请刷新查看最新结果")
        t.status = target
        rel.audit(session, p.username, "ticket:" + action, t.ticket_id, {"note": note.strip()})
        session.commit()
        return {"ok": True, "status": target, "notice": "仅更新本项目工单；真实权限仍需 IT 开通"}
