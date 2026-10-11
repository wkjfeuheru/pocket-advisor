"""V2 迁移与 ORM 必须共享同一数据库契约。"""

import re

import pytest

from app.db.models.agent_session import AgentSession, AgentTurn
from app.db.schema import SCHEMA_APPLY_ORDER, _load_sql


def test_v2_migration_is_last_and_follows_all_referenced_tables():
    assert SCHEMA_APPLY_ORDER[-1] == "007_advisor_v2.sql"
    assert SCHEMA_APPLY_ORDER.index("006_portfolio_advisor.sql") < SCHEMA_APPLY_ORDER.index("007_advisor_v2.sql")


@pytest.mark.parametrize("table,column,declaration", [
    ("agent_sessions", "context", r"jsonb NOT NULL DEFAULT '\{\}'::jsonb"),
    ("agent_turns", "graph_version", r"varchar\(32\) NOT NULL DEFAULT 'legacy'"),
    ("agent_tool_evidence", "task_id", r"varchar\(128\) NOT NULL DEFAULT ''"),
    ("allocation_plans", "task_id", r"varchar\(128\) NOT NULL DEFAULT ''"),
])
def test_v2_columns_are_added_idempotently_with_legacy_safe_defaults(table, column, declaration):
    sql = _load_sql("007_advisor_v2.sql")
    assert re.search(rf"ALTER TABLE finance\.{table}\s+ADD COLUMN IF NOT EXISTS {column} {declaration}", sql)


def test_session_context_and_graph_version_are_mapped_with_safe_defaults():
    assert AgentSession.__table__.c.context.server_default is not None
    assert AgentTurn.__table__.c.graph_version.default.arg == "legacy"


def test_generation_snapshot_has_a_unique_trusted_key_and_json_response():
    from app.db.models.portfolio_advisor import AllocationGeneration

    sql = _load_sql("007_advisor_v2.sql")
    assert re.search(r"CREATE TABLE IF NOT EXISTS finance\.allocation_generations\s*\(", sql)
    assert re.search(r"PRIMARY KEY\s*\(customer_id, request_id, session_id, task_id\)", sql)
    assert re.search(r"response jsonb NOT NULL", sql)
    assert list(AllocationGeneration.__table__.primary_key.columns.keys()) == ["customer_id", "request_id", "session_id", "task_id"]
    assert AllocationGeneration.__table__.c.response.nullable is False
