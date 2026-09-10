from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from . import db, pilot, ticketing, reliability as rel


class TicketDecision(BaseModel):
    ticket_id: str = Field(max_length=32)
    action: Literal["cancel", "approve", "reject"]
    note: str = Field(min_length=1, max_length=1000)


class FeedbackIn(BaseModel):
    category: Literal["知识问答", "个人信息", "权限申请", "人工转交", "其他"]
    rating: int = Field(ge=1, le=5)
    content: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    id: int
    status: Literal["open", "resolved"]
    resolution: str = Field(min_length=1, max_length=2000)


def router(current, require):
    r = APIRouter(prefix="/api")

    @r.get("/tickets")
    def tickets(p=Depends(current)):
        return ticketing.list_for(p)

    @r.post("/tickets/decision")
    def decide(b: TicketDecision, p=Depends(current)):
        return ticketing.decide(p, b.ticket_id, b.action, b.note)

    @r.get("/newbie/requests")
    def requests(p=Depends(require("newbie"))):
        return rel.requests(p.username)

    @r.get("/pilot/feedback")
    def feedback(p=Depends(current)):
        return pilot.feedback_items(p)

    @r.post("/pilot/feedback")
    def submit(b: FeedbackIn, p=Depends(current)):
        if not b.content.strip():
            raise HTTPException(400, "请填写反馈内容")
        with db.get_session() as s:
            f = pilot.Feedback(owner=p.username, category=b.category, rating=b.rating, content=b.content.strip())
            s.add(f)
            s.flush()
            rel.audit(s, p.username, "feedback:submitted", f.id)
            s.commit()
            return {"ok": True, "id": f.id}

    @r.post("/pilot/feedback/resolve")
    def resolve(b: ResolveIn, p=Depends(require("ops"))):
        with db.get_session() as s:
            f = s.query(pilot.Feedback).filter_by(id=b.id).with_for_update().first()
            if f is None:
                raise HTTPException(404, "反馈不存在")
            f.status, f.resolution = b.status, b.resolution
            rel.audit(s, p.username, "feedback:" + b.status, f.id)
            s.commit()
            return {"ok": True}

    @r.get("/ops/readiness")
    def readiness(p=Depends(require("ops"))):
        return pilot.dashboard()

    @r.get("/ops/audit")
    def audit(p=Depends(require("ops"))):
        with db.get_session() as s:
            return [{"actor": a.actor, "action": a.action, "target": a.target,
                     "at": a.created_at.isoformat()} for a in
                    s.query(rel.AuditEvent).order_by(rel.AuditEvent.id.desc()).limit(100)]
    return r
