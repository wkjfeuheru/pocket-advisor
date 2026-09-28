"""认证用户与会话的数据访问层。

在存储边界把 ORM 行映射为普通 ``dict``，让路由与内存替身无需感知 ORM。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update

from app.db.models.user import User, UserSession
from app.db.session import ensure_schema, session_scope
from app.core.security import TOKEN_TTL_SECONDS, hash_password, is_unique_violation

PASSWORD_MAX_LENGTH = 64
WEAK_PASSWORDS = frozenset({
    "admin123", "123456", "12345678", "password", "admin", "changeme",
    "qwerty", "111111", "000000",
})


def _validate_password(password: str) -> None:
    """在认证存储边界统一拒绝过短、过长或常见弱口令。"""
    if len(password or "") < 6:
        raise ValueError("密码至少需要 6 个字符")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"密码不得超过 {PASSWORD_MAX_LENGTH} 个字符")
    if password.lower() in WEAK_PASSWORDS:
        raise ValueError("密码过于简单，请更换")


class PostgresAuthStore:
    """认证（用户 + 会话）的 PostgreSQL 存储。"""

    def register(
        self, username: str, password: str, display_name: str = "", *, is_admin: bool = False,
    ) -> dict[str, Any]:
        """注册用户；``is_admin`` 仅由引导脚本使用（注册接口不暴露该参数）。"""
        username = (username or "").strip()
        if len(username) < 2:
            raise ValueError("用户名至少需要 2 个字符")
        _validate_password(password)
        salt = secrets.token_hex(16)
        name = display_name.strip() or username
        ensure_schema()
        try:
            with session_scope() as session:
                user = User(
                    customer_id=None,
                    username=username,
                    display_name=name,
                    password_hash=hash_password(password, salt),
                    salt=salt,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                    is_admin=bool(is_admin),
                )
                session.add(user)
                session.flush()
                user.customer_id = f"CUST{user.id:06d}"
                customer_id = user.customer_id
        except Exception as exc:
            if is_unique_violation(exc):
                raise ValueError(f"用户名 {username} 已存在") from exc
            raise
        return {
            "customer_id": customer_id,
            "username": username,
            "display_name": name,
            "is_admin": bool(is_admin),
        }

    def login(self, username: str, password: str) -> dict[str, Any]:
        ensure_schema()
        with session_scope() as session:
            user = session.scalars(
                select(User).where(func.lower(User.username) == func.lower((username or "").strip()))
            ).first()
            if user is None or not secrets.compare_digest(
                hash_password(password, user.salt), user.password_hash,
            ):
                raise ValueError("用户名或密码错误")
            token = secrets.token_urlsafe(32)
            session.execute(
                delete(UserSession).where(UserSession.expires_at <= int(time.time()))
            )
            session.add(UserSession(
                token=token,
                customer_id=user.customer_id,
                expires_at=int(time.time()) + TOKEN_TTL_SECONDS,
            ))
            return {
                "customer_id": user.customer_id,
                "username": user.username,
                "display_name": user.display_name,
                "token": token,
                "is_admin": bool(user.is_admin),
            }

    def verify_token(self, token: str) -> str | None:
        if not token:
            return None
        ensure_schema()
        with session_scope() as session:
            session_row = session.get(UserSession, token)
            if session_row is None or session_row.expires_at <= int(time.time()):
                if session_row is not None:
                    session.delete(session_row)
                return None
            return session_row.customer_id

    def logout(self, token: str) -> bool:
        if not token:
            return False
        ensure_schema()
        with session_scope() as session:
            result = session.execute(
                delete(UserSession).where(UserSession.token == token)
            )
            return bool(result.rowcount)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        ensure_schema()
        with session_scope() as session:
            user = session.scalars(
                select(User).where(func.lower(User.username) == func.lower((username or "").strip()))
            ).first()
            return self._public(user) if user else None

    def get_user_by_customer_id(self, customer_id: str) -> dict[str, Any] | None:
        ensure_schema()
        with session_scope() as session:
            user = session.scalars(
                select(User).where(User.customer_id == str(customer_id).upper())
            ).first()
            return self._public(user) if user else None

    def is_admin(self, customer_id: str) -> bool:
        """读取用户的库内管理员角色；用户不存在时返回 False。"""
        if not customer_id:
            return False
        ensure_schema()
        with session_scope() as session:
            value = session.scalar(
                select(User.is_admin).where(User.customer_id == str(customer_id).upper())
            )
            return bool(value)

    def set_admin(self, customer_id: str, is_admin: bool) -> bool:
        """授予/撤销管理员角色，返回是否命中用户。"""
        ensure_schema()
        with session_scope() as session:
            result = session.execute(
                update(User)
                .where(User.customer_id == str(customer_id).upper())
                .values(is_admin=bool(is_admin))
            )
            return bool(result.rowcount)

    def set_password(self, customer_id: str, password: str) -> bool:
        """重置密码（重新生成盐），返回是否命中用户。"""
        _validate_password(password)
        ensure_schema()
        salt = secrets.token_hex(16)
        with session_scope() as session:
            result = session.execute(
                update(User)
                .where(User.customer_id == str(customer_id).upper())
                .values(password_hash=hash_password(password, salt), salt=salt)
            )
            return bool(result.rowcount)

    def list_users(self) -> list[dict[str, Any]]:
        """全部用户（不含账户数字；管理端用 portfolio 口径覆盖）。"""
        ensure_schema()
        with session_scope() as session:
            users = list(session.scalars(select(User).order_by(User.id)))
            return [self._public(user) | {"id": user.id} for user in users]

    def delete_user(self, customer_id: str) -> bool:
        """删除用户；账户/持仓/成交/流水由外键级联一并清理。"""
        ensure_schema()
        with session_scope() as session:
            result = session.execute(
                delete(User).where(User.customer_id == str(customer_id).upper())
            )
            return bool(result.rowcount)

    @staticmethod
    def _public(user: User) -> dict[str, Any]:
        return {
            "username": user.username,
            "customer_id": user.customer_id,
            "display_name": user.display_name,
            "created_at": user.created_at,
            "is_admin": bool(user.is_admin),
        }


_user_store: PostgresAuthStore | None = None


def get_user_store() -> PostgresAuthStore:
    global _user_store
    if _user_store is None:
        _user_store = PostgresAuthStore()
    return _user_store


__all__ = [
    "PASSWORD_MAX_LENGTH",
    "PostgresAuthStore",
    "WEAK_PASSWORDS",
    "_validate_password",
    "get_user_store",
]
