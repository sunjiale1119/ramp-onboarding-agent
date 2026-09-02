"""持久化层：SQLAlchemy 模型 + MySQL 连接。

八张表，对应产品里的八种状态：
    employees      新人档案（域、mentor、入职日期）
    knowledge      知识条目（来源分级、有效期、域、向量）
    memories       三层记忆（情景 / 语义 / 程序）+ 可见性
    sessions       会话
    messages       消息
    traces         调用链留痕（耗时、档位、token、成本）
    escalations    升级卡片（未命中 → Mentor → 沉淀）
    push_log       主动推送记录（用来算打扰预算）

LangGraph 的 checkpoint 不在这里——它由 checkpointer 单独持有，
这是刻意的状态所有权划分：会话文本归 messages，暂停中的图归 checkpointer。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ 档案
class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    team: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(16), default="biz")
    mentor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mentor_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onboard_date: Mapped[date] = mapped_column(Date)

    def day_index(self, today: date | None = None) -> int:
        """今天是这位新人的第几天（Day 1 起算）。"""
        return ((today or date.today()) - self.onboard_date).days + 1


# ------------------------------------------------------------------ 知识
class Knowledge(Base):
    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(16), index=True)
    question: Mapped[str] = mapped_column(String(512))
    answer: Mapped[str] = mapped_column(Text)

    source_level: Mapped[str] = mapped_column(String(4), index=True)  # L1 / L2 / L3
    source_name: Mapped[str] = mapped_column(String(255))
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_knowledge_domain_level", "domain", "source_level"),)

    @property
    def is_stale(self) -> bool:
        return bool(self.expires_on and self.expires_on < date.today())

    def cite(self) -> str:
        bits = [self.source_name]
        if self.confirmed_by:
            bits.append(f"由 {self.confirmed_by} 确认")
        if self.effective_from:
            bits.append(f"生效 {self.effective_from:%Y-%m-%d}")
        if self.is_stale:
            bits.append("⚠ 已过期")
        return " · ".join(bits)


# ------------------------------------------------------------------ 记忆
class Memory(Base):
    """三层记忆共用一张表，用 kind 区分——因为它们的读写路径一致，
    差异只在保留期和可见性，没必要拆三张表。"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[str] = mapped_column(String(32), index=True)  # 属于哪位新人
    kind: Mapped[str] = mapped_column(String(16), index=True)  # episodic/semantic/procedural
    topic: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)

    # 可见性矩阵落到字段上：提问原文只有本人可见，主题聚合才给 mentor
    visible_to_self: Mapped[bool] = mapped_column(Boolean, default=True)
    visible_to_mentor: Mapped[bool] = mapped_column(Boolean, default=False)
    visible_to_hr: Mapped[bool] = mapped_column(Boolean, default=False)

    weight: Mapped[float] = mapped_column(Float, default=1.0)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ 会话
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    turns: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant / system
    content: Mapped[str] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 走了哪条路径
    domain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ 观测
class Trace(Base):
    """一次会话的一个 span。运营排查时按 session_id 拉出整条链。"""

    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    turn: Mapped[int] = mapped_column(Integer, default=0)
    span: Mapped[str] = mapped_column(String(64))  # guardrail / classify / retrieve / tool:x ...
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ 飞轮
class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    employee_id: Mapped[str] = mapped_column(String(32), index=True)
    mentor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    domain: Mapped[str] = mapped_column(String(16))
    question: Mapped[str] = mapped_column(String(512))
    best_score: Mapped[float] = mapped_column(Float, default=0.0)
    hints: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="open")  # open/answered/sunk
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    knowledge_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PushLog(Base):
    __tablename__ = "push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String(32), index=True)
    day_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ 引擎
_engine = None
_Session = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(config.mysql_url(), pool_pre_ping=True, future=True)
    return _engine


def session_factory():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session


def get_session():
    return session_factory()()


def create_database() -> None:
    """建库（如不存在），然后建表。"""
    from sqlalchemy import text

    boot = create_engine(config.mysql_url(with_db=False), future=True)
    with boot.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{config.MYSQL_DB}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        conn.commit()
    boot.dispose()
    from . import auth  # noqa: F401 —— 导入即注册 users / auth_sessions 到 metadata
    Base.metadata.create_all(engine())


def reset_database() -> None:
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


def ping() -> tuple[bool, str]:
    """连通性自检，给 CLI 用。"""
    from sqlalchemy import text

    try:
        with engine().connect() as conn:
            v = conn.execute(text("SELECT VERSION()")).scalar()
        return True, f"MySQL {v}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


__all__ = [
    "Base", "Employee", "Knowledge", "Memory", "Session", "Message",
    "Trace", "Escalation", "PushLog",
    "engine", "get_session", "create_database", "reset_database", "ping",
]
