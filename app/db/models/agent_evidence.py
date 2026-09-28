"""Stock Agent 短期工具证据 ORM 映射。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentToolEvidence(Base):
    __tablename__ = "agent_tool_evidence"
    __table_args__ = (
        Index("idx_agent_tool_evidence_request", "request_id"),
        Index("idx_agent_tool_evidence_expires", "expires_at"),
        {"schema": "finance"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("finance.agent_turns.request_id", ondelete="CASCADE"), nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("finance.agent_sessions.id", ondelete="CASCADE"), nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("finance.users.customer_id", ondelete="CASCADE"), nullable=False,
    )
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sanitized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    as_of: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AgentToolEvidence"]
