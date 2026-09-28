"""用户注册、登录与账户生命周期。路由只做 HTTP 映射。"""

from __future__ import annotations

from typing import Any


def get_user_store():
    from app.crud.user_crud import get_user_store as _get

    return _get()


class UserService:
    """认证存储之上的薄编排。"""

    def register(self, *, username: str, password: str, display_name: str = "") -> dict[str, Any]:
        return get_user_store().register(
            username=username, password=password, display_name=display_name,
        )

    def login(self, *, username: str, password: str) -> dict[str, Any]:
        from app.core.dependencies import is_admin

        result = get_user_store().login(username=username, password=password)
        result.pop("is_admin", None)
        customer_id = result["customer_id"] if "customer_id" in result else ""
        return {**result, "is_admin": is_admin(customer_id)}

    def logout(self, token: str) -> None:
        if token:
            get_user_store().logout(token)

    def current_user(self, token: str) -> tuple[str, dict[str, Any] | None]:
        """返回 ``(unauthorized|missing|ok, user)``。"""
        from app.core.dependencies import is_admin

        customer_id = get_user_store().verify_token(token)
        if not customer_id:
            return "unauthorized", None
        user = get_user_store().get_user_by_customer_id(customer_id)
        if not user:
            return "missing", None
        return "ok", {**user, "is_admin": is_admin(customer_id)}

    def delete_account(self, customer_id: str) -> bool:
        """用户不存在返回 False。"""
        store = get_user_store()
        if not store.get_user_by_customer_id(customer_id):
            return False
        store.delete_user(customer_id)
        return True


_service: UserService | None = None


def get_user_service() -> UserService:
    global _service
    if _service is None:
        _service = UserService()
    return _service


__all__ = ["UserService", "get_user_service", "get_user_store"]
