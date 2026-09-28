"""把异常文本里的连接串和密钥抹掉，再写日志或健康检查。"""

from __future__ import annotations

import re

_DSN = re.compile(r"postgres(?:ql)?://\S+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|password|secret|token|dsn)\b\s*[=:]\s*\S+"
)
_BEARER = re.compile(r"(?i)bearer\s+\S+")


def sanitize_text(text: str) -> str:
    """去掉 DSN、Bearer 和形如 ``password=...`` 的片段。"""
    cleaned = _DSN.sub("[redacted-dsn]", text or "")
    cleaned = _BEARER.sub("Bearer [redacted]", cleaned)
    cleaned = _SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    return cleaned[:300]


def sanitize_error(exc: BaseException) -> str:
    return sanitize_text(f"{type(exc).__name__}: {exc}")


__all__ = ["sanitize_error", "sanitize_text"]
