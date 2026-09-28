"""安全工具：密码哈希、令牌有效期与唯一键冲突识别。"""

from __future__ import annotations

import hashlib

#: 登录会话有效期（7 天）。
TOKEN_TTL_SECONDS = 7 * 24 * 3600


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-SHA256 加盐哈希。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def is_unique_violation(exc: BaseException) -> bool:
    """识别 PostgreSQL unique_violation（SQLSTATE 23505）及其包装异常。

    既覆盖 SQLAlchemy ``IntegrityError``（``orig.sqlstate`` / ``pgcode``），也覆盖
    裸 psycopg 异常与测试用假异常（``sqlstate`` 属性）。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        orig = getattr(current, "orig", None)
        for candidate in (current, orig):
            if candidate is None:
                continue
            state = str(
                getattr(candidate, "sqlstate", "")
                or getattr(candidate, "pgcode", "")
                or ""
            )
            if state == "23505":
                return True
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    return "duplicate" in text and "unique" in text


class UniqueConstraintError(Exception):
    """测试与适配层用来模拟 PostgreSQL unique_violation（SQLSTATE 23505）。"""

    sqlstate = "23505"


__all__ = [
    "TOKEN_TTL_SECONDS",
    "UniqueConstraintError",
    "hash_password",
    "is_unique_violation",
]
