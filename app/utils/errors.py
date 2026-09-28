"""API 层错误处理：对外只返回安全文案，内部记录完整异常。"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "服务暂时不可用，请稍后重试"


class BusinessError(Exception):
    """业务失败的基类；路由按 ``code`` 映射 HTTP 状态。"""

    code = "business_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def http_500(action: str, exc: BaseException) -> HTTPException:
    """把未预期异常记入日志，响应里不回传内部细节。"""
    logger.exception("%s failed: %s", action, exc)
    return HTTPException(status_code=500, detail=f"{action}失败，请稍后重试")


def client_ip(request: Request) -> str:
    """取限流用的客户端地址；默认不信任 X-Forwarded-For。"""
    from app.core.config import TRUST_PROXY

    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


__all__ = ["BusinessError", "GENERIC_FAILURE", "client_ip", "http_500"]
