"""Stock ReAct Agent 的装配与服务端证据契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def _context(request_id: str = "11111111-1111-1111-1111-111111111111"):
    from app.agents.stock.evidence_store import EvidenceContext

    return EvidenceContext(
        request_id=request_id,
        session_id="22222222-2222-2222-2222-222222222222",
        customer_id="CUST000001",
    )


def test_memory_evidence_store_expires_and_isolates_requests():
    from app.agents.stock.evidence_store import EvidenceAccessError, MemoryAgentEvidenceStore

    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    store = MemoryAgentEvidenceStore(clock=lambda: now)
    evidence = store.put(
        _context(),
        tool_call_id="call-1",
        tool_name="fuyao_a_share_get_quote",
        evidence_type="quote",
        payload={"thscode": "600519.SH", "last": 100.0, "api_key": "secret"},
        source="fuyao",
        as_of="2026-09-28T10:00:00+08:00",
    )

    loaded = store.get(evidence["id"], _context())
    assert loaded["payload"] == {"thscode": "600519.SH", "last": 100.0, "api_key": "[redacted]"}
    assert loaded["expires_at"] == (now + timedelta(hours=24)).isoformat()

    with pytest.raises(EvidenceAccessError):
        store.get(evidence["id"], _context("33333333-3333-3333-3333-333333333333"))

    store._clock = lambda: now + timedelta(hours=25)
    assert store.cleanup_expired() == 1
    with pytest.raises(EvidenceAccessError):
        store.get(evidence["id"], _context())


def test_evidence_preview_limits_rows_and_drops_news_body():
    from app.agents.stock.evidence_store import compact_evidence_preview, sanitize_evidence_payload

    payload = {
        "rows": [{"n": index} for index in range(25)],
        "headers": {"Authorization": "Bearer secret"},
        "body": "full copyrighted article",
        "title": "标题",
    }

    sanitized = sanitize_evidence_payload(payload)
    preview = compact_evidence_preview(sanitized, "evidence-1")

    assert len(preview["preview"]["rows"]) == 20
    assert preview["preview"]["headers"] == "[redacted]"
    assert preview["preview"]["body"] == "[redacted]"
    assert preview["evidence_id"] == "evidence-1"


def test_build_stock_agent_uses_create_agent_with_limits_and_structured_response(monkeypatch):
    from app.agents.stock import agent as stock_agent_module
    from app.agents.stock.contracts import StockAgentResponse

    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "compiled-stock-agent"

    monkeypatch.setattr(stock_agent_module, "create_agent", fake_create_agent)
    mcp = SimpleNamespace(name="fuyao_a_share_get_quote")
    local = SimpleNamespace(name="calculate_technical_indicators")

    built = stock_agent_module.build_stock_agent(object(), [mcp], [local])

    assert built == "compiled-stock-agent"
    assert captured["tools"] == [mcp, local]
    assert captured["response_format"] is StockAgentResponse
    assert captured["name"] == "stock_agent"
    assert "market_overview" in captured["system_prompt"]
    assert "equity_research" in captured["system_prompt"]
    assert "stock_screening" in captured["system_prompt"]
    limits = [item for item in captured["middleware"] if item.__class__.__name__ == "ToolCallLimitMiddleware"]
    assert [(item.tool_name, item.run_limit, item.exit_behavior) for item in limits] == [(None, 20, "end")]
    model_limits = [item for item in captured["middleware"] if item.__class__.__name__ == "ModelCallLimitMiddleware"]
    assert [(item.run_limit, item.exit_behavior) for item in model_limits] == [(20, "end")]


def test_mcp_result_is_persisted_and_model_only_receives_compact_handle():
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.middleware import persist_tool_output

    store = MemoryAgentEvidenceStore()
    result = persist_tool_output(
        store,
        _context(),
        tool_name="fuyao_a_share_prices_history",
        tool_call_id="call-kline",
        arguments={"thscode": "600519.SH"},
        result={
            "data": [{"close": index, "as_of": f"2026-09-{index + 1:02d}"} for index in range(25)],
            "source": "fuyao",
            "as_of": "2026-09-25T15:00:00+08:00",
        },
    )

    assert result["evidence_id"]
    assert len(result["preview"]["data"]) == 20
    stored = store.get(result["evidence_id"], _context())
    assert len(stored["payload"]["data"]) == 25
    assert stored["evidence_type"] == "kline"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            lambda: __import__("langchain_core.messages", fromlist=["ToolMessage"]).ToolMessage(
                content="ok",
                artifact={"structured_content": {"data": [{"close": 10.0}]}},
                tool_call_id="call-structured",
            ),
            {"data": [{"close": 10.0}]},
        ),
        (
            lambda: __import__("langchain_core.messages", fromlist=["ToolMessage"]).ToolMessage(
                content=[{"type": "text", "text": '{"data":[{"close":11.0}]}'}],
                tool_call_id="call-block",
            ),
            {"data": [{"close": 11.0}]},
        ),
    ],
)
def test_mcp_adapter_tool_message_shapes_are_unwrapped(message, expected):
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.middleware import persist_tool_output

    store = MemoryAgentEvidenceStore()
    preview = persist_tool_output(
        store,
        _context(),
        tool_name="fuyao_a_share_prices_history",
        tool_call_id="adapter-call",
        arguments={"thscode": "600519.SH"},
        result=message(),
    )

    payload = store.get(preview["evidence_id"], _context())["payload"]
    assert payload["data"][0]["close"] == expected["data"][0]["close"]


def test_real_fuyao_quote_shape_is_normalized_before_persistence():
    from langchain_core.messages import ToolMessage

    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.middleware import persist_tool_output

    store = MemoryAgentEvidenceStore()
    message = ToolMessage(
        content="ok",
        artifact={"structured_content": {
            "data": {
                "timestamp": "2026-09-28T10:00:00+08:00",
                "item": [{
                    "thscode": "000001.SH",
                    "price": 3200.5,
                    "price_change_ratio_pct": 0.8,
                }],
            },
        }},
        tool_call_id="real-quote",
    )
    preview = persist_tool_output(
        store,
        _context(),
        tool_name="fuyao_a_share_index_get_a_share_index_prices_snapshot",
        tool_call_id="real-quote",
        arguments={"thscodes": "000001.SH"},
        result=message,
    )

    stored = store.get(preview["evidence_id"], _context())
    assert stored["evidence_type"] == "quote"
    assert stored["payload"]["data"] == [{
        "thscode": "000001.SH",
        "last": 3200.5,
        "change_pct": 0.8,
        "as_of": "2026-09-28T10:00:00+08:00",
        "source": "fuyao",
    }]


def test_technical_tool_reads_evidence_handle_and_persists_derived_result():
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.tools import (
        bind_stock_execution,
        build_stock_local_tools,
        reset_stock_execution,
    )

    store = MemoryAgentEvidenceStore()
    source = store.put(
        _context(),
        tool_call_id="call-kline",
        tool_name="fuyao_a_share_prices_history",
        evidence_type="kline",
        payload={
            "thscode": "600519.SH",
            "data": [
                {"close": 100 + index, "high": 101 + index, "low": 99 + index, "volume": 1000 + index,
                 "as_of": f"2026-09-{index + 1:02d}"}
                for index in range(20)
            ],
            "source": "fuyao",
            "as_of": "2026-09-20T15:00:00+08:00",
        },
        source="fuyao",
        as_of="2026-09-20T15:00:00+08:00",
    )
    tools = {tool.name: tool for tool in build_stock_local_tools(store=store)}
    token = bind_stock_execution(_context())
    try:
        result = tools["calculate_technical_indicators"].invoke({"kline_evidence_id": source["id"]})
    finally:
        reset_stock_execution(token)

    derived = store.get(result["evidence_id"], _context())
    assert derived["evidence_type"] == "technicals"
    assert derived["payload"]["thscode"] == "600519.SH"
    assert derived["payload"]["status"] == "ok"


def test_screening_tool_owns_the_candidate_order():
    from datetime import date

    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.tools import bind_stock_execution, build_stock_local_tools, reset_stock_execution
    from app.crud.stock_feature_crud import MemoryStockFeatureStore

    warehouse = MemoryStockFeatureStore()
    warehouse.refresh(date.today(), rows=[
        {"thscode": "000001.SZ", "name": "A", "industry": "银行", "trade_status": "交易",
         "listed_days": 1000, "avg_turnover_20": 100_000_000, "net_assets": 1, "earnings_quality": 0.8,
         "cashflow_quality": 0.9, "valuation": 8, "stability": 0.9},
        {"thscode": "600000.SH", "name": "B", "industry": "银行", "trade_status": "交易",
         "listed_days": 1000, "avg_turnover_20": 100_000_000, "net_assets": 1, "earnings_quality": 0.7,
         "cashflow_quality": 0.7, "valuation": 12, "stability": 0.7},
    ])
    store = MemoryAgentEvidenceStore()
    membership = store.put(
        _context(),
        tool_call_id="index-members",
        tool_name="fuyao_a_share_index_get_a_share_index_constituents_ths_stock_list",
        evidence_type="index_members",
        payload={"thscode": "000300.SH", "members": ["000001.SZ"]},
        source="fuyao",
        as_of="2026-09-28",
    )
    tools = {tool.name: tool for tool in build_stock_local_tools(store=store, warehouse=warehouse)}
    token = bind_stock_execution(_context(), profile={"risk_level": "R3"})
    try:
        result = tools["score_stock_candidates"].invoke({
            "strategy": "value_quality",
            "horizon": "medium",
            "industries": ["银行"],
            "index_scope": [],
            "candidate_count": 2,
            "filter_by_profile": True,
        })
        scoped = tools["score_stock_candidates"].invoke({
            "strategy": "value_quality",
            "horizon": "medium",
            "industries": [],
            "index_scope": ["000300.SH"],
            "candidate_count": 2,
            "filter_by_profile": False,
            "membership_evidence_ids": [membership["id"]],
        })
    finally:
        reset_stock_execution(token)

    evidence = store.get(result["evidence_id"], _context())
    assert evidence["evidence_type"] == "stock_screening"
    assert [item["thscode"] for item in evidence["payload"]["candidates"]] == [
        "000001.SZ", "600000.SH",
    ]
    scoped_evidence = store.get(scoped["evidence_id"], _context())
    assert [item["thscode"] for item in scoped_evidence["payload"]["candidates"]] == ["000001.SZ"]


def test_server_assembles_artifact_from_evidence_and_suppresses_unsupported_numbers():
    from app.agents.stock.assembler import assemble_stock_result
    from app.agents.stock.contracts import StockAgentResponse
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore

    store = MemoryAgentEvidenceStore()
    market = store.put(
        _context(), tool_call_id="local-market", tool_name="aggregate_market_snapshot",
        evidence_type="market_overview", source="fuyao", as_of="2026-09-28T10:00:00+08:00",
        payload={
            "failed": False,
            "indices": [{"thscode": "000001.SH", "name": "上证指数", "last": 3200.0,
                         "change_pct": 1.2, "source": "fuyao", "as_of": "2026-09-28T10:00:00+08:00"}],
            "breadth": {}, "sectors": {"industry": [], "concept": []},
            "sources": ["fuyao"], "warnings": [], "as_of": "2026-09-28T10:00:00+08:00",
        },
    )
    response = StockAgentResponse(
        scenario="market_overview", summary="市场有效数据已获取，但模型声称 9999 点",
        evidence_ids=[market["id"]], evidence_quality="ok", failed=False,
    )
    rendered = assemble_stock_result(response, _context(), store)
    assert rendered.result is not None
    assert rendered.result.market_overview["indices"][0]["last"] == 3200.0
    assert "9999" not in rendered.summary

    unsupported = StockAgentResponse(
        scenario="market_overview", summary="上证指数 9999 点",
        evidence_ids=[], evidence_quality="ok", failed=False,
    )
    degraded = assemble_stock_result(unsupported, _context(), store)
    assert degraded.result is not None and degraded.result.evidence_quality == "failed"
    assert degraded.result.market_overview["indices"] == []
    assert degraded.candidates == []
    assert degraded.status == "degraded"
    assert "9999" not in degraded.summary


@pytest.mark.parametrize(
    ("scenario", "evidence_type", "payload"),
    [
        (
            "market_overview",
            "market_overview",
            {"failed": True, "indices": [], "warnings": ["market_evidence_incomplete"]},
        ),
        (
            "equity_research",
            "events",
            {"thscode": "600519.SH", "status": "unavailable", "events": [], "source": "tushare"},
        ),
        (
            "equity_research",
            "fundamentals",
            {"thscode": "600519.SH", "roe": None, "gross_margin": None, "source": "fuyao", "as_of": "2026-09-28"},
        ),
    ],
)
def test_unusable_evidence_returns_a_nonfatal_generic_answer(scenario, evidence_type, payload):
    from app.agents.stock.assembler import assemble_stock_result
    from app.agents.stock.contracts import StockAgentResponse
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore

    store = MemoryAgentEvidenceStore()
    row = store.put(
        _context(), tool_call_id="bad-evidence", tool_name="local",
        evidence_type=evidence_type, payload=payload, source=str(payload.get("source") or "fuyao"),
        as_of=str(payload.get("as_of") or ""),
    )
    result = assemble_stock_result(StockAgentResponse(
        scenario=scenario,
        summary="模型不应保留的个股结论",
        evidence_ids=[row["id"]],
        evidence_quality="failed",
        failed=True,
    ), _context(), store)

    assert result.status == "degraded"
    assert result.result is not None and result.result.evidence_quality == "failed"
    assert result.result.scenario == scenario
    assert result.candidates == []
    assert "个股结论" not in result.summary


def test_fundamental_tool_rejects_mixed_symbols():
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore
    from app.agents.stock.tools import bind_stock_execution, build_stock_local_tools, reset_stock_execution

    store = MemoryAgentEvidenceStore()
    ids = []
    for code in ("600519.SH", "000001.SZ"):
        row = store.put(
            _context(), tool_call_id=code, tool_name="financials", evidence_type="financials",
            payload={"thscode": code, "roe": 0.1, "source": "fuyao", "as_of": "2026-09-28"},
            source="fuyao", as_of="2026-09-28",
        )
        ids.append(row["id"])
    tools = {item.name: item for item in build_stock_local_tools(store=store)}
    token = bind_stock_execution(_context())
    try:
        with pytest.raises(ValueError, match="mixed_symbol_evidence"):
            tools["analyze_stock_fundamentals"].invoke({"financial_evidence_ids": ids})
    finally:
        reset_stock_execution(token)


def test_clarification_tool_versions_typed_clarification_payload():
    from app.agents.stock import tools as stock_tools
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore

    from app.agents.evidence import NeedsClarification
    tools = {item.name: item for item in stock_tools.build_stock_local_tools(store=MemoryAgentEvidenceStore())}
    with pytest.raises(NeedsClarification) as caught:
        tools["request_stock_clarification"].invoke({
            "reason": "missing_strategy",
            "question": "请选择策略",
            "fields": ["strategy"],
            "options": [{"field": "strategy", "id": "value_quality", "label": "价值质量"}],
        })
    seen = caught.value.payload

    assert seen["agent_version"] == "stock-react-v1"
    assert seen["kind"] == "clarification"
    assert seen["options"][0]["field"] == "strategy"
