"""运行环境、PostgreSQL 连接与业务策略常量的单一取值入口。

设计要点：

- 只读取配置文件与操作系统环境变量，不导入任何业务模块。
- 金额阈值/新鲜度阈值这类**领域策略值**经 ``dotenv_values()`` 直接读取 ``.env``，
  不依赖 ``load_dotenv()`` 的调用顺序，避免"先 import 应用再读 .env"时默认值被冻结。
"""

from __future__ import annotations

import os

from dotenv import dotenv_values, load_dotenv

from app.core.settings_models import (
    AdvisorSettings,
    PostgresSettings,
    load_advisor_settings,
    load_postgres_settings,
    settings_dump,
)

_DOTENV_VALUES = dotenv_values()

load_dotenv()

# 已关闭匿名模式，所有业务请求必须携带有效 Bearer token。
AUTH_REQUIRED = True

# 运行环境与加固参数。
UVICORN_HOST = os.getenv("UVICORN_HOST", "127.0.0.1").strip() or "127.0.0.1"
UVICORN_PORT = int(os.getenv("UVICORN_PORT", "8000"))
APP_ENV = os.getenv("APP_ENV", "development").strip().lower() or "development"
IS_PRODUCTION = APP_ENV in {"prod", "production"}
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").strip().lower() in {"1", "true", "yes", "on"}
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
    ).split(",")
    if origin.strip()
]
AUTH_RATE_LIMIT_ATTEMPTS = int(os.getenv("AUTH_RATE_LIMIT_ATTEMPTS", "8"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60"))

# 管理员 customer_id 白名单（逗号分隔）；用于限制管理接口（如清空全库记录）。
ADMIN_CUSTOMER_IDS = {
    cid.strip().upper()
    for cid in os.getenv("ADMIN_CUSTOMER_IDS", "").split(",")
    if cid.strip()
}


def _domain_env(name: str, default: str) -> str:
    """先读操作系统环境变量，再读 ``.env``，且不修改进程级环境。"""
    if name in os.environ:
        return os.environ[name]
    value = _DOTENV_VALUES.get(name)
    return default if value is None else value


# 模拟交易的金额阈值：单笔充值上限与单笔申购下限。
PORTFOLIO_MAX_DEPOSIT_AMOUNT = float(_domain_env("PORTFOLIO_MAX_DEPOSIT_AMOUNT", "10000000"))
PORTFOLIO_MIN_ORDER_AMOUNT = float(_domain_env("PORTFOLIO_MIN_ORDER_AMOUNT", "100"))

# 产品研究的新鲜度阈值：超过阈值只披露来源与日期，不据此生成收益/比较结论。
PRODUCT_PERFORMANCE_FRESHNESS_DAYS = int(os.getenv("PRODUCT_PERFORMANCE_FRESHNESS_DAYS", "31"))
PRODUCT_HOLDINGS_FRESHNESS_DAYS = int(os.getenv("PRODUCT_HOLDINGS_FRESHNESS_DAYS", "120"))

# ── PostgreSQL 存储 ─────────────────────────────────────
# 所有关系型数据与认证均使用 PostgreSQL。
# 连接/连接池参数经 ``PostgresSettings`` 校验（端口格式、池上限不低于下限、超时为正），
# 此处保留同名兼容常量。
_postgres = load_postgres_settings()
POSTGRES_DSN = _postgres.dsn
POSTGRES_HOST = _postgres.host
POSTGRES_PORT = _postgres.port
POSTGRES_USER = _postgres.user
POSTGRES_PASSWORD = _postgres.password
POSTGRES_DB = _postgres.database
POSTGRES_CONNECT_TIMEOUT = _postgres.connect_timeout
POSTGRES_POOL_MIN_SIZE = _postgres.pool_min_size
POSTGRES_POOL_MAX_SIZE = _postgres.pool_max_size
POSTGRES_POOL_TIMEOUT = _postgres.pool_timeout


def postgres_dsn() -> str:
    """公开的 DSN 构建入口。每次调用都重新读取环境，不使用 import 时的快照。"""
    return load_postgres_settings().psycopg_dsn()


def get_postgres_connection_factory():
    """返回 PostgreSQL 连接工厂；驱动缺失时显式失败。"""
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 PostgreSQL 驱动，请安装 psycopg[binary]") from exc
    settings = load_postgres_settings()
    dsn = settings.psycopg_dsn()
    timeout = settings.connect_timeout
    return lambda: psycopg.connect(dsn, connect_timeout=timeout)


__all__ = [
    "ADMIN_CUSTOMER_IDS",
    "AdvisorSettings",
    "APP_ENV",
    "AUTH_RATE_LIMIT_ATTEMPTS",
    "AUTH_RATE_LIMIT_WINDOW_SECONDS",
    "AUTH_REQUIRED",
    "CORS_ALLOW_ORIGINS",
    "IS_PRODUCTION",
    "PORTFOLIO_MAX_DEPOSIT_AMOUNT",
    "PORTFOLIO_MIN_ORDER_AMOUNT",
    "POSTGRES_DB",
    "POSTGRES_DSN",
    "POSTGRES_HOST",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "PRODUCT_HOLDINGS_FRESHNESS_DAYS",
    "PRODUCT_PERFORMANCE_FRESHNESS_DAYS",
    "PostgresSettings",
    "TRUST_PROXY",
    "UVICORN_HOST",
    "UVICORN_PORT",
    "get_postgres_connection_factory",
    "load_advisor_settings",
    "load_postgres_settings",
    "postgres_dsn",
    "settings_dump",
]
