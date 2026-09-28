"""Agent 工具只读、身份隔离，以及行情上游降级。"""

from __future__ import annotations

import asyncio
import inspect

from app.agents.tools import ALL_TOOLS
from app.agents.tools.database import (
    bind_readonly_services,
    get_account_snapshot,
    list_positions,
    reset_readonly_services,
)
from app.agents.tools.guard import filter_mcp_tools, is_forbidden_tool, select_readonly_tools


class _Tool:
    def __init__(self, name: str, *, metadata=None, args=None) -> None:
        self.name = name
        self.metadata = metadata
        self.args = args or {}


class _Portfolio:
    def __init__(self) -> None:
        self.customers: list[str] = []

    def get_account(self, customer_id: str):
        self.customers.append(customer_id)
        return {"customer_id": customer_id, "cash_balance": 10, "updated_at": "2026-09-27"}

    def list_positions(self, customer_id: str):
        self.customers.append(customer_id)
        return []


def test_static_registry_has_no_trading_or_sql_tools():
    names = [tool.name for tool in ALL_TOOLS]
    assert names
    assert all(not is_forbidden_tool(name) for name in names)
    for tool in ALL_TOOLS:
        assert "customer_id" not in (tool.args or {})


def test_mcp_write_tools_are_dropped():
    kept = select_readonly_tools([
        _Tool("get_quote", metadata={"readOnlyHint": True}),
        _Tool("place_order"),
        _Tool("execute_sql"),
        _Tool("清仓"),
    ])
    assert [tool.name for tool in kept] == ["get_quote"]


def test_mcp_tools_fail_closed_on_annotations_schema_and_server_source():
    accepted, rejected = filter_mcp_tools([
        _Tool(
            "fuyao_a_share_get_quote",
            metadata={"readOnlyHint": True, "destructiveHint": False},
            args={"thscode": {"type": "string"}},
        ),
        _Tool("fuyao_a_share_unknown_probe", args={"thscode": {"type": "string"}}),
        _Tool(
            "fuyao_a_share_get_quote_with_sql",
            metadata={"readOnlyHint": True},
            args={"sql": {"type": "string"}},
        ),
        _Tool(
            "untrusted_get_quote",
            metadata={"readOnlyHint": True},
            args={"thscode": {"type": "string"}},
        ),
    ], allowed_server_prefixes=("fuyao_a_share_",))

    assert [tool.name for tool in accepted] == ["fuyao_a_share_get_quote"]
    assert rejected == {
        "fuyao_a_share_unknown_probe": "missing_readonly_annotation",
        "fuyao_a_share_get_quote_with_sql": "dangerous_schema",
        "untrusted_get_quote": "untrusted_server",
    }


def test_mcp_duplicate_names_are_all_rejected():
    first = _Tool("fuyao_meta_search", metadata={"readOnlyHint": True})
    second = _Tool("fuyao_meta_search", metadata={"readOnlyHint": True})

    accepted, rejected = filter_mcp_tools(
        [first, second], allowed_server_prefixes=("fuyao_meta_",)
    )

    assert accepted == []
    assert rejected == {"fuyao_meta_search": "duplicate_name"}


def test_account_tool_uses_trusted_customer_only():
    portfolio = _Portfolio()
    tokens = bind_readonly_services(library=None, portfolio=portfolio)
    try:
        payload = get_account_snapshot.invoke(
            {},
            config={"configurable": {"customer_id": "cust000001"}},
        )
        positions = list_positions.invoke(
            {},
            config={"configurable": {"customer_id": "cust000001"}},
        )
    finally:
        reset_readonly_services(tokens)
    assert portfolio.customers == ["CUST000001", "CUST000001"]
    assert "cust000002" not in payload
    assert "未编造" not in positions
    assert "customer_id" not in inspect.signature(get_account_snapshot.func).parameters


def test_stock_loader_reports_a_warning_without_a_key():
    from app.mcp.mcp_client import load_stock_mcp_tools

    tools, warning = asyncio.run(load_stock_mcp_tools(""))
    assert tools == []
    assert warning == "fuyao_unconfigured"


def test_strict_stock_loader_prefixes_servers_and_rejects_unannotated_tools(monkeypatch):
    from app.mcp.mcp_client import load_stock_mcp_tools

    created = {}

    class _Client:
        def __init__(self, connections, **kwargs):
            created.update(kwargs)

        async def get_tools(self):
            return [
                _Tool("fuyao_a_share_get_quote", metadata={"readOnlyHint": True}),
                _Tool("fuyao_meta_unknown_probe"),
            ]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _Client)
    tools, warning = asyncio.run(load_stock_mcp_tools("test-key", strict=True))

    assert created["tool_name_prefix"] is True
    assert [item.name for item in tools] == ["fuyao_a_share_get_quote"]
    assert warning == ""
