"""Single-company data ownership and manual fulfillment, not a simulated HR system."""
from datetime import date, datetime, timedelta
import hashlib
import hmac
import math
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Date, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from . import auth, db, external, reliability as rel

FIELDS = {'probation', 'leave_balance', 'social_insurance', 'housing_fund', 'onboarding_docs', 'entitlements'}


def managed_fields():
    if len(os.getenv('RAMP_DATA_INGEST_TOKEN_SHA256', '')) != 64:
        return set()
    return set(os.getenv('RAMP_DATA_INGEST_FIELDS', 'probation,leave_balance,social_insurance,housing_fund,onboarding_docs').split(',')) & FIELDS


def authorize_ingest(request, field):
    if request.url.scheme != 'https':
        raise HTTPException(400, '数据接入只接受 HTTPS；反向代理须限制可信来源')
    expected = os.getenv('RAMP_DATA_INGEST_TOKEN_SHA256', '')
    if len(expected) != 64:
        raise HTTPException(503, '企业数据接入尚未配置')
    header = request.headers.get('authorization', '')
    token = header[7:] if header.startswith('Bearer ') else ''
    if not token or len(token) > 512 or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(401, '接入凭据无效')
    if field not in managed_fields():
        raise HTTPException(403, '该数据源无权访问此字段')


class BusinessRecord(db.Base):
    __tablename__ = 'business_records'
    __table_args__ = (UniqueConstraint('employee_id', 'field', 'revision'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String(64), index=True)
    field: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    value: Mapped[dict] = mapped_column(JSON)
    as_of: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date] = mapped_column(Date)
    source_ref: Mapped[str] = mapped_column(String(240))
    actor: Mapped[str] = mapped_column(String(80))
    source_kind: Mapped[str] = mapped_column(String(16))
    revoked: Mapped[int] = mapped_column(Integer, default=0)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Delivery(db.Base):
    __tablename__ = 'ticket_deliveries'
    ticket_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    executor: Mapped[str] = mapped_column(String(64))
    assigned_by: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str] = mapped_column(String(500), default='')
    note: Mapped[str] = mapped_column(String(1000), default='')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def migrate(engine=None):
    for cls in (BusinessRecord, Delivery):
        cls.__table__.create(engine or db.engine(), checkfirst=True)


class RecordIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    employee_id: str = Field(min_length=1, max_length=64)
    field: str
    value: dict
    as_of: date
    valid_until: date
    source_ref: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=0)
    revoked: bool = False


def latest(session, employee_id, field):
    return session.query(BusinessRecord).filter_by(employee_id=employee_id, field=field).order_by(BusinessRecord.revision.desc()).first()


def serialize(r):
    return {'employee_id': r.employee_id, 'field': r.field, 'revision': r.revision,
            'value': r.value, 'as_of': str(r.as_of), 'valid_until': str(r.valid_until),
            'source_ref': r.source_ref, 'source_kind': r.source_kind, 'actor': r.actor,
            'revoked': bool(r.revoked), 'usable': not r.revoked and r.as_of <= date.today() <= r.valid_until}


def read(session, employee_id, field):
    row = latest(session, employee_id, field)
    if row is None or not serialize(row)['usable']:
        raise external.NotConnected('已确认业务记录', f'{field} 的记录缺失、已撤销或超过有效期；请联系 HR / IT 更新，不能据此判断尚未办理')
    return {'field': field, 'value': row.value, 'source': row.source_ref,
            'as_of': str(row.as_of), 'valid_until': str(row.valid_until), 'revision': row.revision,
            'source_kind': row.source_kind, 'notice': '这是注明日期的业务快照，不是实时查询；制度文档不能替代个人记录'}


def validate_value(session, field, value):
    keys = {'probation': {'probation_end'}, 'leave_balance': {'total', 'used'},
            'social_insurance': {'status', 'actual_from'}, 'housing_fund': {'status', 'base'},
            'onboarding_docs': {'submitted'}, 'entitlements': {'granted'}}
    required = {'probation': {'probation_end'}, 'leave_balance': {'total', 'used'},
                'social_insurance': {'status'}, 'housing_fund': {'status'},
                'onboarding_docs': {'submitted'}, 'entitlements': {'granted'}}
    if set(value) - keys[field] or not required[field].issubset(value):
        raise ValueError('业务字段不完整或包含不支持的字段')
    result = dict(value)
    if field == 'probation':
        result['probation_end'] = date.fromisoformat(value['probation_end']).isoformat()
    if field in ('social_insurance', 'housing_fund'):
        if value['status'] not in ('paid', 'pending', 'not_started', 'unknown'):
            raise ValueError('状态必须从选项中选择')
        if value.get('actual_from'):
            result['actual_from'] = date.fromisoformat(value['actual_from']).isoformat()
        if value.get('base') is not None and (type(value['base']) is not int or not 0 <= value['base'] <= 10000000):
            raise ValueError('缴存基数须为非负整数')
    if field == 'leave_balance':
        for k in ('total', 'used'):
            if type(value[k]) not in (int, float) or not math.isfinite(value[k]) or not 0 <= value[k] <= 366:
                raise ValueError('假期额度及已休天数须在0至366之间')
        if value['used'] > value['total']:
            raise ValueError('已休天数不能超过核定额度')
        result['remaining'] = value['total'] - value['used']
    if field in ('onboarding_docs', 'entitlements'):
        name, catalog = ('submitted', 'doc_catalog') if field == 'onboarding_docs' else ('granted', 'entitlement_catalog')
        values = value[name]
        allowed = external.get_config(session, catalog) or {}
        if not isinstance(values, list) or len(values) > 300 or any(not isinstance(v, str) or v not in allowed for v in values):
            raise ValueError('清单项目必须来自已配置目录')
        result[name] = sorted(set(values))
    return result


