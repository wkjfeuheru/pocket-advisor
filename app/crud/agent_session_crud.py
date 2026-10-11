"""投顾会话、轮次与画像的 PostgreSQL 访问。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.security import UniqueConstraintError, is_unique_violation
from app.db.models.advisor_profile import AdvisorProfile
from app.db.models.agent_session import AgentMessage, AgentSession, AgentTurn
from app.db.session import ensure_schema, session_scope
from app.utils.text import normalize_whitespace

_SESSION: ContextVar[Any] = ContextVar("advisor_db_session", default=None)

ACTIVE_STATUSES = ("pending", "running", "interrupted")
TURN_STATUSES = ("pending", "running", "interrupted", "completed", "failed", "cancelled")


def conversation_title(message: str) -> str:
    """用清理后的前 40 个字符做标题，不调用模型。"""
    return normalize_whitespace(message)[:40] or "新对话"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


class PostgresAdvisorStore:
    """业务表读写。调用方通过 :meth:`transaction` 包住一组写入。"""

    @contextmanager
    def transaction(self) -> Iterator["PostgresAdvisorStore"]:
        if _SESSION.get() is not None:
            yield self
            return
        ensure_schema()
        with session_scope() as session:
            token = _SESSION.set(session)
            try:
                yield self
            finally:
                _SESSION.reset(token)

    def _db(self) -> Any:
        session = _SESSION.get()
        if session is None:
            raise RuntimeError("投顾存储必须在 transaction() 内使用")
        return session

    def find_turn(self, request_id: str) -> dict[str, Any] | None:
        row = self._db().get(AgentTurn, _uuid(request_id))
        if row is None:
            return None
        parent = self._db().get(AgentSession, row.session_id)
        payload = _turn_dict(row)
        payload["customer_id"] = parent.customer_id if parent is not None else ""
        return payload

    def get_session(self, customer_id: str, session_id: str) -> dict[str, Any] | None:
        row = self._db().get(AgentSession, _uuid(session_id))
        if row is None or row.customer_id != customer_id.upper():
            return None
        return _session_dict(row)

    def lock_session(self, session_id: str) -> None:
        self._db().execute(
            select(AgentSession).where(AgentSession.id == _uuid(session_id)).with_for_update()
        )

    def active_turn(self, session_id: str) -> dict[str, Any] | None:
        row = self._db().scalars(
            select(AgentTurn)
            .where(AgentTurn.session_id == _uuid(session_id))
            .where(AgentTurn.status.in_(ACTIVE_STATUSES))
        ).first()
        return _turn_dict(row) if row is not None else None

    def insert_session(self, customer_id: str, title: str) -> dict[str, Any]:
        now = _utcnow()
        row = AgentSession(
            id=uuid.uuid4(),
            customer_id=customer_id.upper(),
            title=title,
            created_at=now,
            updated_at=now,
        )
        self._db().add(row)
        self._db().flush()
        return _session_dict(row)

    def touch_session(self, session_id: str) -> None:
        row = self._db().get(AgentSession, _uuid(session_id))
        if row is not None:
            row.updated_at = _utcnow()

    def insert_message(
        self,
        session_id: str,
        request_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = AgentMessage(
            id=uuid.uuid4(),
            session_id=_uuid(session_id),
            request_id=_uuid(request_id),
            role=role,
            content=content,
            public_metadata=metadata or {},
            created_at=_utcnow(),
        )
        self._db().add(row)
        try:
            self._db().flush()
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise UniqueConstraintError("同一轮同一角色不能重复写入") from exc
            raise
        return _message_dict(row)

    def insert_turn(self, session_id: str, request_id: str, status: str = "pending", *, graph_version: str = "advisor-v2") -> dict[str, Any]:
        if status not in TURN_STATUSES:
            raise ValueError("非法轮次状态")
        now = _utcnow()
        row = AgentTurn(
            request_id=_uuid(request_id),
            session_id=_uuid(session_id),
            status=status,
            graph_version=graph_version,
            stage="",
            interrupt_payload=None,
            final_answer="",
            error_code="",
            retryable=False,
            created_at=now,
            updated_at=now,
        )
        self._db().add(row)
        try:
            self._db().flush()
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise UniqueConstraintError("会话已有活动轮次或请求已存在") from exc
            raise
        return _turn_dict(row)

    def update_turn(self, request_id: str, **fields: Any) -> dict[str, Any]:
        row = self._db().get(AgentTurn, _uuid(request_id))
        if row is None:
            raise KeyError(request_id)
        if "status" in fields and fields["status"] not in TURN_STATUSES:
            raise ValueError("非法轮次状态")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _utcnow()
        self._db().flush()
        return _turn_dict(row)

    def claim_turn(
        self,
        customer_id: str,
        request_id: str,
        expected_status: str,
        *,
        require_retryable: bool = False,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """按状态原子抢占轮次，避免 resume/retry 重复执行 checkpoint。"""
        conditions = [
            AgentTurn.request_id == _uuid(request_id),
            AgentTurn.status == expected_status,
            AgentTurn.session_id.in_(
                select(AgentSession.id).where(AgentSession.customer_id == customer_id.upper())
            ),
        ]
        if require_retryable:
            conditions.append(AgentTurn.retryable.is_(True))
        result = self._db().scalars(
            update(AgentTurn)
            .where(*conditions)
            .values(**fields, updated_at=_utcnow())
            .returning(AgentTurn)
        ).first()
        if result is None:
            return None
        payload = _turn_dict(result)
        payload["customer_id"] = customer_id.upper()
        return payload

    def list_sessions(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self._db().scalars(
            select(AgentSession)
            .where(AgentSession.customer_id == customer_id.upper())
            .order_by(AgentSession.updated_at.desc())
        ).all()
        return [_session_dict(row) for row in rows]

    def list_turns(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._db().scalars(select(AgentTurn).where(AgentTurn.session_id == _uuid(session_id))).all()
        return [_turn_dict(row) for row in rows]

    def update_session_context(self, session_id: str, context: dict[str, Any]) -> None:
        row = self._db().get(AgentSession, _uuid(session_id))
        if row is None:
            raise KeyError(session_id)
        row.context = context

    def list_messages(self, customer_id: str, session_id: str) -> list[dict[str, Any]]:
        parent = self.get_session(customer_id, session_id)
        if parent is None:
            return []
        rows = self._db().scalars(
            select(AgentMessage)
            .where(AgentMessage.session_id == _uuid(session_id))
            .order_by(AgentMessage.created_at.asc())
        ).all()
        return [_message_dict(row) for row in rows]

    def delete_session(self, customer_id: str, session_id: str) -> bool:
        row = self._db().get(AgentSession, _uuid(session_id))
        if row is None or row.customer_id != customer_id.upper():
            return False
        self._db().delete(row)
        self._db().flush()
        return True

    def get_profile(self, customer_id: str) -> dict[str, Any]:
        row = self._db().get(AdvisorProfile, customer_id.upper())
        if row is None:
            return _empty_profile(customer_id)
        return _profile_dict(row)

    def upsert_profile(self, customer_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        row = self._db().get(AdvisorProfile, customer_id.upper())
        if row is None:
            row = AdvisorProfile(customer_id=customer_id.upper(), updated_at=_utcnow())
            self._db().add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _utcnow()
        self._db().flush()
        return _profile_dict(row)

    def complete_turn(self, request_id: str, **fields: Any) -> bool:
        """仅当轮次仍处于活动状态时才落完成，避免覆盖已失败/已重试的轮次。

        返回是否写入（``False`` 表示轮次已是终态，调用方应跳过副作用）。
        """
        values = {"status": "completed", "stage": "完成", "retryable": False, "error_code": ""}
        values.update(fields)
        result = self._db().execute(
            update(AgentTurn)
            .where(AgentTurn.request_id == _uuid(request_id))
            .where(AgentTurn.status.in_(("pending", "running")))
            .values(**values, updated_at=_utcnow())
        )
        return bool(result.rowcount)

    def fail_active_turn(self, request_id: str, *, code: str, retryable: bool) -> bool:
        """仅当轮次仍处于活动状态时标记失败，避免覆盖已终态（完成/失败）的轮次。"""
        result = self._db().execute(
            update(AgentTurn)
            .where(AgentTurn.request_id == _uuid(request_id))
            .where(AgentTurn.status.in_(("pending", "running")))
            .values(
                status="failed",
                stage="失败",
                error_code=code,
                retryable=retryable,
                updated_at=_utcnow(),
            )
        )
        return bool(result.rowcount)

    def mark_stale_running(self, timeout_seconds: int) -> int:
        cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
        result = self._db().execute(
            update(AgentTurn)
            .where(AgentTurn.status.in_(("pending", "running")))
            .where(AgentTurn.updated_at < cutoff)
            .values(status="failed", retryable=True, error_code="turn_timeout", updated_at=_utcnow())
        )
        return int(result.rowcount or 0)


def _session_dict(row: AgentSession) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "customer_id": row.customer_id,
        "title": row.title,
        "context": row.context or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _message_dict(row: AgentMessage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "request_id": str(row.request_id),
        "role": row.role,
        "content": row.content,
        "public_metadata": row.public_metadata or {},
        "created_at": _iso(row.created_at),
    }


def _turn_dict(row: AgentTurn) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id),
        "session_id": str(row.session_id),
        "status": row.status,
        "graph_version": row.graph_version,
        "stage": row.stage,
        "interrupt_payload": row.interrupt_payload,
        "final_answer": row.final_answer,
        "error_code": row.error_code,
        "retryable": bool(row.retryable),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _empty_profile(customer_id: str) -> dict[str, Any]:
    return {
        "customer_id": customer_id.upper(),
        "risk_level": None,
        "investment_horizon": None,
        "investment_goal": None,
        "liquidity_need": None,
        "budget_amount": None,
    }


def _profile_dict(row: AdvisorProfile) -> dict[str, Any]:
    budget = row.budget_amount
    return {
        "customer_id": row.customer_id,
        "risk_level": row.risk_level,
        "investment_horizon": row.investment_horizon,
        "investment_goal": row.investment_goal,
        "liquidity_need": row.liquidity_need,
        "budget_amount": float(budget) if budget is not None else None,
    }


_store: PostgresAdvisorStore | None = None


def get_advisor_store() -> PostgresAdvisorStore:
    global _store
    if _store is None:
        _store = PostgresAdvisorStore()
    return _store


__all__ = [
    "ACTIVE_STATUSES",
    "PostgresAdvisorStore",
    "conversation_title",
    "get_advisor_store",
]
