"""Durable request receipts and per-user execution locks for the small-team pilot.

No blind retry of uncertain model runs. Confirm retries are safe only because the
ticket adapter commits its receipt in the same transaction as the ticket itself.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading

from fastapi import HTTPException
from sqlalchemy import String, Integer, DateTime, JSON, text
from sqlalchemy.orm import Mapped, mapped_column

from . import db


class RequestReceipt(db.Base):
    __tablename__ = "request_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running")
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TicketReceipt(db.Base):
    __tablename__ = "ticket_receipts"
    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    ticket_id: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict] = mapped_column(JSON)


class AuditEvent(db.Base):
    __tablename__ = "operation_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(160))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def migrate(engine=None):
    engine = engine or db.engine()
    for cls in (RequestReceipt, TicketReceipt, AuditEvent):
        cls.__table__.create(engine, checkfirst=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def audit(session, actor, action, target, detail=None):
    session.add(AuditEvent(actor=actor, action=action, target=str(target)[:160], detail=detail or {}))


_locks = [threading.RLock() for _ in range(128)]
_capacity = threading.BoundedSemaphore(int(os.getenv("RAMP_MAX_CONCURRENT", "3")))


@contextmanager
def named_lock(key):
    name = "ramp:" + hashlib.sha256(key.encode()).hexdigest()[:48]
    engine = db.engine()
    if engine.dialect.name in ("mysql", "mariadb"):
        # Dedicated connection holds the advisory lock across business commits.
        # MySQL releases it when a crashed worker disconnects.
        with engine.connect() as conn:
            if conn.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar() != 1:
                raise HTTPException(409, "同一操作正在处理，请稍后查询结果，不要重复提交")
            try:
                yield
            finally:
                conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
    else:
        lock = _locks[int(name[-8:], 16) % len(_locks)]
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "操作正在处理中")
        try:
            yield
        finally:
            lock.release()


def require_session(owner, sid):
    with db.get_session() as session:
        row = session.get(db.Session, sid)
        if row is None or row.employee_id != owner:
            raise HTTPException(403, "会话不存在或不属于当前账号")


def run_once(owner, key, kind, payload, fn):
    rid = digest([owner, key])
    fingerprint = digest([kind, payload])
    with named_lock("user:" + owner):
        with db.get_session() as session:
            receipt = session.get(RequestReceipt, rid)
            if receipt:
                if receipt.digest != fingerprint:
                    raise HTTPException(409, "请求编号已用于不同内容，请勿复用")
                if receipt.status == "succeeded":
                    return receipt.response
                if kind != "confirm":
                    receipt.status = "uncertain"
                    session.commit()
                    raise HTTPException(409, "上次请求结果不确定。请查操作记录；不要自动重试付费问答，可新开会话")
            sid = payload.get("session_id") or ("sess_" + rid[:32])
            if payload.get("session_id"):
                require_session(owner, sid)
            if not receipt:
                since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                n = session.query(RequestReceipt).filter(RequestReceipt.owner == owner,
                    RequestReceipt.kind == "ask", RequestReceipt.created_at >= since).count()
                if kind == "ask" and n >= int(os.getenv("RAMP_DAILY_ASK_LIMIT", "50")):
                    raise HTTPException(429, "已达到今天的试用问答额度，明天再试或联系管理员")
            if not _capacity.acquire(blocking=False):
                raise HTTPException(429, "系统正忙，请稍后重试同一个请求")
            try:
                if not receipt:
                    if not payload.get("session_id") and session.get(db.Session, sid) is None:
                        session.add(db.Session(id=sid, employee_id=owner))
                    receipt = RequestReceipt(id=rid, owner=owner, request_id=key, session_id=sid,
                        kind=kind, digest=fingerprint, status="running")
                    session.add(receipt)
                    session.commit()
                try:
                    result = fn(sid)
                    receipt.status = "uncertain" if (result.get("action_result") or {}).get("status") == "uncertain" else "succeeded"
                    receipt.response = result
                    receipt.updated_at = datetime.now()
                    audit(session, owner, kind + (":uncertain" if receipt.status == "uncertain" else ":completed"), sid, {"request_id": key})
                    session.commit()
                    return result
                except Exception:
                    session.rollback()
                    receipt = session.get(RequestReceipt, rid)
                    receipt.status = "uncertain"
                    receipt.updated_at = datetime.now()
                    audit(session, owner, kind + ":uncertain", sid, {"request_id": key})
                    session.commit()
                    raise
            finally:
                _capacity.release()


def requests(owner):
    with db.get_session() as session:
        rows = session.query(RequestReceipt).filter_by(owner=owner).order_by(RequestReceipt.created_at.desc()).limit(30).all()
        return [{"request_id": r.request_id, "session_id": r.session_id, "kind": r.kind,
                 "status": r.status, "at": r.created_at.isoformat(),
                 "result": r.response if r.status == "succeeded" else None} for r in rows]
