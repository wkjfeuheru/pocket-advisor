"""认证与账户生命周期路由。登录共用；注销仅限普通用户。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas.user import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse
from app.services.user_service import get_user_service
from app.core.dependencies import require_user
from app.utils.errors import http_500

router = APIRouter(tags=["auth"])


@router.post("/api/register", response_model=RegisterResponse)
async def register(request: RegisterRequest) -> RegisterResponse:
    try:
        return RegisterResponse(**get_user_service().register(
            username=request.username, password=request.password, display_name=request.display_name,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise http_500("注册", exc) from exc


@router.post("/api/login", response_model=LoginResponse)
async def login(request: LoginRequest) -> LoginResponse:
    try:
        return LoginResponse(**get_user_service().login(
            username=request.username, password=request.password,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise http_500("登录", exc) from exc


@router.post("/api/logout")
async def logout(request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    get_user_service().logout(token)
    return {"status": "ok", "message": "已登出"}


@router.get("/api/me")
async def get_current_user(request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    status, user = get_user_service().current_user(auth_header[7:].strip())
    if status == "unauthorized":
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.delete("/api/account")
async def delete_account(request: Request) -> dict[str, Any]:
    customer_id = require_user(request)
    try:
        if not get_user_service().delete_account(customer_id):
            raise HTTPException(status_code=404, detail="用户不存在")
        return {"status": "ok", "message": "账号已注销"}
    except HTTPException:
        raise
    except Exception as exc:
        raise http_500("注销", exc) from exc


__all__ = [
    "LoginRequest",
    "delete_account",
    "get_current_user",
    "login",
    "logout",
    "register",
    "router",
]
