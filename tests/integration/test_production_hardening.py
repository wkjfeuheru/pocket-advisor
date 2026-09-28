"""上线前安全护栏：限流、错误脱敏、弱口令、生产文档开关。"""

from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request

from app.utils.errors import http_500
from app.utils.rate_limit import SlidingWindowLimiter, enforce_auth_rate_limit
from app.crud.user_crud import (
    WEAK_PASSWORDS,
    _validate_password,
)
from app.core.security import (
    UniqueConstraintError,
    is_unique_violation,
)
from app.main import app


def test_http_500_does_not_leak_exception_text():
    exc = RuntimeError("DSN=postgres://user:secret@db/advisor")
    error = http_500("登录", exc)
    assert error.status_code == 500
    assert "secret" not in error.detail
    assert "postgres://" not in error.detail
    assert "稍后重试" in error.detail


def test_unique_violation_detects_sqlstate_and_psycopg_shape():
    assert is_unique_violation(UniqueConstraintError())
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = UniqueConstraintError()
    assert is_unique_violation(wrapped)
    assert not is_unique_violation(ValueError("账户不存在"))


def test_weak_passwords_are_rejected():
    for password in ("admin123", "123456", "password"):
        assert password in WEAK_PASSWORDS
        try:
            _validate_password(password)
        except ValueError as exc:
            assert "简单" in str(exc)
        else:
            raise AssertionError(f"{password} 应被拒绝")


def test_password_max_length_is_enforced():
    try:
        _validate_password("a" * 65)
    except ValueError as exc:
        assert "64" in str(exc)
    else:
        raise AssertionError("超长密码应被拒绝")


def test_sliding_window_limiter_blocks_after_budget():
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60)
    assert limiter.allow("login:1.1.1.1")
    assert limiter.allow("login:1.1.1.1")
    assert limiter.allow("login:1.1.1.1")
    assert limiter.allow("login:1.1.1.1") is False
    assert limiter.allow("login:2.2.2.2") is True


def test_auth_rate_limit_returns_429(monkeypatch):
    class _Limiter:
        def allow(self, key: str) -> bool:
            return False

    monkeypatch.setattr("app.utils.rate_limit.get_auth_limiter", lambda: _Limiter())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/login",
        "raw_path": b"/api/login",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.10", 443),
        "server": ("test", 80),
    }
    request = Request(scope)
    try:
        enforce_auth_rate_limit(request, "login")
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("超限必须 429")


def test_production_disables_openapi_docs():
    from app.core.config import IS_PRODUCTION

    if IS_PRODUCTION:
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None
    else:
        assert app.docs_url == "/docs"
        assert app.redoc_url == "/redoc"
        assert app.openapi_url == "/openapi.json"
