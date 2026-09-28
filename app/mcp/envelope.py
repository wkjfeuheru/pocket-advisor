"""上游响应信封：统一错误码、截止时间，并去掉密钥。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.utils.sanitize import sanitize_error, sanitize_text

Envelope = dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_upstream_error(exc: BaseException) -> str:
    """把异常映射成稳定错误码，不回传密钥。"""
    text = sanitize_error(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in name or "timeout" in text:
        return "timeout"
    if "429" in text or "rate" in text and "limit" in text:
        return "rate_limited"
    if "not found" in text or name in {"lookuperror", "keyerror"}:
        return "not_found"
    if "403" in text or "forbidden" in text or "permission" in text:
        return "unavailable"
    return "unavailable"


def wrap_envelope(payload: Any, *, as_of: str = "", code: str = "") -> Envelope:
    """把成功数据或异常收成信封。"""
    if isinstance(payload, BaseException):
        error_code = code or classify_upstream_error(payload)
        return {
            "ok": False,
            "code": error_code,
            "as_of": as_of,
            "data": None,
            "warning": sanitize_error(payload),
        }
    data = payload
    stamp = as_of
    if isinstance(payload, dict):
        stamp = stamp or str(payload.get("as_of") or payload.get("update_time") or "")
    return {
        "ok": True,
        "code": "",
        "as_of": stamp or _now(),
        "data": data,
        "warning": "",
    }


def sanitize_envelope_warning(text: str) -> str:
    return sanitize_text(text)


__all__ = ["classify_upstream_error", "sanitize_envelope_warning", "wrap_envelope"]
