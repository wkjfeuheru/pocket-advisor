"""管理员标志下发：login 与 /api/me 必须准确暴露 is_admin。"""

from __future__ import annotations

import asyncio

import pytest

from app.core import dependencies as deps
from app.routers import user as auth_routes
from app.services import user_service


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {"Authorization": "Bearer good-token"}


class _Store:
    def verify_token(self, token):
        return "ADMIN1" if token == "admin-token" else ("USER1" if token == "user-token" else None)

    def get_user_by_customer_id(self, customer_id):
        return {"customer_id": customer_id, "username": customer_id.lower(), "display_name": customer_id}

    def login(self, username, password):
        return {
            "customer_id": "ADMIN1",
            "username": username,
            "display_name": "管理员",
            "token": "admin-token",
        }


@pytest.fixture()
def store(monkeypatch):
    from app.core import dependencies as deps

    monkeypatch.setattr(user_service, "get_user_store", lambda: _Store())
    monkeypatch.setattr(deps, "get_user_store", lambda: _Store())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", {"ADMIN1"})


def test_login_marks_admin(store):
    response = asyncio.run(auth_routes.login(auth_routes.LoginRequest(username="admin", password="secret")))

    assert response.is_admin is True
    assert response.customer_id == "ADMIN1"


def test_me_exposes_is_admin_for_admin(store):
    payload = asyncio.run(auth_routes.get_current_user(_FakeRequest({"Authorization": "Bearer admin-token"})))

    assert payload["is_admin"] is True


def test_me_exposes_is_admin_false_for_regular_user(store):
    payload = asyncio.run(auth_routes.get_current_user(_FakeRequest({"Authorization": "Bearer user-token"})))

    assert payload["is_admin"] is False


def test_is_admin_is_whitelist_only_and_case_insensitive(monkeypatch):
    from app.core import dependencies as deps

    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", {"ADMIN1"})
    monkeypatch.setattr(deps, "get_user_store", lambda: type("S", (), {"is_admin": lambda self, cid: False})())

    assert deps._is_admin("admin1") is True
    assert deps._is_admin("ADMIN1") is True
    assert deps._is_admin("USER1") is False


def test_no_admin_configured_means_nobody_is_admin(monkeypatch):
    from app.core import dependencies as deps

    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())
    monkeypatch.setattr(deps, "get_user_store", lambda: type("S", (), {"is_admin": lambda self, cid: False})())

    assert deps._is_admin("ADMIN1") is False


def test_require_user_rejects_admin(monkeypatch):
    from fastapi import HTTPException

    from app.core import dependencies as deps

    class _Store:
        def verify_token(self, token):
            return "ADMIN1"

        def is_admin(self, customer_id):
            return True

    monkeypatch.setattr(deps, "get_user_store", lambda: _Store())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())

    with pytest.raises(HTTPException) as exc:
        deps.require_user(_FakeRequest({"Authorization": "Bearer admin-token"}))
    assert exc.value.status_code == 403
    assert "管理接口" in exc.value.detail


def test_require_admin_rejects_regular_user(monkeypatch):
    from fastapi import HTTPException

    from app.core import dependencies as deps

    class _Store:
        def verify_token(self, token):
            return "USER1"

        def is_admin(self, customer_id):
            return False

    monkeypatch.setattr(deps, "get_user_store", lambda: _Store())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())

    with pytest.raises(HTTPException) as exc:
        deps.require_admin(_FakeRequest({"Authorization": "Bearer user-token"}))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("guard", [deps.require_user, deps.require_admin])
def test_role_store_failure_makes_auth_guard_unavailable(monkeypatch, guard):
    from fastapi import HTTPException

    class _FailingRoleStore(_Store):
        def is_admin(self, customer_id):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(deps, "get_user_store", lambda: _FailingRoleStore())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())

    with pytest.raises(HTTPException) as exc:
        guard(_FakeRequest({"Authorization": "Bearer user-token"}))
    assert exc.value.status_code == 503


def test_is_admin_does_not_turn_role_store_failure_into_regular_user(monkeypatch):
    class _FailingRoleStore:
        def is_admin(self, customer_id):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(deps, "get_user_store", lambda: _FailingRoleStore())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())

    with pytest.raises(RuntimeError, match="database unavailable"):
        deps.is_admin("ADMIN1")
