"""投顾会话、消息与轮次的 ORM 映射。

表结构以 ``migrations/003_advisor.sql`` 为事实源。本模块只做映射。
``checkpoint_*`` 是 PgSaver 的基础设施表，不在这里声明。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentSession(Base):
    """一次投顾对话。``id`` 同时作为 LangGraph ``thread_id``。"""

    __tablename__ = "agent_sessions"
    __table_args__ = {"schema": "finance"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("finance.users.customer_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentMessage(Base):
    """会话中的一条公开消息。同一 ``request_id`` 的同一角色只能有一行。"""

    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("uq_agent_messages_request_role", "request_id", "role", unique=True),
        {"schema": "finance"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    public_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTurn(Base):
    """一轮投顾请求。``request_id`` 是客户端幂等键。"""

    __tablename__ = "agent_turns"
    __table_args__ = (
        Index(
            "uq_agent_turns_one_active",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running', 'interrupted')"),
        ),
        {"schema": "finance"},
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("finance.agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    interrupt_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    final_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    retryable: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AgentMessage", "AgentSession", "AgentTurn"]
