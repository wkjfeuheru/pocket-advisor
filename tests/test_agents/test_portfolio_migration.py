"""迁移 006 的 DDL 契约：七张表与关键约束必须真的存在。

只读 SQL 文本，不需要数据库。真正落到 PostgreSQL 的往返（尤其覆盖语义与唯一约束）
由 ``tests/integration/test_portfolio_advisor_postgres.py`` 用真实库验证。

这类测试防的是「改了 ORM 忘了改迁移」——假 Session 只能验证属性赋值，抓不到
「迁移没真正建列」，而线上是迁移说了算。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.db.schema import SCHEMA_APPLY_ORDER, sql_dir

MIGRATION = "006_portfolio_advisor.sql"

NEW_TABLES = (
    "product_nav_history",
    "allocation_plans",
    "allocation_plan_items",
    "ai_accounts",
    "ai_positions",
    "ai_trades",
    "ai_portfolio_snapshots",
)


@pytest.fixture(scope="module")
def ddl() -> str:
    path = sql_dir() / MIGRATION
    assert path.is_file(), f"缺少迁移文件: {path}"
    return path.read_text(encoding="utf-8")


def test_the_migration_is_registered_in_the_apply_order(ddl):
    assert MIGRATION in SCHEMA_APPLY_ORDER, (
        f"{MIGRATION} 未加入 SCHEMA_APPLY_ORDER，部署时不会被执行"
    )
    # 顺序必须在 005 之后，否则外键指向的表可能还不存在。
    assert SCHEMA_APPLY_ORDER.index(MIGRATION) > SCHEMA_APPLY_ORDER.index("005_agent_evidence.sql")


@pytest.mark.parametrize("table", NEW_TABLES)
def test_every_new_table_is_created_idempotently(ddl, table):
    pattern = rf"CREATE TABLE IF NOT EXISTS finance\.{table}\s*\("
    assert re.search(pattern, ddl, re.IGNORECASE), f"缺少幂等建表: {table}"


def test_nav_history_is_keyed_by_product_and_trade_date_with_source_guard(ddl):
    # 主键决定幂等 upsert 的冲突目标。
    assert re.search(
        r"PRIMARY KEY \(product_code, trade_date\)", ddl, re.IGNORECASE
    ), "product_nav_history 必须以 (product_code, trade_date) 为主键"
    # data_source 只有两种取值，且默认 real。
    assert re.search(
        r"data_source varchar\(16\) NOT NULL DEFAULT 'real'", ddl, re.IGNORECASE
    )
    assert "'synthetic'" in ddl and "'real'" in ddl


def test_ai_trades_has_both_idempotency_levels(ddl):
    """两级幂等键缺一不可，否则"采纳一个多标的方案只成交一笔"会复发。"""
    assert re.search(r"adoption_key varchar\(128\)", ddl, re.IGNORECASE), (
        "ai_trades 缺少 adoption_key（采纳级幂等键）"
    )
    assert re.search(r"trade_key varchar\(128\)", ddl, re.IGNORECASE), (
        "ai_trades 缺少 trade_key（成交级幂等键）"
    )
    assert re.search(
        r"UNIQUE INDEX IF NOT EXISTS uq_ai_trades_trade_key[\s\S]*?\(customer_id, trade_key\)",
        ddl,
        re.IGNORECASE,
    ), "ai_trades 需要 UNIQUE (customer_id, trade_key)"


def _table_body(ddl: str, table: str) -> str:
    """截取 ``CREATE TABLE finance.<table> ( ... );`` 的正文。"""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS finance\.{table}\s*\((.*?)\n\);",
        ddl,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, f"找不到 {table} 的建表语句"
    return match.group(1)


def test_ai_trades_is_append_only_by_construction(ddl):
    """账本是 append-only：成交只增不改。

    只在 ``ai_trades`` 的表体里断言，不用整份文件——``ai_accounts.cash_balance``
    本来就必须可更新（买卖与申赎都会改现金），全局断言会误伤合法操作。

    这里只查 ``UPDATE``：``ON DELETE CASCADE`` 是外键子句，不是删除语句，
    查 ``DELETE`` 会误报。真正的 append-only 由服务层保证（``LedgerService``
    从不改已写入的成交），并由下面的唯一索引兜住重复写入。
    """
    body = _table_body(ddl, "ai_trades")
    assert not re.search(r"\bUPDATE\b", body, re.IGNORECASE)
    # 同一采纳的成交键必须唯一，防止"只成交一笔"的问题复发。
    assert re.search(
        r"UNIQUE INDEX IF NOT EXISTS uq_ai_trades_trade_key[\s\S]*?\(customer_id, trade_key\)",
        ddl,
        re.IGNORECASE,
    )


def test_allocation_plans_does_not_forbid_multiple_adoptions(ddl):
    """不可逆、新采纳 = 再调仓：不能有 status='adopted' 的唯一索引。"""
    assert not re.search(
        r"UNIQUE INDEX[\s\S]{0,200}?\(customer_id\)[\s\S]{0,200}?WHERE status = 'adopted'",
        ddl,
        re.IGNORECASE,
    ), "不允许对已采纳方案加唯一索引，否则第二次采纳会失败"


def test_snapshots_are_keyed_by_owner_and_date(ddl):
    assert re.search(
        r"PRIMARY KEY \(customer_id, owner, snapshot_date\)", ddl, re.IGNORECASE
    ), "快照表必须能同时容纳用户线与 AI 线"
    assert re.search(r"owner varchar\(8\)", ddl, re.IGNORECASE)
