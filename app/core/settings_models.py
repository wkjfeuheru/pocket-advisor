"""经校验的 PostgreSQL 设置模型。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


class PostgresSettings(BaseModel):
    """PostgreSQL 连接与连接池参数。"""

    model_config = ConfigDict(frozen=True)

    dsn: str = ""
    host: str = "localhost"
    port: str = "5432"
    user: str = "postgres"
    password: str = ""
    database: str = "advisor"
    connect_timeout: float = Field(default=10.0, gt=0)
    pool_min_size: int = Field(default=4, ge=1)
    pool_max_size: int = Field(default=16, ge=1)
    pool_timeout: float = Field(default=30.0, gt=0)

    @field_validator("port")
    @classmethod
    def _port_is_numeric(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("POSTGRES_PORT 必须是数字")
        return value

    @field_validator("pool_max_size")
    @classmethod
    def _pool_max_not_below_min(cls, value: int, info) -> int:
        minimum = info.data.get("pool_min_size")
        if minimum is not None and value < minimum:
            raise ValueError("POSTGRES_POOL_MAX_SIZE 不得小于 POSTGRES_POOL_MIN_SIZE")
        return value

    def psycopg_dsn(self) -> str:
        """psycopg 连接串。已配置 ``POSTGRES_DSN`` 时原样使用。"""
        if self.dsn:
            return self.dsn
        if not self.password:
            raise RuntimeError("POSTGRES_PASSWORD 或 POSTGRES_DSN 必须配置")
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        database = quote(self.database, safe="")
        return f"postgresql://{user}:{password}@{self.host}:{self.port}/{database}"

    def sqlalchemy_url(self) -> str:
        """SQLAlchemy 引擎地址。把 ``postgresql://`` 换成 psycopg 方言。"""
        dsn = self.psycopg_dsn()
        if dsn.startswith("postgresql+") or "+" in dsn.split("://", 1)[0]:
            return dsn
        if dsn.startswith("postgresql://"):
            return "postgresql+psycopg://" + dsn[len("postgresql://"):]
        if dsn.startswith("postgres://"):
            return "postgresql+psycopg://" + dsn[len("postgres://"):]
        return dsn


def load_postgres_settings() -> PostgresSettings:
    """从环境变量装配并校验 PostgreSQL 设置。"""
    return PostgresSettings(
        dsn=os.getenv("POSTGRES_DSN", "").strip(),
        host=os.getenv("POSTGRES_HOST", "localhost").strip(),
        port=os.getenv("POSTGRES_PORT", "5432").strip(),
        user=os.getenv("POSTGRES_USER", "postgres").strip(),
        password=os.getenv("POSTGRES_PASSWORD", "").strip(),
        database=os.getenv("POSTGRES_DB", "advisor").strip(),
        connect_timeout=_env_float("POSTGRES_CONNECT_TIMEOUT", 10.0),
        pool_min_size=_env_int("POSTGRES_POOL_MIN_SIZE", 4),
        pool_max_size=_env_int("POSTGRES_POOL_MAX_SIZE", 16),
        pool_timeout=_env_float("POSTGRES_POOL_TIMEOUT", 30.0),
    )


class AdvisorSettings(BaseModel):
    """投顾模型、行情密钥与单轮运行限额。密钥只供运行时使用，不进入健康检查。"""

    model_config = ConfigDict(frozen=True)

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    fuyao_api_key: str = ""
    tushare_token: str = ""
    turn_timeout_seconds: int = Field(default=120, gt=0)
    max_message_chars: int = Field(default=4000, gt=0)
    context_message_limit: int = Field(default=20, ge=1)


def load_advisor_settings() -> AdvisorSettings:
    """每次从环境变量装配，测试改环境后不必重启进程。"""
    return AdvisorSettings(
        api_key=os.getenv("ADVISOR_MODEL_API_KEY", "").strip(),
        base_url=os.getenv("ADVISOR_MODEL_BASE_URL", "https://api.deepseek.com").strip()
        or "https://api.deepseek.com",
        model_name=os.getenv("ADVISOR_MODEL_NAME", "deepseek-chat").strip() or "deepseek-chat",
        fuyao_api_key=os.getenv("FUYAO_API_KEY", "").strip(),
        tushare_token=os.getenv("TUSHARE_TOKEN", "").strip(),
        turn_timeout_seconds=_env_int("AGENT_TURN_TIMEOUT_SECONDS", 120),
        max_message_chars=_env_int("AGENT_MAX_MESSAGE_CHARS", 4000),
        context_message_limit=_env_int("AGENT_CONTEXT_MESSAGE_LIMIT", 20),
    )


def settings_dump(settings: BaseModel) -> dict[str, Any]:
    """便于诊断/日志的只读字典视图。投顾密钥不出现在该视图中。"""
    payload = settings.model_dump()
    for secret in ("api_key", "fuyao_api_key", "tushare_token", "password", "dsn"):
        if secret in payload and payload[secret]:
            payload[secret] = "***"
    return payload


__all__ = [
    "AdvisorSettings",
    "PostgresSettings",
    "load_advisor_settings",
    "load_postgres_settings",
    "settings_dump",
]
