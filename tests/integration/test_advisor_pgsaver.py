"""真实 PostgreSQL 下验证 PgSaver 可跨 runtime 实例恢复 checkpoint。"""

from __future__ import annotations

import asyncio
import os
import selectors
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint


def test_postgres_saver_restores_checkpoint_across_runtime_instances(monkeypatch):
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要 RUN_POSTGRES_INTEGRATION=1 和可访问的真实 PostgreSQL")

    from app.agents import runtime

    thread_id = f"pgsaver-integration-{uuid4()}"
    settings = SimpleNamespace(
        api_key="integration-test",
        fuyao_api_key="",
        turn_timeout_seconds=120,
        context_message_limit=20,
    )

    async def no_fuyao(_api_key, **_kwargs):
        return [], "fuyao_unconfigured"

    monkeypatch.setattr(runtime, "load_advisor_settings", lambda: settings)
    monkeypatch.setattr(runtime, "build_chat_model", lambda _settings: object())
    monkeypatch.setattr(runtime, "load_stock_mcp_tools", no_fuyao)
    monkeypatch.setattr(runtime, "load_tushare_client", lambda _token: (object(), "tushare_unconfigured"))
    monkeypatch.setattr(runtime, "get_agent_evidence_store", lambda: SimpleNamespace(cleanup_expired=lambda: 0))
    monkeypatch.setattr(runtime, "build_stock_local_tools", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "build_evidence_middleware", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "build_stock_agent", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "get_product_library", lambda: object())
    monkeypatch.setattr(runtime, "build_specialists", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime, "build_advisor_graph", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "_mark_stale", lambda _timeout: None)
    monkeypatch.setattr("app.services.portfolio_service.get_portfolio_service", lambda: object())

    async def exercise_cross_instance_restore():
        first = await runtime.init_agents()
        assert first.status == "ready", first.reason
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        message_version = first.checkpointer.get_next_version(None, None)
        checkpoint["channel_values"] = {"messages": ["persisted-across-instances"]}
        checkpoint["channel_versions"] = {"messages": message_version}
        saved_config = await first.checkpointer.aput(
            config,
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {"messages": message_version},
        )
        await runtime.close_agents()

        second = await runtime.init_agents()
        assert second.status == "ready", second.reason
        try:
            restored = await second.checkpointer.aget_tuple(saved_config)
            assert restored is not None
            assert restored.checkpoint["id"] == checkpoint["id"]
            assert restored.checkpoint["channel_values"] == {
                "messages": ["persisted-across-instances"]
            }
        finally:
            async with second.pool.connection() as connection:
                await connection.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,)
                )
                await connection.execute(
                    "DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,)
                )
                await connection.execute(
                    "DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,)
                )
            await runtime.close_agents()

    asyncio.run(
        exercise_cross_instance_restore(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )
