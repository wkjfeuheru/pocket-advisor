"""投顾配置、迁移契约，以及生产代码不引用参考目录。"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def _stub_agent_initialization(monkeypatch, *, pool_class, saver_class, mark_stale):
    """隔离外部服务，只保留 runtime 初始化编排供回归测试观察。"""
    from app.agents import runtime
    import langgraph.checkpoint.postgres.aio
    import psycopg_pool

    settings = SimpleNamespace(
        api_key="test-key",
        fuyao_api_key="",
        turn_timeout_seconds=120,
        context_message_limit=20,
    )
    monkeypatch.setattr(runtime, "load_advisor_settings", lambda: settings)
    monkeypatch.setattr(runtime, "build_chat_model", lambda _settings: object())
    monkeypatch.setattr(runtime, "postgres_dsn", lambda: "postgresql://test")
    monkeypatch.setattr(runtime, "load_stock_mcp_tools", lambda _key, **_kwargs: _async_value(([], "fuyao_unconfigured")))
    monkeypatch.setattr(runtime, "load_tushare_client", lambda _token: (object(), "tushare_unconfigured"))
    monkeypatch.setattr(runtime, "get_agent_evidence_store", lambda: SimpleNamespace(cleanup_expired=lambda: 0))
    monkeypatch.setattr(runtime, "build_stock_local_tools", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "build_evidence_middleware", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "build_stock_agent", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "get_product_library", lambda: object())
    monkeypatch.setattr(runtime, "build_specialists", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime, "build_advisor_graph", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "_mark_stale", mark_stale)
    monkeypatch.setattr("app.services.portfolio_service.get_portfolio_service", lambda: object())
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", pool_class)
    monkeypatch.setattr(langgraph.checkpoint.postgres.aio, "AsyncPostgresSaver", saver_class)


async def _empty_fuyao(_api_key):
    return None, "fuyao_unconfigured"


async def _async_value(value):
    return value


class _OpenablePool:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def open(self):
        return None

    async def close(self):
        return None


class _Saver:
    def __init__(self, pool):
        self.pool = pool

    async def setup(self):
        return None


def test_init_configures_postgres_pool_with_dict_rows(monkeypatch):
    from psycopg.rows import dict_row
    from app.agents.runtime import close_agents, init_agents

    pools = []

    class _Pool(_OpenablePool):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)

    _stub_agent_initialization(monkeypatch, pool_class=_Pool, saver_class=_Saver, mark_stale=lambda _: None)

    async def run():
        try:
            return await init_agents()
        finally:
            await close_agents()

    runtime = asyncio.run(run())
    assert runtime.status == "ready"
    assert pools[0].kwargs["kwargs"]["row_factory"] is dict_row


def test_init_runs_stale_cleanup_off_the_event_loop_thread(monkeypatch):
    from app.agents.runtime import close_agents, init_agents

    loop_thread = threading.get_ident()
    cleanup_threads = []
    _stub_agent_initialization(
        monkeypatch,
        pool_class=_OpenablePool,
        saver_class=_Saver,
        mark_stale=lambda _timeout: cleanup_threads.append(threading.get_ident()),
    )

    async def run():
        try:
            return await init_agents()
        finally:
            await close_agents()

    runtime = asyncio.run(run())
    assert runtime.status == "ready"
    assert len(cleanup_threads) == 1
    assert cleanup_threads[0] != loop_thread


def test_init_builds_stock_react_agent_from_dynamic_and_local_tools(monkeypatch):
    from app.agents import runtime
    from app.agents.runtime import close_agents, init_agents

    mcp_tool = SimpleNamespace(name="fuyao_a_share_get_quote")
    local_tool = SimpleNamespace(name="calculate_technical_indicators")
    built = {}
    _stub_agent_initialization(
        monkeypatch,
        pool_class=_OpenablePool,
        saver_class=_Saver,
        mark_stale=lambda _timeout: None,
    )
    monkeypatch.setattr(runtime, "load_stock_mcp_tools", lambda _key, **kwargs: _async_value(([mcp_tool], "")))
    monkeypatch.setattr(runtime, "build_stock_local_tools", lambda **_kwargs: [local_tool])

    def capture(model, mcp_tools, local_tools, **kwargs):
        built.update({"mcp": mcp_tools, "local": local_tools, **kwargs})
        return object()

    monkeypatch.setattr(runtime, "build_stock_agent", capture)

    async def run():
        try:
            return await init_agents()
        finally:
            await close_agents()

    initialized = asyncio.run(run())
    assert initialized.status == "ready"
    assert built["mcp"] == [mcp_tool]
    assert built["local"] == [local_tool]
    assert built["checkpointer"] is initialized.checkpointer


def test_stock_agent_build_failure_only_degrades_stock_branch(monkeypatch):
    from app.agents import runtime
    from app.agents.runtime import close_agents, init_agents

    captured = {}
    _stub_agent_initialization(
        monkeypatch,
        pool_class=_OpenablePool,
        saver_class=_Saver,
        mark_stale=lambda _timeout: None,
    )
    monkeypatch.setattr(runtime, "build_stock_agent", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stock boom")))

    def specialists(_model, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(runtime, "build_specialists", specialists)

    async def run():
        try:
            return await init_agents()
        finally:
            await close_agents()

    initialized = asyncio.run(run())
    assert initialized.status == "ready"
    assert captured["stock_agent"] is None
    assert "stock_agent_unavailable" in captured["stock_warning"]


def test_advisor_settings_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv("ADVISOR_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL_NAME", raising=False)
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_TURN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGENT_MAX_MESSAGE_CHARS", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_MESSAGE_LIMIT", raising=False)
    from app.core.settings_models import load_advisor_settings

    settings = load_advisor_settings()
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.model_name == "deepseek-chat"
    assert settings.turn_timeout_seconds == 120
    assert settings.max_message_chars == 4000
    assert settings.context_message_limit == 20
    assert settings.api_key == ""

    monkeypatch.setenv("AGENT_TURN_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        load_advisor_settings()


def test_init_failure_is_degraded_and_redacted(monkeypatch):
    from app.agents.runtime import close_agents, init_agents

    monkeypatch.setenv("ADVISOR_MODEL_API_KEY", "sk-test")

    class _Pool:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("connect postgresql://user:s3cret@db/advisor failed")

    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _Pool)

    async def run():
        try:
            return await init_agents()
        finally:
            await close_agents()

    import asyncio

    runtime = asyncio.run(run())
    assert runtime.status == "degraded"
    assert "s3cret" not in runtime.reason
    assert "postgresql://" not in runtime.reason


def test_app_package_does_not_import_reference_tree():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "FAVersion2" or name.startswith("FAVersion2.") for name in names):
                offenders.append(str(path))
    assert offenders == []


def test_advisor_migration_is_idempotent_and_isolates_users():
    from app.db.schema import SCHEMA_APPLY_ORDER, _load_sql

    assert SCHEMA_APPLY_ORDER.index("003_advisor.sql") > SCHEMA_APPLY_ORDER.index("001_schema.sql")
    sql = _load_sql("003_advisor.sql")
    assert "CREATE TABLE IF NOT EXISTS finance.agent_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS finance.agent_messages" in sql
    assert "CREATE TABLE IF NOT EXISTS finance.agent_turns" in sql
    assert "CREATE TABLE IF NOT EXISTS finance.advisor_profiles" in sql
    assert "uq_agent_messages_request_role" in sql
    assert "uq_agent_turns_one_active" in sql
    assert "REFERENCES finance.users(customer_id)" in sql
    assert "pending" in sql and "cancelled" in sql
    assert "R1" in sql and "均衡增长" in sql
    assert "budget_amount > 0" in sql
    assert "finance.checkpoint" not in sql


def test_registered_models_match_the_migration():
    from app.db.base import Base, load_all_models

    load_all_models()
    names = {table.name for table in Base.metadata.tables.values()}
    assert {"agent_sessions", "agent_messages", "agent_turns", "advisor_profiles", "agent_tool_evidence"} <= names
    assert not any(name.startswith("checkpoint_") for name in names)


def test_agent_evidence_migration_has_ttl_and_request_indexes():
    from app.db.schema import SCHEMA_APPLY_ORDER, _load_sql

    # 只断言 005 已注册且在 003 之后，不要求它是最后一个——
    # 后续迁移（如 006_portfolio_advisor.sql）本来就会排到它后面。
    assert "005_agent_evidence.sql" in SCHEMA_APPLY_ORDER
    assert SCHEMA_APPLY_ORDER.index("005_agent_evidence.sql") > SCHEMA_APPLY_ORDER.index(
        "003_advisor.sql"
    )
    sql = _load_sql("005_agent_evidence.sql")
    assert "CREATE TABLE IF NOT EXISTS finance.agent_tool_evidence" in sql
    assert "REFERENCES finance.agent_turns(request_id) ON DELETE CASCADE" in sql
    assert "idx_agent_tool_evidence_request" in sql
    assert "idx_agent_tool_evidence_expires" in sql


@pytest.mark.network
def test_live_model_smoke_is_opt_in():
    import os

    if os.getenv("RUN_NETWORK_TESTS") != "1" or not os.getenv("ADVISOR_MODEL_API_KEY"):
        pytest.skip("需要 RUN_NETWORK_TESTS=1 且已配置 ADVISOR_MODEL_API_KEY")
    from app.agents.model import build_chat_model

    model = build_chat_model()
    assert model is not None
