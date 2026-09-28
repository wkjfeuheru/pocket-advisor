"""认证与账户请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, description="用户名")
    password: str = Field(..., min_length=6, max_length=64, description="密码")
    display_name: str = Field(default="", description="显示名称")


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    customer_id: str
    username: str
    display_name: str = ""
    is_admin: bool = False


class LoginResponse(BaseModel):
    customer_id: str
    username: str
    display_name: str = ""
    token: str
    expires_in: int = 7 * 24 * 3600
    is_admin: bool = False


class RegisterResponse(BaseModel):
    customer_id: str
    username: str
    display_name: str = ""


__all__ = ["LoginRequest", "LoginResponse", "RegisterRequest", "RegisterResponse", "UserInfo"]
