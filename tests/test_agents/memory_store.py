"""内存中的投顾存储，语义与 PostgreSQL 约束对齐，供不连库的测试使用。"""

from __future__ import annotations

import copy
import uuid
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app.core.security import UniqueConstraintError

ACTIVE = {"pending", "running", "interrupted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryAdvisorStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.turns: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self._claim_lock = threading.Lock()

    @contextmanager
    def transaction(self) -> Iterator["MemoryAdvisorStore"]:
        snapshot = (
            copy.deepcopy(self.sessions),
            copy.deepcopy(self.messages),
            copy.deepcopy(self.turns),
            copy.deepcopy(self.profiles),
        )
        try:
            yield self
        except Exception:
            self.sessions, self.messages, self.turns, self.profiles = snapshot
            raise

    def find_turn(self, request_id: str) -> dict[str, Any] | None:
        turn = self.turns.get(request_id)
        if turn is None:
            return None
        parent = self.sessions.get(turn["session_id"])
        payload = dict(turn)
        payload["customer_id"] = parent["customer_id"] if parent else ""
        return payload

    def get_session(self, customer_id: str, session_id: str) -> dict[str, Any] | None:
        row = self.sessions.get(session_id)
        if row is None or row["customer_id"] != customer_id.upper():
            return None
        return dict(row)

    def lock_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            raise KeyError(session_id)

    def active_turn(self, session_id: str) -> dict[str, Any] | None:
        for turn in self.turns.values():
            if turn["session_id"] == session_id and turn["status"] in ACTIVE:
                return dict(turn)
        return None

    def insert_session(self, customer_id: str, title: str) -> dict[str, Any]:
        now = _now().isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "customer_id": customer_id.upper(),
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
        self.sessions[row["id"]] = row
        return dict(row)

    def touch_session(self, session_id: str) -> None:
        self.sessions[session_id]["updated_at"] = _now().isoformat()

    def insert_message(self, session_id, request_id, role, content, metadata=None) -> dict[str, Any]:
        for message in self.messages:
            if message["request_id"] == request_id and message["role"] == role:
                raise UniqueConstraintError("同一轮同一角色不能重复写入")
        row = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "request_id": request_id,
            "role": role,
            "content": content,
            "public_metadata": metadata or {},
            "created_at": _now().isoformat(),
        }
        self.messages.append(row)
        return dict(row)

    def insert_turn(self, session_id: str, request_id: str, status: str = "pending") -> dict[str, Any]:
        if request_id in self.turns:
            raise UniqueConstraintError("请求已存在")
        if status in ACTIVE and self.active_turn(session_id) is not None:
            raise UniqueConstraintError("会话已有活动轮次")
        now = _now().isoformat()
        row = {
            "request_id": request_id,
            "session_id": session_id,
            "status": status,
            "stage": "",
            "interrupt_payload": None,
            "final_answer": "",
            "error_code": "",
            "retryable": False,
            "created_at": now,
            "updated_at": now,
        }
        self.turns[request_id] = row
        return dict(row)

    def update_turn(self, request_id: str, **fields: Any) -> dict[str, Any]:
        row = self.turns[request_id]
        row.update(fields)
        row["updated_at"] = _now().isoformat()
        if row["status"] in ACTIVE:
            for other in self.turns.values():
                if other["request_id"] != request_id and other["session_id"] == row["session_id"] and other["status"] in ACTIVE:
                    raise UniqueConstraintError("会话已有活动轮次")
        return dict(row)

    def claim_turn(
        self,
        customer_id: str,
        request_id: str,
        expected_status: str,
        *,
        require_retryable: bool = False,
        **fields: Any,
    ) -> dict[str, Any] | None:
        with self._claim_lock:
            turn = self.turns.get(request_id)
            parent = self.sessions.get(turn["session_id"]) if turn else None
            if (
                turn is None
                or parent is None
                or parent["customer_id"] != customer_id.upper()
                or turn["status"] != expected_status
                or (require_retryable and not turn["retryable"])
            ):
                return None
            return self.update_turn(request_id, **fields)

    def list_sessions(self, customer_id: str) -> list[dict[str, Any]]:
        rows = [row for row in self.sessions.values() if row["customer_id"] == customer_id.upper()]
        return sorted(rows, key=lambda item: item["updated_at"], reverse=True)

    def list_messages(self, customer_id: str, session_id: str) -> list[dict[str, Any]]:
        if self.get_session(customer_id, session_id) is None:
            return []
        return [dict(row) for row in self.messages if row["session_id"] == session_id]

    def delete_session(self, customer_id: str, session_id: str) -> bool:
        row = self.sessions.get(session_id)
        if row is None or row["customer_id"] != customer_id.upper():
            return False
        del self.sessions[session_id]
        self.messages = [item for item in self.messages if item["session_id"] != session_id]
        self.turns = {
            key: value for key, value in self.turns.items() if value["session_id"] != session_id
        }
        return True

    def get_profile(self, customer_id: str) -> dict[str, Any]:
        return dict(self.profiles.get(customer_id.upper()) or {
            "customer_id": customer_id.upper(),
            "risk_level": None,
            "investment_horizon": None,
            "investment_goal": None,
            "liquidity_need": None,
            "budget_amount": None,
        })

    def upsert_profile(self, customer_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        current = self.get_profile(customer_id)
        current.update(fields)
        current["customer_id"] = customer_id.upper()
        self.profiles[customer_id.upper()] = current
        return dict(current)

    def complete_turn(self, request_id: str, **fields: Any) -> bool:
        turn = self.turns.get(request_id)
        if turn is None or turn["status"] not in {"pending", "running"}:
            return False
        turn.update({"status": "completed", "stage": "完成", "retryable": False, "error_code": ""})
        turn.update(fields)
        turn["updated_at"] = _now().isoformat()
        return True

    def fail_active_turn(self, request_id: str, *, code: str, retryable: bool) -> bool:
        turn = self.turns.get(request_id)
        if turn is None or turn["status"] not in {"pending", "running"}:
            return False
        turn["status"] = "failed"
        turn["stage"] = "失败"
        turn["error_code"] = code
        turn["retryable"] = retryable
        turn["updated_at"] = _now().isoformat()
        return True

    def mark_stale_running(self, timeout_seconds: int) -> int:
        cutoff = (_now() - timedelta(seconds=timeout_seconds)).isoformat()
        count = 0
        for turn in self.turns.values():
            if turn["status"] in {"pending", "running"} and turn["updated_at"] < cutoff:
                turn["status"] = "failed"
                turn["retryable"] = True
                turn["error_code"] = "turn_timeout"
                count += 1
        return count
