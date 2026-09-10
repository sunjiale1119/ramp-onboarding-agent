"""Small-team pilot feedback and operational readiness; all numbers are measured."""
from datetime import datetime, timedelta
import os

from sqlalchemy import Integer, String, Text, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column

from . import db, reliability as rel


class Feedback(db.Base):
    __tablename__ = "pilot_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(32))
    rating: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def migrate(engine=None):
    Feedback.__table__.create(engine or db.engine(), checkfirst=True)


def ready():
    try:
        with db.get_session() as s:
            s.execute(text("SELECT 1"))
            from .knowledge_versions import Catalog
            versioned = s.get(Catalog, 1) is not None
            # Missing migrations are a readiness failure, not a fake healthy status.
            s.query(rel.RequestReceipt.id).limit(1).all()
            s.query(Feedback.id).limit(1).all()
        return versioned
    except Exception:
        return False


def dashboard():
    since = datetime.now() - timedelta(days=7)
    with db.get_session() as s:
        requests = s.query(rel.RequestReceipt).filter(rel.RequestReceipt.created_at >= since).all()
        finished = [r for r in requests if r.status == "succeeded"]
        feedback = s.query(Feedback).all()
        cost = s.query(func.coalesce(func.sum(db.Trace.cost), 0)).scalar()
        return {"release": os.getenv("RAMP_RELEASE", "local"), "ready": ready(),
            "window": "近7天", "requests": len(requests), "completed": len(finished),
            "uncertain": sum(r.status == "uncertain" for r in requests),
            "running": sum(r.status == "running" for r in requests),
            "participants": len({r.owner for r in requests}),
            "completion_rate": len(finished)/len(requests) if requests else None,
            "feedback_total": len(feedback), "feedback_open": sum(r.status == "open" for r in feedback),
            "total_recorded_cost": float(cost),
            "model_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
            "embedding_configured": bool(os.getenv("DASHSCOPE_API_KEY")),
            "notice": "完成率代表请求处理完成，不代表答案正确。未执行模型健康请求；无数据时不计算通过率。"}


def feedback_items(p):
    with db.get_session() as s:
        q = s.query(Feedback)
        if not p.can_view("ops"):
            q = q.filter_by(owner=p.username)
        return [{"id": x.id, "owner": x.owner, "category": x.category, "rating": x.rating,
                 "content": x.content, "status": x.status, "resolution": x.resolution,
                 "at": x.created_at.isoformat()} for x in q.order_by(Feedback.id.desc()).limit(100)]
