"""API 共享依赖与鉴权门禁。

Bearer token → customer_id；普通用户与管理员互斥：
``require_user`` 拒绝管理员，``require_admin`` 拒绝普通用户。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.config import ADMIN_CUSTOMER_IDS


def get_user_store():
    from app.crud.user_crud import get_user_store as _get

    return _get()


def resolve_customer_id(request: Request) -> str:
    """从 Authorization: Bearer 解析并校验 token，返回 customer_id；失败抛 401。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    customer_id = get_user_store().verify_token(auth_header[7:].strip())
    if not customer_id:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return customer_id


_require_customer_id = resolve_customer_id


def authorize_customer(request: Request, path_customer_id: str) -> str:
    """要求已登录，且路径中的 customer_id 必须等于登录用户，否则 403。"""
    current = resolve_customer_id(request)
    if str(current).upper() != str(path_customer_id).upper():
        raise HTTPException(status_code=403, detail="无权访问该客户资源")
    return current


_authorize_customer = authorize_customer


def is_admin(customer_id: str) -> bool:
    """判断客户是否为管理员。

    两条路径取并集：命中 ``ADMIN_CUSTOMER_IDS`` 环境变量白名单，或库内
    ``users.is_admin`` 为真。角色查询失败必须失败关闭。
    """
    if str(customer_id).upper() in ADMIN_CUSTOMER_IDS:
        return True
    checker = getattr(get_user_store(), "is_admin", None)
    if checker is None:
        return False
    return bool(checker(customer_id))


_is_admin = is_admin


def require_user(request: Request) -> str:
    """已登录且不是管理员；管理员请走管理接口。"""
    customer_id = resolve_customer_id(request)
    try:
        admin = is_admin(customer_id)
    except Exception as exc:  # noqa: BLE001 - 授权状态未知时拒绝放行
        raise HTTPException(status_code=503, detail="权限服务暂不可用") from exc
    if admin:
        raise HTTPException(status_code=403, detail="管理员请使用管理接口")
    return customer_id


def require_admin(request: Request) -> str:
    customer_id = resolve_customer_id(request)
    try:
        admin = is_admin(customer_id)
    except Exception as exc:  # noqa: BLE001 - 授权状态未知时拒绝放行
        raise HTTPException(status_code=503, detail="权限服务暂不可用") from exc
    if not admin:
        raise HTTPException(status_code=403, detail="仅管理员可访问该接口")
    return customer_id


_require_admin = require_admin
_require_user = require_user


def get_db():
    """请求级 ORM Session，供后续 Agent 路由使用。

    现有 CRUD 仍使用 ``app.db.session.session_scope``，不经过本依赖。
    """
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "ADMIN_CUSTOMER_IDS",
    "_authorize_customer",
    "_is_admin",
    "_require_admin",
    "_require_customer_id",
    "_require_user",
    "authorize_customer",
    "get_db",
    "get_user_store",
    "is_admin",
    "require_admin",
    "require_user",
    "resolve_customer_id",
]