def save_record(body, actor, source_kind='manual'):
    if source_kind == 'manual' and body.field in managed_fields():
        raise HTTPException(409, '该字段由企业数据接口管理，请在源系统修改或由运维明确变更数据归属')
    if body.field not in FIELDS or not body.source_ref.strip():
        raise HTTPException(400, '请选择业务字段并填写来源编号')
    if not body.revoked and not (body.as_of <= date.today() <= body.valid_until <= body.as_of + timedelta(days=90)):
        raise HTTPException(400, '快照日期不能在未来，有效期须覆盖今天且距快照日不超过90天')
    fingerprint = rel.digest([body.model_dump(mode='json'), actor, source_kind])
    with rel.named_lock('record:' + body.employee_id + ':' + body.field), db.get_session() as s:
        user = s.get(auth.User, body.employee_id)
        if user is None or not user.active:
            raise HTTPException(400, '员工账号不存在或未激活；数据同步不能创建账号或授予角色')
        old = latest(s, body.employee_id, body.field)
        if old and old.digest == fingerprint:
            return serialize(old)
        if (old.revision if old else 0) != body.expected_revision:
            raise HTTPException(409, '记录已更新，请重新读取后核对，不可覆盖他人更新')
        try:
            value = {} if body.revoked else validate_value(s, body.field, body.value)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise HTTPException(400, '字段值格式错误，请核对日期、状态、额度和目录项目') from None
        row = BusinessRecord(employee_id=body.employee_id, field=body.field,
            revision=body.expected_revision+1, value=value, as_of=body.as_of, valid_until=body.valid_until,
            source_ref=body.source_ref.strip(), actor=actor, source_kind=source_kind,
            revoked=int(body.revoked), digest=fingerprint)
        s.add(row)
        rel.audit(s, actor, 'business:revoked' if body.revoked else 'business:recorded', body.employee_id + '/' + body.field,
                  {'revision': row.revision})
        s.commit()
        return serialize(row)


def catalog_revision(s):
    return rel.digest({k: external.get_config(s, k) for k in external.DEFAULT_CONFIG})


def configure(body, p):
    if p.role != 'admin':
        raise HTTPException(403, '仅管理员可修改服务目录')
    key, value = body.get('key'), body.get('value')
    if key not in external.DEFAULT_CONFIG:
        raise HTTPException(400, '不支持的配置项')
    with rel.named_lock('enterprise:catalog'), db.get_session() as s:
        try:
            external.validate_config(s, key, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if body.get('expected_revision') != catalog_revision(s):
            raise HTTPException(409, '配置已变化或缺少版本凭证，请刷新后重试')
        if key == 'entitlement_catalog':
            removed = set(external.get_config(s, key) or {}) - set(value)
            roles = external.get_config(s, 'role_entitlements') or {}
            approvers = external.get_config(s, 'resource_approvers') or {}
            referenced = set(approvers) | {x for values in roles.values() for x in values}
            pending = s.query(db.Ticket).filter(db.Ticket.resource.in_(removed), db.Ticket.status.notin_(('closed', 'cancelled', 'rejected'))).first() if removed else None
            if removed & referenced or pending:
                raise HTTPException(409, '资源仍被岗位、审批配置或未结工单引用，请先处理引用，历史工单不删除')
        external.set_config(s, key, value)
        rel.audit(s, p.username, 'catalog:updated', key)
        s.commit()
        return {'ok': True, 'revision': catalog_revision(s)}


class DeliveryIn(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=32)
    action: str
    executor: str = Field(default='', max_length=64)
    reference: str = Field(default='', max_length=500)
    note: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=0)


