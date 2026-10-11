"""Stock Agent 短期证据的 PostgreSQL 仓库。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from app.agents.stock.evidence_store import (
    EVIDENCE_TTL,
    EvidenceAccessError,
    EvidenceContext,
    input_hash,
    sanitize_evidence_payload,
)
from app.db.models.agent_evidence import AgentToolEvidence
from app.db.session import ensure_schema, session_scope


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _row(item: AgentToolEvidence) -> dict[str, Any]:
    return {
        "id": str(item.id), "request_id": str(item.request_id), "session_id": str(item.session_id),
        "customer_id": item.customer_id, "tool_call_id": item.tool_call_id, "tool_name": item.tool_name,
        "task_id": item.task_id,
        "input_hash": item.input_hash, "evidence_type": item.evidence_type,
        "payload": item.sanitized_payload, "source": item.source, "as_of": item.as_of,
        "created_at": item.created_at.isoformat(), "expires_at": item.expires_at.isoformat(),
    }


class PostgresAgentEvidenceStore:
    def put(
        self,
        context: EvidenceContext,
        *,
        tool_call_id: str,
        tool_name: str,
        evidence_type: str,
        payload: Any,
        source: str = "",
        as_of: str = "",
        arguments: Any = None,
    ) -> dict[str, Any]:
        ensure_schema()
        now = datetime.now(timezone.utc)
        item = AgentToolEvidence(
            id=uuid.uuid4(), request_id=_uuid(context.request_id), session_id=_uuid(context.session_id),
            customer_id=context.customer_id.upper(), tool_call_id=str(tool_call_id), tool_name=str(tool_name),
            task_id=context.task_id,
            input_hash=input_hash(arguments or {}), evidence_type=str(evidence_type),
            sanitized_payload=sanitize_evidence_payload(payload), source=str(source), as_of=str(as_of),
            created_at=now, expires_at=now + EVIDENCE_TTL,
        )
        with session_scope() as session:
            session.add(item)
            session.flush()
            return _row(item)

    def get(self, evidence_id: str, context: EvidenceContext) -> dict[str, Any]:
        ensure_schema()
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            item = session.scalar(
                select(AgentToolEvidence)
                .where(AgentToolEvidence.id == _uuid(evidence_id))
                .where(AgentToolEvidence.request_id == _uuid(context.request_id))
                .where(AgentToolEvidence.session_id == _uuid(context.session_id))
                .where(AgentToolEvidence.customer_id == context.customer_id.upper())
                .where(AgentToolEvidence.task_id.in_((context.task_id, *context.dependency_task_ids)))
                .where(AgentToolEvidence.expires_at > now)
            )
            if item is None:
                raise EvidenceAccessError("evidence_not_available")
            return _row(item)

    def list(self, evidence_ids: list[str], context: EvidenceContext) -> list[dict[str, Any]]:
        return [self.get(evidence_id, context) for evidence_id in evidence_ids]

    def cleanup_expired(self) -> int:
        ensure_schema()
        with session_scope() as session:
            result = session.execute(delete(AgentToolEvidence).where(
                AgentToolEvidence.expires_at <= datetime.now(timezone.utc)
            ))
            return int(result.rowcount or 0)


_store: PostgresAgentEvidenceStore | None = None


def get_agent_evidence_store() -> PostgresAgentEvidenceStore:
    global _store
    if _store is None:
        _store = PostgresAgentEvidenceStore()
    return _store


__all__ = ["PostgresAgentEvidenceStore", "get_agent_evidence_store"]
