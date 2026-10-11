"""PostgreSQL schema 契约与集中式 SQL 脚本加载。

表结构的**唯一事实源**是仓库 ``migrations/*.sql`` 下的幂等 DDL；本模块只负责按
固定顺序加载并应用它们，供部署脚本（``python -m scripts.migrate``）与 ORM 首次使用时的
惰性建表共用。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

BUSINESS_SCHEMA = "finance"

# 业务结构 DDL 的应用顺序。种子单独由 include_seed 控制。
SCHEMA_APPLY_ORDER = (
    "001_schema.sql",
    "003_advisor.sql",
    "004_stock_features.sql",
    "005_agent_evidence.sql",
    "006_portfolio_advisor.sql",
    "007_advisor_v2.sql",
)

SEED_SCHEMA_FILES = ("002_seed.sql",)


def sql_dir() -> Path:
    """定位仓库 ``migrations/`` 目录；容器内可通过 ``FINANCE_SQL_DIR`` 覆盖。"""
    env = os.getenv("FINANCE_SQL_DIR", "").strip()
    if env:
        path = Path(env)
        if not path.is_dir():
            raise RuntimeError(f"FINANCE_SQL_DIR 不是目录: {path}")
        return path
    candidates = (
        Path(__file__).resolve().parents[2] / "migrations",
        Path.cwd() / "migrations",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError("找不到 migrations/ 目录。请在仓库根目录运行，或设置 FINANCE_SQL_DIR。")


def _load_sql(filename: str) -> str:
    """从仓库根目录的 migrations/ 加载 UTF-8 脚本。"""
    path = sql_dir() / filename
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 PostgreSQL SQL 脚本: {path}") from exc
    if not content:
        raise RuntimeError(f"PostgreSQL SQL 脚本为空: {path}")
    return content


def _execute_sql(cursor: Any, label: str, sql: str) -> None:
    """执行一段 DDL；失败带上脚本名，不吞异常。"""
    try:
        cursor.execute(sql)
    except Exception as exc:
        raise RuntimeError(f"应用 {label} 失败: {exc}") from exc


def apply_postgres_schema(connection: Any, *, include_seed: bool = False) -> None:
    """按统一清单幂等应用 schema。失败向上抛出，由调用方决定提交或退出。"""
    cursor = connection.cursor()
    try:
        for filename in SCHEMA_APPLY_ORDER:
            _execute_sql(cursor, filename, _load_sql(filename))
        if include_seed:
            for filename in SEED_SCHEMA_FILES:
                _execute_sql(cursor, filename, _load_sql(filename))
    finally:
        cursor.close()


def schema_statements(*, include_seed: bool = False) -> list[str]:
    """返回将要执行的 SQL 文本序列（诊断/测试用）。"""
    statements = [_load_sql(name) for name in SCHEMA_APPLY_ORDER]
    if include_seed:
        statements.extend(_load_sql(name) for name in SEED_SCHEMA_FILES)
    return statements


# 整份结构脚本。需要直接执行 DDL 的调用方读这一份，不再按历史补丁拆常量。
SCHEMA_SQL = _load_sql("001_schema.sql")


__all__ = [
    "BUSINESS_SCHEMA",
    "SCHEMA_APPLY_ORDER",
    "SCHEMA_SQL",
    "SEED_SCHEMA_FILES",
    "apply_postgres_schema",
    "schema_statements",
    "sql_dir",
]