def deliver(p, b):
    if b.action not in ('assign', 'complete', 'fail', 'accept', 'reopen') or not b.note.strip():
        raise HTTPException(400, '请选择操作并填写说明')
    with rel.named_lock('delivery:' + b.ticket_id), db.get_session() as s:
        t = s.query(db.Ticket).filter_by(ticket_id=b.ticket_id).with_for_update().first()
        if t is None:
            raise HTTPException(404, '工单不存在')
        d = s.get(Delivery, t.ticket_id)
        if b.action == 'assign':
            if p.role != 'admin' and p.username != t.approver:
                raise HTTPException(403, '只有指定审批人或管理员可分配执行人')
        elif b.action in ('complete', 'fail'):
            if not d or p.username != d.executor or p.username == t.employee_id:
                raise HTTPException(403, '只有指定执行人可登记交付，申请人不能执行自己的申请')
        elif p.username != t.employee_id:
            raise HTTPException(403, '只有申请人可验收或退回自己的交付')
        if (d.revision if d else 0) != b.expected_revision:
            raise HTTPException(409, '工单执行记录已变化，请刷新核对；不会重复执行')
        if b.action == 'assign':
            user = s.get(auth.User, b.executor)
            if t.status not in ('approved', 'delivery_failed', 'fulfilling') or not user or not user.active or user.role not in ('ops', 'admin') or user.username == t.employee_id:
                raise HTTPException(400, '仅已批准/执行中/失败工单可分配给有效的 IT 运营或管理员，且不能是申请人')
            if d is None:
                d = Delivery(ticket_id=t.ticket_id, executor=b.executor, assigned_by=p.username, revision=0)
                s.add(d)
            d.executor, d.assigned_by, t.status = b.executor, p.username, 'fulfilling'
        elif b.action in ('complete', 'fail'):
            if t.status != 'fulfilling' or (b.action == 'complete' and not b.reference.strip()):
                raise HTTPException(400, '只有执行中工单可登记结果；交付必须提供企业工单或执行凭证编号')
            t.status = 'delivered' if b.action == 'complete' else 'delivery_failed'
            d.reference = b.reference.strip()
        else:
            if t.status != 'delivered' or not d:
                raise HTTPException(409, '只有已交付工单可验收或退回')
            t.status = 'closed' if b.action == 'accept' else 'fulfilling'
        d.note, d.updated_at, d.revision = b.note.strip(), datetime.now(), d.revision+1
        rel.audit(s, p.username, 'delivery:' + b.action, t.ticket_id,
                  {'revision': d.revision, 'executor': d.executor, 'reference': d.reference, 'note': d.note})
        s.commit()
        return {'ok': True, 'status': t.status, 'revision': d.revision,
                'notice': '记录人工办理与验收结果；系统未自动开通企业权限，权限快照需另行同步'}


def router(current):
    r = APIRouter(prefix='/api/enterprise')
    def editor(p=Depends(current)):
        if p.role not in ('admin', 'hr'):
            raise HTTPException(403, '只有 HR 数据负责人或管理员可维护业务记录')
        return p

    @r.get('/catalog')
    def catalog(p=Depends(editor)):
        with db.get_session() as s:
            return {'config': {k: external.get_config(s, k) for k in external.DEFAULT_CONFIG},
                    'revision': catalog_revision(s), 'can_edit': p.role == 'admin',
                    'ingest_configured': len(os.getenv('RAMP_DATA_INGEST_TOKEN_SHA256', '')) == 64,
                    'ingest_fields': sorted(set(os.getenv('RAMP_DATA_INGEST_FIELDS', 'probation,leave_balance,social_insurance,housing_fund,onboarding_docs').split(',')))}

    @r.post('/catalog')
    def config_save(body: dict, p=Depends(editor)):
        return configure(body, p)

    @r.get('/records/{employee_id}')
    def records(employee_id: str, p=Depends(editor)):
        with db.get_session() as s:
            return [serialize(x) for f in sorted(FIELDS) if (x := latest(s, employee_id, f))]

    @r.post('/records')
    def record(b: RecordIn, p=Depends(editor)):
        if b.field == 'entitlements' and p.role != 'admin':
            raise HTTPException(403, '权限快照仅由管理员维护；HR 不得确认 IT 权限')
        return save_record(b, p.username)

    @r.get('/people')
    def people(p=Depends(editor)):
        with db.get_session() as s:
            return [{'username': u.username, 'name': u.display_name, 'role': u.role} for u in s.query(auth.User).filter_by(active=True).order_by(auth.User.username).all()]

    @r.get('/executors')
    def executors(p=Depends(current)):
        with db.get_session() as s:
            return [{'username': u.username, 'name': u.display_name} for u in s.query(auth.User).filter(auth.User.active.is_(True), auth.User.role.in_(('admin', 'ops'))).all()]

    @r.post('/delivery')
    def delivery(b: DeliveryIn, p=Depends(current)):
        return deliver(p, b)

    @r.post('/ingest')
    def ingest(b: RecordIn, request: Request):
        authorize_ingest(request, b.field)
        return save_record(b, 'integration:gateway', 'integration')

    @r.get('/ingest/revision')
    def ingest_revision(employee_id: str, field: str, request: Request):
        authorize_ingest(request, field)
        with db.get_session() as s:
            user = s.get(auth.User, employee_id)
            if not user or not user.active:
                raise HTTPException(404, '有效员工账号不存在')
            row = latest(s, employee_id, field)
            return {'revision': row.revision if row else 0}

    return r
