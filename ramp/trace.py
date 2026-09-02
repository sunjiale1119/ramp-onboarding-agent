"""调用链留痕。

存在的理由只有一个：**一次用户投诉进来，运营要能在 3 分钟内判断
问题出在检索、在工具、还是在模型。** 分不清这三者的产品，出了事
只能整体调 prompt 碰运气。

所以每个 span 必须带够三样东西：耗时、档位与 token、成败与原因。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import db


def _uid() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


@dataclass
class Span:
    name: str
    session_id: str
    turn: int = 0
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    duration_ms: int = 0
    ok: bool = True
    uid: str = field(default_factory=_uid)
    """去重用。子图会继承父图 state 再整体回传，spans 的 add 累加器
    会把父图那几条又加一遍——没有稳定 id 就没法把它们认出来。"""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "span": self.name,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost": round(self.cost, 6),
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "detail": self.detail,
        }


@contextmanager
def span(name: str, session_id: str, turn: int = 0, **detail: Any) -> Iterator[Span]:
    """用法：

        with trace.span("retrieve", sid) as sp:
            r = knowledge.search(q)
            sp.detail["best_score"] = r.best_score
    """
    sp = Span(name=name, session_id=session_id, turn=turn, detail=dict(detail))
    t0 = time.perf_counter()
    try:
        yield sp
    except Exception as exc:  # noqa: BLE001
        sp.ok = False
        sp.detail["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        sp.duration_ms = int((time.perf_counter() - t0) * 1000)


def record_llm(sp: Span, result: Any) -> Span:
    """把一次 LLM 调用的用量并进 span。"""
    sp.model = result.model
    sp.tokens_in = result.tokens_in
    sp.tokens_out = result.tokens_out
    sp.cost = result.cost
    sp.detail["cache_hit_tokens"] = getattr(result, "cache_hit_tokens", 0)
    if result.tokens_in:
        sp.detail["cache_hit_ratio"] = round(
            getattr(result, "cache_hit_tokens", 0) / result.tokens_in, 3
        )
    return sp


def persist(spans: list[dict[str, Any]], session_id: str, turn: int = 0) -> int:
    """把一轮的所有 span 落库。失败不影响主流程——
    可观测性不能反过来把产品拖垮。"""
    if not spans:
        return 0
    session = db.get_session()
    try:
        # 幂等：HITL 那一轮会写两次——ask() 停在确认点时写一次父图的 span，
        # resume() 拿到合并后的全量再写一次，两边有重叠。按 uid 跳过已写的。
        existing: set[str] = set()
        for (d,) in session.query(db.Trace.detail).filter(
            db.Trace.session_id == session_id, db.Trace.detail.isnot(None)
        ):
            if isinstance(d, dict) and d.get("_uid"):
                existing.add(d["_uid"])

        written = 0
        for s in spans:
            uid = s.get("uid")
            if uid and uid in existing:
                continue
            if uid:
                existing.add(uid)
            session.add(
                db.Trace(
                    session_id=session_id,
                    turn=turn,
                    span=s.get("span", "?"),
                    model=s.get("model"),
                    tokens_in=s.get("tokens_in", 0),
                    tokens_out=s.get("tokens_out", 0),
                    cost=s.get("cost", 0.0),
                    duration_ms=s.get("duration_ms", 0),
                    ok=s.get("ok", True),
                    detail={**(s.get("detail") or {}), "_uid": s.get("uid")},
                )
            )
            written += 1
        session.commit()
        return written
    except Exception:  # noqa: BLE001
        session.rollback()
        return 0
    finally:
        session.close()


def waterfall(session_id: str) -> list[dict[str, Any]]:
    """按时间还原一条会话的调用链，给运营控制台用。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.Trace)
            .filter_by(session_id=session_id)
            .order_by(db.Trace.turn, db.Trace.id)
            .all()
        )
        return [
            {
                "turn": r.turn,
                "span": r.span,
                "model": r.model,
                "tokens": (r.tokens_in, r.tokens_out),
                "cost": r.cost,
                "ms": r.duration_ms,
                "ok": r.ok,
                "detail": r.detail or {},
            }
            for r in rows
        ]
    finally:
        session.close()


def session_cost(session_id: str) -> dict[str, Any]:
    from sqlalchemy import func as F

    session = db.get_session()
    try:
        row = (
            session.query(
                F.coalesce(F.sum(db.Trace.cost), 0.0),
                F.coalesce(F.sum(db.Trace.tokens_in), 0),
                F.coalesce(F.sum(db.Trace.tokens_out), 0),
                F.coalesce(F.sum(db.Trace.duration_ms), 0),
            )
            .filter(db.Trace.session_id == session_id)
            .one()
        )
        return {"cost": float(row[0]), "tokens_in": int(row[1]),
                "tokens_out": int(row[2]), "duration_ms": int(row[3])}
    finally:
        session.close()
