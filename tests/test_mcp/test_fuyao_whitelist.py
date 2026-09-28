"""Fuyao 只读白名单与响应信封。"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.agents.tools.guard import is_forbidden_tool
from app.mcp.envelope import classify_upstream_error, wrap_envelope
from app.mcp.fuyao import FUYAO_OPERATIONS, FuyaoClient, select_whitelisted_tools


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_forbidden_names_are_not_in_the_whitelist():
    assert FUYAO_OPERATIONS
    for name in FUYAO_OPERATIONS:
        assert not is_forbidden_tool(name)
    assert "place_order" not in FUYAO_OPERATIONS
    assert "execute_sql" not in FUYAO_OPERATIONS


def test_whitelist_keeps_only_named_readonly_operations():
    selected = select_whitelisted_tools([
        _Tool("place_order"),
        _Tool("search_security"),
        _Tool("quote"),
        _Tool("unknown_probe"),
        _Tool("execute_sql"),
    ])
    assert set(selected) == {"search_security", "quote"}


def test_real_fuyao_tool_names_bind_to_internal_operations():
    """服务端实际发布的工具名必须映射，而不是依赖猜测式别名。"""
    selected = select_whitelisted_tools([
        _Tool("get_meta_tickers_search"),
        _Tool("get_meta_tickers_list"),
        _Tool("get_a_share_prices_snapshot"),
        _Tool("get_a_share_prices_historical"),
        _Tool("get_a_share_financials_indicators"),
        _Tool("get_a_share_valuations_snapshot"),
        _Tool("get_a_share_calendar_trading_days"),
        _Tool("get_a_share_index_catalog_ths_index_list"),
        _Tool("get_a_share_index_constituents_ths_stock_list"),
        _Tool("get_a_share_index_prices_snapshot"),
    ])
    assert {
        "search_security",
        "list_industries",
        "list_indices",
        "quote",
        "kline",
        "financials",
        "valuation",
        "trade_calendar",
        "index_members",
    } <= set(selected)


def test_real_quote_binding_translates_thscode_to_service_argument(monkeypatch):
    """内部 thscode 不能原样传给要求 thscodes 的真实快照工具。"""
    from app.mcp import fuyao as fuyao_module
    from app.mcp.fuyao import load_fuyao_client

    class _CaptureTool:
        name = "get_a_share_prices_snapshot"

        def __init__(self):
            self.received = None

        async def ainvoke(self, kwargs):
            self.received = kwargs
            return {"items": []}

    tool = _CaptureTool()

    async def _load(_api_key):
        return [tool], ""

    monkeypatch.setattr(fuyao_module, "load_stock_mcp_tools", _load)
    client, _warning = asyncio.run(load_fuyao_client("test-key"))
    assert client is not None
    asyncio.run(client.call("quote", thscode="600519.SH"))
    assert tool.received == {"thscodes": "600519.SH"}


def test_real_quote_content_block_becomes_sourced_quote_row():
    """LangChain MCP 的 text content block 必须解析成业务可用、可追溯的行情行。"""
    from app.mcp.fuyao_dto import normalize_payload

    payload = [{
        "type": "text",
        "text": json.dumps({
            "code": 0,
            "data": {
                "timestamp": "2026-09-27T14:00:00+08:00",
                "item": [{
                    "thscode": "600519.SH",
                    "ticker": "贵州茅台",
                    "last_price": 1400.0,
                    "price_change_ratio_pct": 0.5,
                }],
            },
        }),
    }]
    rows = normalize_payload("quote", payload)
    assert rows == [{
        "source": "fuyao",
        "thscode": "600519.SH",
        "name": "贵州茅台",
        "last": 1400.0,
        "change_pct": 0.5,
        "as_of": "2026-09-27T14:00:00+08:00",
    }]


def test_quote_operation_returns_one_normalized_row_for_one_thscode():
    """业务层按单只标的调用 quote 时不应收到 MCP 批量外壳。"""
    raw = [{
        "type": "text",
        "text": json.dumps({
            "data": {
                "timestamp": "2026-09-27T14:00:00+08:00",
                "item": [{
                    "thscode": "600519.SH",
                    "last_price": 1400.0,
                    "price_change_ratio_pct": 0.5,
                }],
            },
        }),
    }]

    async def quote(**_kwargs):
        return raw

    payload = asyncio.run(FuyaoClient({"quote": quote}).call("quote", thscode="600519.SH"))
    assert payload["data"] == {
        "source": "fuyao",
        "thscode": "600519.SH",
        "last": 1400.0,
        "change_pct": 0.5,
        "as_of": "2026-09-27T14:00:00+08:00",
    }


@pytest.mark.network
def test_live_fuyao_catalog_maps_required_readonly_tools():
    """只在显式开启时访问 Fuyao，防止真实目录再次悄然漂移。"""
    if os.getenv("RUN_NETWORK_TESTS") != "1":
        pytest.skip("set RUN_NETWORK_TESTS=1 to run Fuyao contract smoke test")
    api_key = os.getenv("FUYAO_API_KEY")
    if not api_key:
        pytest.skip("FUYAO_API_KEY is not configured")
    from app.mcp.fuyao import load_fuyao_client

    client, warning = asyncio.run(load_fuyao_client(api_key))
    assert warning in {"", "fuyao_tools_partial"}
    assert client is not None
    assert {"search_security", "quote", "kline", "financials", "valuation"} <= set(client.operations)


def test_envelope_marks_errors_and_redacts_secrets():
    ok = wrap_envelope({"price": 1.2, "as_of": "2026-09-27T14:00:00+08:00"})
    assert ok["ok"] is True
    assert ok["as_of"] == "2026-09-27T14:00:00+08:00"
    assert ok["data"]["price"] == 1.2
    timeout = wrap_envelope(TimeoutError("api_key=sk-secret timed out"))
    assert timeout["ok"] is False
    assert timeout["code"] == "timeout"
    assert "sk-secret" not in timeout["warning"]
    assert "api_key=[redacted]" in timeout["warning"] or "api_key" not in timeout["warning"].lower()
    missing = wrap_envelope(LookupError("not found"))
    assert missing["code"] == "not_found"
    limited = wrap_envelope(RuntimeError("429 rate limited"))
    assert limited["code"] == "rate_limited"


def test_client_call_uses_envelope_and_unknown_ops_fail():
    async def quote(**kwargs):
        return {"last": 10.5, "as_of": "2026-09-27T14:01:00+08:00"}

    client = FuyaoClient({"quote": quote})
    assert client.operations == ["quote"]
    payload = asyncio.run(client.call("quote", thscode="600519.SH"))
    assert payload["ok"] is True
    assert payload["data"]["last"] == 10.5
    unknown = asyncio.run(client.call("kline", thscode="600519.SH"))
    assert unknown["ok"] is False
    assert unknown["code"] == "unavailable"


def test_empty_key_still_reports_unconfigured():
    from app.mcp.fuyao import load_fuyao_client

    client, warning = asyncio.run(load_fuyao_client(""))
    assert client is None
    assert warning == "fuyao_unconfigured"


def test_quote_and_members_drop_unknown_fields():
    from app.mcp.fuyao_dto import normalize_payload

    quote = normalize_payload("quote", {
        "price": 10.5,
        "pct_chg": 1.2,
        "update_time": "2026-09-27T14:00:00+08:00",
        "api_key": "secret",
    })
    assert quote["last"] == 10.5
    assert quote["change_pct"] == 1.2
    assert quote["source"] == "fuyao"
    assert quote["as_of"] == "2026-09-27T14:00:00+08:00"
    assert "api_key" not in quote
    members = normalize_payload("index_members", {
        "index_code": "000300.SH",
        "con_code": ["600519.SH", "000001.SZ"],
        "token": "hidden",
    })
    assert members["thscode"] == "000300.SH"
    assert members["members"] == ["600519.SH", "000001.SZ"]
    assert "token" not in members


def test_classify_upstream_error_does_not_keep_tokens():
    text = classify_upstream_error(RuntimeError("token=abc123 unavailable"))
    assert "abc123" not in text
    assert text in {"timeout", "rate_limited", "unavailable", "not_found"}


def test_mcp_adapter_and_transport_load_with_the_configured_servers():
    """启动冒烟：版本约束下 mcp 与 langchain-mcp-adapters 必须真的能构造客户端。"""
    import mcp  # noqa: F401
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.mcp.mcp_client import _SERVERS, _server_config

    assert _SERVERS
    config = {name: _server_config(url, "test-key") for name, url in _SERVERS.items()}
    assert {item["transport"] for item in config.values()} == {"streamable_http"}
    assert all(item["headers"]["X-api-key"] == "test-key" for item in config.values())
    assert MultiServerMCPClient(config) is not None


def test_mcp_connection_failure_degrades_without_leaking_the_key(monkeypatch, caplog):
    import app.mcp.mcp_client as mcp_client

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("connect failed api_key=sk-secret")

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _Boom)
    with caplog.at_level("WARNING"):
        tools, warning = asyncio.run(mcp_client.load_stock_mcp_tools("sk-secret"))
    assert tools == []
    assert warning == "fuyao_unavailable"
    assert "sk-secret" not in caplog.text


def test_loaded_tools_become_whitelisted_operations_only(monkeypatch):
    """真实加载路径只把白名单内的只读操作交给业务层，模型拿不到工具对象。"""
    from app.mcp import fuyao as fuyao_module
    from app.mcp.fuyao import load_fuyao_client

    class _Tool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, kwargs):
            return {"thscode": kwargs.get("thscode"), "price": 10.5,
                    "update_time": "2026-09-27T14:00:00+08:00"}

    class _Client:
        def __init__(self, config):
            self.config = config

        async def get_tools(self):
            return [_Tool("place_order"), _Tool("quote"), _Tool("unknown_probe"), _Tool("execute_sql")]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _Client)
    client, warning = asyncio.run(load_fuyao_client("test-key"))
    assert warning == "fuyao_tools_partial"
    assert client is not None
    assert client.operations == ["quote"]

    envelope = asyncio.run(client.call("quote", thscode="600519.SH"))
    assert envelope["ok"] is True
    assert envelope["data"]["last"] == 10.5
    forbidden = asyncio.run(client.call("place_order", thscode="600519.SH"))
    assert forbidden["ok"] is False
    assert forbidden["code"] == "unavailable"
    assert fuyao_module.FUYAO_OPERATIONS


def test_whitelist_diagnostics_names_the_unmapped_operations():
    """发现成功但对不上内部操作名时，必须能说出缺了哪些、上游给了哪些。"""
    from app.mcp.fuyao import FUYAO_OPERATIONS, whitelist_diagnostics

    class _Tool:
        def __init__(self, name):
            self.name = name

    diagnostics = whitelist_diagnostics([_Tool("stock_quote"), _Tool("stock_kline")])
    assert diagnostics["discovered"] == ["stock_kline", "stock_quote"]
    assert diagnostics["mapped"] == []
    assert diagnostics["unmapped"] == sorted(FUYAO_OPERATIONS)


def test_partially_mapped_tools_report_the_missing_operations():
    from app.mcp.fuyao import whitelist_diagnostics

    class _Tool:
        def __init__(self, name):
            self.name = name

    diagnostics = whitelist_diagnostics([_Tool("quote"), _Tool("kline")])
    assert diagnostics["mapped"] == ["kline", "quote"]
    assert "quote" not in diagnostics["unmapped"]
    assert "index_members" in diagnostics["unmapped"]


def test_unmapped_tool_names_fail_loudly_instead_of_returning_a_dead_client(monkeypatch, caplog):
    """对不上名字时不能返回一个"每次调用都失败"的客户端，真实原因会被误读成上游故障。"""
    from app.mcp.fuyao import load_fuyao_client

    class _Tool:
        def __init__(self, name):
            self.name = name

    class _Client:
        def __init__(self, config):
            self.config = config

        async def get_tools(self):
            return [_Tool("stock_quote"), _Tool("stock_kline"), _Tool("stock_finance")]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _Client)
    with caplog.at_level("WARNING"):
        client, warning = asyncio.run(load_fuyao_client("test-key"))
    assert client is None
    assert warning == "fuyao_tools_unmapped"
    assert "stock_quote" in caplog.text
    assert "search_security" in caplog.text
    assert "test-key" not in caplog.text


def test_fully_mapped_tools_report_no_warning(monkeypatch):
    from app.mcp.fuyao import FUYAO_OPERATIONS, load_fuyao_client

    class _Tool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, kwargs):
            return {}

    class _Client:
        def __init__(self, config):
            self.config = config

        async def get_tools(self):
            return [_Tool(name) for name in FUYAO_OPERATIONS]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _Client)
    client, warning = asyncio.run(load_fuyao_client("test-key"))
    assert warning == ""
    assert client is not None
    assert client.unmapped == []
    assert set(client.operations) == set(FUYAO_OPERATIONS)
