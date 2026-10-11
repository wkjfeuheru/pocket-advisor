"""模拟交易业务系统 API —— FastAPI 应用组装与 ASGI 入口。

启动::

    python -m app.main
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.agents.registry import init_agents
from app.core.config import CORS_ALLOW_ORIGINS, IS_PRODUCTION
from app.core.logging import configure_logging
from app.db.base import load_all_models
from app.routers import routers
from app.utils.rate_limit import enforce_auth_rate_limit

load_all_models()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时配置日志，并初始化投顾运行时与净值调度。任一初始化失败都不阻止业务 API。"""
    configure_logging()
    await init_agents()
    try:
        # 净值调度是增强能力：APScheduler 缺失或启动失败只降级，不影响业务 API。
        from app.services.portfolio_advisor.scheduler import nav_scheduler

        with nav_scheduler() as scheduler_status:
            _app.state.nav_scheduler_status = scheduler_status
            yield
    finally:
        from app.agents.registry import close_agents

        await close_agents()


app = FastAPI(
    title="模拟交易业务 API",
    description="模拟交易业务系统后端 API（商品、持仓、账户、认证与管理）",
    version="2.1.0",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in routers:
    app.include_router(_router)


@app.get("/")
async def root() -> dict:
    payload = {
        "service": "模拟交易业务 API",
        "version": "2.1.0",
        "health": "/api/health",
    }
    if not IS_PRODUCTION:
        payload["docs"] = "/docs"
        payload["endpoints"] = [
            "POST /api/register",
            "POST /api/login",
            "POST /api/logout",
            "GET  /api/me",
            "DELETE /api/account",
            "GET  /api/health",
            "GET  /api/portfolio/products",
            "GET  /api/portfolio/products/{code}",
            "GET  /api/portfolio/account",
            "GET  /api/portfolio/positions",
            "POST /api/portfolio/deposit",
            "POST /api/portfolio/orders",
            "POST /api/portfolio/liquidate",
            "GET  /api/portfolio/orders",
            "GET  /api/portfolio/transactions",
            "GET  /api/admin/users",
            "GET  /api/admin/users/{customer_id}/portfolio",
            "GET  /api/admin/products",
            "POST /api/admin/products",
            "POST /api/admin/products/{code}/offline",
            "POST /api/admin/products/{code}/publish",
        ]
    return payload


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.middleware("http")
async def auth_rate_limit_middleware(request, call_next):
    """登录/注册进程内限流，抵御凭据爆破。"""
    path = request.url.path.rstrip("/")
    if request.method == "POST" and path in {"/api/login", "/api/register"}:
        action = "login" if path.endswith("login") else "register"
        try:
            enforce_auth_rate_limit(request, action)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


def selector_loop_factory():
    """强制使用 Selector 事件循环。

    Windows 上 asyncio 默认的 ``ProactorEventLoop`` 与 psycopg 异步连接不兼容，
    投顾运行时（PgSaver 连接池）会永久降级；官方启动路径必须避开它。
    """
    import asyncio
    import selectors

    return asyncio.SelectorEventLoop(selectors.SelectSelector())


__all__ = ["app", "selector_loop_factory"]


if __name__ == "__main__":
    import sys

    import uvicorn

    from app.core.config import UVICORN_HOST, UVICORN_PORT

    # 非 reload 的 uvicorn 在 Windows 会装 ProactorEventLoop，必须显式换掉。
    loop = "app.main:selector_loop_factory" if sys.platform == "win32" else "auto"
    uvicorn.run("app.main:app", host=UVICORN_HOST, port=UVICORN_PORT, reload=False, loop=loop)
