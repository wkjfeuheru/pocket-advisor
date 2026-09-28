"""认证用户与登录会话的 ORM 映射（``finance.users`` / ``finance.sessions``）。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """注册用户。``customer_id`` 由自增 ``id`` 派生（``CUST%06d``）。"""

    __tablename__ = "users"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    salt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserSession(Base):
    """登录会话（Bearer token）。"""

    __tablename__ = "sessions"
    __table_args__ = {"schema": "finance"}

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["User", "UserSession"]
