"""进程内滑动窗口限流（登录/注册防爆破）。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.core.config import AUTH_RATE_LIMIT_ATTEMPTS, AUTH_RATE_LIMIT_WINDOW_SECONDS
from app.utils.errors import client_ip


class SlidingWindowLimiter:
    """按键统计时间窗口内的命中次数。"""

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_auth_limiter: SlidingWindowLimiter | None = None
_auth_limiter_lock = threading.Lock()


def get_auth_limiter() -> SlidingWindowLimiter:
    global _auth_limiter
    if _auth_limiter is None:
        with _auth_limiter_lock:
            if _auth_limiter is None:
                _auth_limiter = SlidingWindowLimiter(
                    AUTH_RATE_LIMIT_ATTEMPTS, AUTH_RATE_LIMIT_WINDOW_SECONDS,
                )
    return _auth_limiter


def enforce_auth_rate_limit(request: Request, action: str = "auth") -> None:
    """超限返回 429；未传入 Request（单测直接调路由）时跳过。"""
    ip = client_ip(request)
    if not get_auth_limiter().allow(f"{action}:{ip}"):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")


__all__ = ["SlidingWindowLimiter", "enforce_auth_rate_limit", "get_auth_limiter"]
