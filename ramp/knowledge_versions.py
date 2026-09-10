"""Versioned knowledge publishing. All writes serialize on one database row.

Existing knowledge rows become immutable published v1 snapshots on migration.
Each later version owns a separate Knowledge row; only drafts may be edited.
Dates are inclusive. A later publication supersedes an earlier version from its
effective date, even if the later version subsequently expires (no resurrection).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import ForeignKey, Integer, String, Text, DateTime, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from . import db


class Catalog(db.Base):
    __tablename__ = "kb_catalog"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class Version(db.Base):
    __tablename__ = "kb_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(40), index=True)
    number: Mapped[int] = mapped_column(Integer)
    knowledge_id: Mapped[int] = mapped_column(ForeignKey("knowledge.id"), unique=True)
    base_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    topic: Mapped[str] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(String(64), default="*")
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("policy_id", "number"),)


class Audit(db.Base):
    __tablename__ = "kb_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ValidationError(ValueError):
    pass


class ConflictError(ValidationError):
    pass


def normalize(value):
    return re.sub(r"[\W_]+", "", value or "").lower()


def lock(session):
    return session.execute(select(Catalog).where(Catalog.id == 1).with_for_update()).scalar_one()


def log(session, v, actor, action, detail):
    session.add(Audit(version_id=v.id, actor=actor, action=action,
                      detail=json.dumps(detail, ensure_ascii=False, default=str)))


def migrate(engine=None):
    """Idempotent, additive migration. Never deletes or rewrites old knowledge."""
    engine = engine or db.engine()
    for table in (Catalog.__table__, Version.__table__, Audit.__table__):
        table.create(engine, checkfirst=True)
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import IntegrityError
    with Session(engine) as session:
        try:
            if session.get(Catalog, 1) is None:
                session.add(Catalog(id=1, generation=0))
                session.commit()
        except IntegrityError:
            session.rollback()
        catalog = lock(session)
        known = select(Version.knowledge_id)
        rows = session.query(db.Knowledge).filter(~db.Knowledge.id.in_(known)).all()
        for row in rows:
            v = Version(policy_id=uuid.uuid4().hex, number=1, knowledge_id=row.id,
                        status="published", revision=1, topic=row.question[:255], scope="*",
                        created_by="legacy-migration", reviewed_by=None,
                        published_at=datetime.utcnow(), change_note="迁移已有知识，保留原始内容与日期；不代表重新审核")
            session.add(v)
            session.flush()
            log(session, v, "system", "migrated", {"knowledge_id": row.id})
        if rows:
            catalog.generation += 1
        session.commit()
        return len(rows)


FIELDS = ("domain", "question", "answer", "source_level", "source_name", "effective_from", "expires_on")


def snapshot(session, v):
    k = session.get(db.Knowledge, v.knowledge_id)
    result = {f: getattr(k, f) for f in FIELDS}
    return {**result, "topic": v.topic, "scope": v.scope}


def serialize(session, v, today=None):
    today = today or date.today()
    k = session.get(db.Knowledge, v.knowledge_id)
    data = snapshot(session, v)
    state = v.status
    if state == "published":
        all_versions = session.query(Version).filter_by(policy_id=v.policy_id, status="published").all()
        later = [x for x in all_versions if x.number > v.number
                 and (session.get(db.Knowledge, x.knowledge_id).effective_from or date.min) <= today]
        state = ("scheduled" if k.effective_from and k.effective_from > today else
                 "superseded" if later else "expired" if k.expires_on and k.expires_on < today else "active")
    return {**{f: x.isoformat() if isinstance(x, date) else x for f, x in data.items()},
            "id": v.id, "knowledge_id": k.id, "policy_id": v.policy_id, "number": v.number,
            "base_id": v.base_id, "revision": v.revision, "status": v.status, "state": state,
            "created_by": v.created_by, "reviewed_by": v.reviewed_by,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "change_note": v.change_note, "citation": citation(k, v, today)}


def citation(k, v, as_of=None):
    return f"{k.cite(as_of=as_of)} · 条款 {v.policy_id[:8]} / v{v.number} · 适用：{'全公司' if v.scope == '*' else v.scope}"


def get_version(session, vid):
    try:
        v = session.get(Version, int(vid))
    except (ValueError, TypeError):
        v = None
    if v is None:
        raise ValidationError("版本不存在，请刷新列表")
    return v


def checked_revision(v, body):
    if body.get("revision") != v.revision:
        raise ConflictError("该版本已被其他操作修改，请刷新后重新检查")


def latest_published(session, policy_id):
    return session.query(Version).filter_by(policy_id=policy_id, status="published").order_by(Version.number.desc()).first()


def parse_fields(body, previous=None):
    data = dict(previous or {})
    for f in (*FIELDS, "scope", "topic"):
        if f in body:
            data[f] = body[f]
    for f, limit in (("question", 512), ("answer", 20000), ("source_name", 255), ("topic", 255), ("scope", 64)):
        data[f] = str(data.get(f) or ("*" if f == "scope" else "")).strip()
        if not data[f] or len(data[f]) > limit:
            raise ValidationError(f"{f} 必须填写且长度不超过 {limit}")
    if not normalize(data["topic"]):
        raise ValidationError("规则主题不能只包含标点")
    if data.get("domain") not in ("hr", "it", "biz") or data.get("source_level") not in ("L1", "L2", "L3"):
        raise ValidationError("请选择有效的领域和来源等级")
    for f in ("effective_from", "expires_on"):
        try:
            value = data.get(f)
            data[f] = date.fromisoformat(value) if isinstance(value, str) and value else value or None
            if data[f] is not None and type(data[f]) is not date:
                raise ValueError("invalid date type")
        except (ValueError, TypeError):
            raise ValidationError("日期须使用 YYYY-MM-DD") from None
    if not data["effective_from"]:
        raise ValidationError("请填写生效日期")
    if data["expires_on"] and data["expires_on"] < data["effective_from"]:
        raise ValidationError("失效日期不得早于生效日期")
    return data


def save_draft(session, body, actor):
    lock(session)
    previous = None
    v = None
    base = get_version(session, body["id"]) if body.get("id") else None
    if base:
        checked_revision(base, body)
        previous = snapshot(session, base)
        if base.status == "draft":
            v = base
        elif base.status != "published":
            raise ValidationError("已撤回草稿不可编辑")
    data = parse_fields(body, previous)
    if base and (data["topic"] != base.topic or data["scope"] != base.scope
                 or data["domain"] != previous["domain"]):
        raise ValidationError("同一条款的主题、领域和适用范围不能更改；请新建条款")
    note = str(body.get("change_note") or "").strip()
    if not note or len(note) > 2000:
        raise ValidationError("请填写变更说明（最多 2000 字）")
    if v is None:
        policy_id = base.policy_id if base else uuid.uuid4().hex
        latest = latest_published(session, policy_id)
        n = session.query(Version).filter_by(policy_id=policy_id).count() + 1
        k = db.Knowledge(**{f: data[f] for f in FIELDS}, confirmed_by=None, embedding=None)
        session.add(k)
        session.flush()
        v = Version(policy_id=policy_id, number=n, knowledge_id=k.id,
                    base_id=latest.id if latest else None, status="draft", revision=1,
                    topic=data["topic"], scope=data["scope"], created_by=actor, change_note=note)
        session.add(v)
        session.flush()
        action = "restored_as_draft" if base and latest and base.id != latest.id else "draft_created"
    else:
        k = session.get(db.Knowledge, v.knowledge_id)
        for f in FIELDS:
            setattr(k, f, data[f])
        v.change_note = note
        v.revision += 1
        k.embedding = None
        action = "draft_updated"
    log(session, v, actor, action, {"snapshot": snapshot(session, v), "from_version": base.id if base else None})
    session.commit()
    return serialize(session, v)


def overlaps(a, b):
    return max(a[0], b[0]) <= min(a[1], b[1])


def published_windows(session):
    groups = {}
    for v in session.query(Version).filter_by(status="published").all():
        groups.setdefault(v.policy_id, []).append(v)
    result = []
    for versions in groups.values():
        versions.sort(key=lambda x: x.number)
        for i, v in enumerate(versions):
            k = session.get(db.Knowledge, v.knowledge_id)
            start, end = k.effective_from or date.min, k.expires_on or date.max
            if i + 1 < len(versions):
                next_start = session.get(db.Knowledge, versions[i+1].knowledge_id).effective_from or date.min
                end = min(end, next_start - timedelta(days=1) if next_start > date.min else date.min)
                if next_start <= start:
                    continue
            if start <= end:
                result.append((v, k, (start, end)))
    return result


def review(session, v):
    k = session.get(db.Knowledge, v.knowledge_id)
    latest = latest_published(session, v.policy_id)
    blockers, candidates = [], []
    if v.status != "draft":
        blockers.append("只有草稿可以发布")
    if (latest.id if latest else None) != v.base_id:
        blockers.append("已有其他新版发布；请基于最新版本重新创建草稿")
    if k.effective_from is None or k.effective_from < date.today():
        blockers.append("新发布版本的生效日期不得早于今天，历史版本请保留迁移记录")
    if latest and k.effective_from and k.effective_from < (session.get(db.Knowledge, latest.knowledge_id).effective_from or date.min):
        blockers.append("生效日期不能早于同一条款已发布的最新版本")
    window = (k.effective_from or date.min, k.expires_on or date.max)
    for other, row, other_window in published_windows(session):
        if other.policy_id == v.policy_id or row.domain != k.domain:
            continue
        if v.scope != "*" and other.scope != "*" and v.scope != other.scope:
            continue
        if not overlaps(window, other_window):
            continue
        same_topic = normalize(other.topic) == normalize(v.topic)
        similarity = difflib.SequenceMatcher(None, normalize(k.question), normalize(row.question)).ratio()
        if same_topic or similarity >= 0.45:
            candidate = {"id": other.id, "policy_id": other.policy_id, "number": other.number,
                         "question": row.question, "answer": row.answer, "scope": other.scope,
                         "source_name": row.source_name, "same_topic": same_topic,
                         "effective_from": other_window[0].isoformat(), "expires_on": other_window[1].isoformat()}
            candidates.append(candidate)
            if same_topic:
                blockers.append(f"规则主题与条款 {other.policy_id[:8]} v{other.number} 的适用时间及范围重叠；请改为该条款的新版本，或先明确主题和适用范围")
    before = snapshot(session, latest) if latest else {}
    after = snapshot(session, v)
    differences = [{"field": f, "before": str(before.get(f) or ""), "after": str(after.get(f) or "")}
                   for f in after if before.get(f) != after.get(f)]
    catalog = session.get(Catalog, 1)
    token = hashlib.sha256(json.dumps({"generation": catalog.generation, "id": v.id,
                           "revision": v.revision, "date": str(date.today()), "snapshot": after,
                           "candidates": candidates}, sort_keys=True, default=str).encode()).hexdigest()
    return {"version": serialize(session, v), "differences": differences,
            "blockers": blockers, "candidates": candidates, "review_token": token,
            "notice": "相似条款仅为审核线索，自动检测不能覆盖所有语义冲突；发布人须核对原制度。"}


def publish(session, body, actor, encode):
    catalog = lock(session)
    v = get_version(session, body.get("id"))
    checked_revision(v, body)
    check = review(session, v)
    if check["blockers"]:
        raise ConflictError("；".join(check["blockers"]))
    if body.get("review_token") != check["review_token"]:
        raise ConflictError("审核预览已过期，请重新检查差异与冲突")
    note = str(body.get("review_note") or "").strip()
    if body.get("reviewed") is not True or not note or len(note) > 2000:
        raise ValidationError("请确认已核对原制度，并填写审核意见（最多 2000 字）")
    if set(body.get("acknowledged") or []) != {x["id"] for x in check["candidates"]}:
        raise ConflictError("请逐条核对所有相似条款，并说明不存在冲突的理由")
    k = session.get(db.Knowledge, v.knowledge_id)
    # Failure here rolls back status, audit and generation together.
    k.embedding = encode(f"{k.question} {k.answer}")
    k.confirmed_by = actor
    v.status, v.reviewed_by = "published", actor
    v.published_at = datetime.utcnow()
    v.revision += 1
    catalog.generation += 1
    session.query(db.Escalation).filter_by(knowledge_id=k.id, status="review_pending").update({"status": "sunk"})
    log(session, v, actor, "published", {"review_note": note, "acknowledged": body.get("acknowledged"),
                                        "snapshot": snapshot(session, v), "generation": catalog.generation})
    session.commit()
    return serialize(session, v)


def discard(session, body, actor):
    catalog = lock(session)
    v = get_version(session, body.get("id"))
    checked_revision(v, body)
    k = session.get(db.Knowledge, v.knowledge_id)
    if v.status == "published" and k.effective_from and k.effective_from > date.today():
        if latest_published(session, v.policy_id).id != v.id:
            raise ConflictError("请从最新的待生效版本开始撤回，避免版本顺序被破坏")
        v.status = "withdrawn"
        catalog.generation += 1
        action = "scheduled_withdrawn"
    elif v.status == "draft":
        v.status = "discarded"
        action = "draft_discarded"
    else:
        raise ValidationError("已发布版本不可删除；请从历史版本创建恢复草稿并审核发布")
    v.revision += 1
    session.query(db.Escalation).filter_by(knowledge_id=k.id, status="review_pending").update({"status": "answered"})
    log(session, v, actor, action, {})
    session.commit()


def active_metadata(session, as_of=None, scope=None):
    """Select versions BEFORE retrieval, never allow expiry to revive an older one."""
    as_of = as_of or date.today()
    result = {}
    for v, k, (start, end) in published_windows(session):
        if start <= as_of <= end and (v.scope == "*" or v.scope == scope):
            result[k.id] = {"version_id": v.id, "policy_id": v.policy_id, "number": v.number,
                            "citation": citation(k, v, as_of), "scope": v.scope}
    return result


def catalog_generation():
    with db.get_session() as session:
        row = session.get(Catalog, 1)
        if row is None:
            raise RuntimeError("知识版本表尚未初始化，请先运行 python -m ramp.migrate_knowledge")
        return row.generation
