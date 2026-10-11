"""股票 Agent 的短期证据句柄、清洗与预览。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

EVIDENCE_TTL = timedelta(hours=24)
PREVIEW_ROWS = 20
_SECRET_KEYS = {
    "authorization", "headers", "cookie", "set-cookie", "api_key", "apikey",
    "password", "secret", "token", "access_token", "refresh_token", "body", "content",
}


class EvidenceAccessError(LookupError):
    """证据不存在、过期或不属于当前请求。"""


@dataclass(frozen=True)
class EvidenceContext:
    request_id: str
    session_id: str
    customer_id: str
    task_id: str = ""
    dependency_task_ids: tuple[str, ...] = ()


def sanitize_evidence_payload(value: Any, *, key: str = "") -> Any:
    if key.lower() in _SECRET_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): sanitize_evidence_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence_payload(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _preview(value: Any) -> Any:
    if isinstance(value, list):
        return [_preview(item) for item in value[:PREVIEW_ROWS]]
    if isinstance(value, dict):
        return {key: _preview(item) for key, item in value.items()}
    return value


def compact_evidence_preview(payload: Any, evidence_id: str) -> dict[str, Any]:
    preview = _preview(payload)
    row_count = len(payload) if isinstance(payload, list) else None
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("items") or payload.get("data")
        if isinstance(rows, list):
            row_count = len(rows)
    return {"evidence_id": evidence_id, "row_count": row_count, "preview": preview}


def input_hash(value: Any) -> str:
    packed = json.dumps(sanitize_evidence_payload(value), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


class MemoryAgentEvidenceStore:
    """测试及降级运行用的内存证据仓库。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rows: dict[str, dict[str, Any]] = {}

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
        now = self._clock()
        evidence_id = str(uuid4())
        row = {
            "id": evidence_id,
            "request_id": context.request_id,
            "session_id": context.session_id,
            "customer_id": context.customer_id.upper(),
            "task_id": context.task_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input_hash": input_hash(arguments or {}),
            "evidence_type": evidence_type,
            "payload": sanitize_evidence_payload(payload),
            "source": source,
            "as_of": as_of,
            "created_at": now.isoformat(),
            "expires_at": (now + EVIDENCE_TTL).isoformat(),
        }
        self._rows[evidence_id] = row
        return dict(row)

    def get(self, evidence_id: str, context: EvidenceContext) -> dict[str, Any]:
        row = self._rows.get(str(evidence_id))
        if (
            row is None
            or row["request_id"] != context.request_id
            or row["session_id"] != context.session_id
            or row["customer_id"] != context.customer_id.upper()
            or row.get("task_id", "") not in (context.task_id, *context.dependency_task_ids)
            or datetime.fromisoformat(row["expires_at"]) <= self._clock()
        ):
            raise EvidenceAccessError("evidence_not_available")
        return dict(row)

    def list(self, evidence_ids: list[str], context: EvidenceContext) -> list[dict[str, Any]]:
        return [self.get(item, context) for item in evidence_ids]

    def cleanup_expired(self) -> int:
        now = self._clock()
        expired = [key for key, row in self._rows.items() if datetime.fromisoformat(row["expires_at"]) <= now]
        for key in expired:
            self._rows.pop(key, None)
        return len(expired)


__all__ = [
    "EVIDENCE_TTL",
    "EvidenceAccessError",
    "EvidenceContext",
    "MemoryAgentEvidenceStore",
    "compact_evidence_preview",
    "input_hash",
    "sanitize_evidence_payload",
]
