"""Admin-only knowledge lifecycle endpoints, mounted by api.py."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from . import db, embeddings, knowledge, knowledge_versions as kv


def router(require):
    routes = APIRouter(prefix="/api/admin/knowledge", dependencies=[Depends(require("admin"))])

    def operation(fn, body, p, **kwargs):
        with db.get_session() as session:
            try:
                return fn(session, body, p.username, **kwargs)
            except kv.ConflictError as exc:
                session.rollback()
                raise HTTPException(409, str(exc)) from None
            except kv.ValidationError as exc:
                session.rollback()
                raise HTTPException(400, str(exc)) from None

    @routes.get("")
    def listing():
        with db.get_session() as session:
            rows = session.query(kv.Version).order_by(kv.Version.id.desc()).all()
            items = [kv.serialize(session, v) for v in rows]
            return {"items": items, "total": len(items), "generation": session.get(kv.Catalog, 1).generation}

    @routes.post("/save")
    def save(body: dict, p=Depends(require("admin"))):
        item = operation(kv.save_draft, body, p)
        return {"ok": True, "item": item, "message": "草稿已保存，当前回答不受影响"}

    @routes.get("/review/{version_id}")
    def review(version_id: int):
        with db.get_session() as session:
            try:
                # Lock also gives preview a consistent generation/snapshot.
                kv.lock(session)
                return kv.review(session, kv.get_version(session, version_id))
            except kv.ValidationError as exc:
                raise HTTPException(400, str(exc)) from None

    @routes.post("/publish")
    def publish(body: dict, p=Depends(require("admin"))):
        try:
            item = operation(kv.publish, body, p, encode=embeddings.encode_one)
        except HTTPException:
            raise
        except Exception:
            # Atomic publishing keeps the draft and previous generation on failure.
            import logging
            logging.getLogger(__name__).exception("知识发布失败")
            raise HTTPException(503, "发布失败，原版本仍然可用。请检查向量服务后重试。") from None
        return {"ok": True, "item": item, "message": "已发布；系统按生效日期使用此版本"}

    @routes.post("/delete")
    def discard(body: dict, p=Depends(require("admin"))):
        operation(kv.discard, body, p)
        return {"ok": True, "message": "已撤回，历史记录保留"}

    @routes.get("/history/{version_id}")
    def history(version_id: int):
        with db.get_session() as session:
            try:
                v = kv.get_version(session, version_id)
            except kv.ValidationError as exc:
                raise HTTPException(404, str(exc)) from None
            versions = session.query(kv.Version).filter_by(policy_id=v.policy_id).order_by(kv.Version.number.desc()).all()
            audit = session.query(kv.Audit).filter(kv.Audit.version_id.in_([x.id for x in versions])).order_by(kv.Audit.id.desc()).all()
            return {"items": [kv.serialize(session, x) for x in versions], "audit": [
                {"actor": a.actor, "action": a.action, "version_id": a.version_id,
                 "detail": a.detail, "at": a.created_at.isoformat()} for a in audit]}

    @routes.get("/preview")
    def preview(query: str, as_of: date | None = None, scope: str | None = None):
        if not query.strip() or len(query) > 2000:
            raise HTTPException(400, "请输入 1–2000 字的问题")
        r = knowledge.search(query, as_of=as_of, scope=scope, top_k=4)
        return {"as_of": str(as_of or date.today()), "confident": r.confident,
                "hits": [h.to_dict() for h in r.hits], "notice": "预览只检索已发布且当日有效的版本，不调用对话模型。"}

    return routes
