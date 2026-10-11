"""真实大盘查询暴露的适配、聚合与预算问题。"""
from datetime import datetime, timezone
import pytest

from app.mcp.fuyao_dto import normalize_payload
from app.agents.stock.market import build_market_overview


def test_real_calendar_dates_survive_normalization():
    rows = normalize_payload("trade_calendar", {"code": 0, "data": {"timestamp": 1791690429135,
        "item": [{"date_ms": 1791475200000, "date": "20261009"}]}})
    assert rows[0]["trade_date"] == "2026-10-09"


def test_real_index_historical_is_readonly_whitelisted():
    from app.mcp.fuyao import operation_for_tool_name
    assert operation_for_tool_name("fuyao_a_share_index_get_a_share_index_prices_historical") == "kline"


def test_business_error_is_not_a_security_code():
    result = normalize_payload("quote", {"code": 1002, "message": "invalid symbol", "data": None})
    assert result.get("thscode") is None
    assert result["failed"] is True


def test_quote_keeps_turnover_and_timestamp_precision():
    rows = normalize_payload("quote", {"code": 0, "data": {"timestamp": 1791690506000,
        "item": [{"thscode": "000001.SH", "last_price": 3813.79, "turnover": 888561200000}]}})
    assert rows[0]["turnover"] == 888561200000
    assert "T" in rows[0]["as_of"]


def test_partial_market_keeps_verified_indices():
    result = build_market_overview(indices=[{"thscode": "000001.SH", "last": 3813.79,
        "change_pct": 0.05, "as_of": "2026-10-09", "source": "fuyao"}], breadth=None,
        sectors=None, now=datetime(2026, 10, 11, tzinfo=timezone.utc), session="closed",
        upstream_error="market_evidence_incomplete")
    assert result["failed"] is False
    assert result["indices"][0]["last"] == 3813.79
    assert result["breadth"]["up"] is None
    assert len(result["warnings"]) <= 2


def test_market_snapshot_does_not_turn_equity_quotes_into_indices():
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    from app.agents.stock.tools import bind_stock_execution, reset_stock_execution, build_stock_local_tools
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("request", "session", "customer")
    row = store.put(ctx, tool_call_id="q", tool_name="fuyao_a_share_get_a_share_prices_snapshot",
        evidence_type="quote", payload={"data": [{"thscode": "600519.SH", "last": 1263, "as_of": "2026-10-09"}]},
        source="fuyao", as_of="2026-10-09")
    token = bind_stock_execution(ctx)
    try:
        tool = next(t for t in build_stock_local_tools(store=store) if t.name == "aggregate_market_snapshot")
        result = tool.invoke({"evidence_ids": [row["id"]]})
    finally:
        reset_stock_execution(token)
    assert store.get(result["evidence_id"], ctx)["payload"]["indices"] == []


def test_millisecond_history_arguments_match_confirmed_timeframe():
    from app.agents.evidence import bind_execution, reset_execution, validate_tool_arguments
    token = bind_execution(None, None, domain_slots={"symbols": ["600519.SH"],
        "timeframe": {"start": "2026-10-01", "end": "2026-10-11"}})
    try:
        assert not validate_tool_arguments("fuyao_a_share_get_a_share_prices_historical", {
            "thscode": "600519.SH", "start": 1790784000000, "end": 1791648000000})
    finally:
        reset_execution(token)


def test_recent_week_is_an_explicit_timeframe():
    from app.agents.slots import normalize_timeframe
    result = normalize_timeframe("最近一周", now=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert result.start == "2026-10-04" and result.end == "2026-10-11"


@pytest.mark.asyncio
async def test_followup_with_trend_suffix_keeps_confirmed_equity_context():
    from app.agents.slots import extract_slots, SlotExtraction, SlotPatch
    class Model:
        def with_structured_output(self, *a, **kw): return self
        async def ainvoke(self, inputs):
            return SlotExtraction(continuation=True, patch=SlotPatch(set_values={"timeframe": "近一年"}))
    result = await extract_slots(Model(), "stock_agent", "看近一年的走势。", [],
        {"scenario": "equity_research", "symbols": ["600519.SH"]})
    assert not result["missing"] and result["slots"]["symbols"] == ["600519.SH"]


def test_financial_report_quarter_is_a_date():
    result = normalize_payload("financials", {"data": {"report": "2026-2", "abilities": []}})
    assert result["as_of"] == "2026-06-30"


@pytest.mark.asyncio
async def test_market_pipeline_retains_partial_data_and_uses_metadata_names():
    from types import SimpleNamespace
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    from app.agents.stock.market_fetch import collect_market_evidence
    calls = []
    async def snapshot(args):
        calls.append(args)
        return {"code": 0, "data": {"timestamp": "2026-10-09T15:00:00+08:00", "item": [
            {"thscode": "000001.SH", "last_price": 3813.79, "price_change_ratio_pct": 0.05}]}}
    async def calendar(args):
        return {"code": 0, "data": {"item": [{"date": "20261009"}]}}
    async def search(args):
        return {"code": 0, "data": {"item": [{"thscode": "000001.SH", "name": "上证指数"}]}}
    tools = [SimpleNamespace(name="fuyao_a_share_index_get_a_share_index_prices_snapshot", ainvoke=snapshot),
        SimpleNamespace(name="fuyao_a_share_get_a_share_calendar_trading_days", ainvoke=calendar),
        SimpleNamespace(name="fuyao_meta_get_meta_tickers_search", ainvoke=search)]
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("request", "session", "customer")
    response = await collect_market_evidence(store, ctx, tools, {})
    payload = store.get(response.evidence_ids[0], ctx)["payload"]
    assert not response.failed and payload["indices"][0]["name"] == "上证指数"
    assert payload["last_calendar_trade_date"] == "2026-10-09"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_index_code_survives_model_name_and_scenario_drift():
    from app.agents.slots import extract_slots, SlotExtraction, SlotPatch
    class Model:
        def with_structured_output(self, *a, **kw): return self
        async def ainvoke(self, inputs):
            return SlotExtraction(patch=SlotPatch(set_values={"scenario": "equity_research", "symbols": ["沪深300指数"]}))
    result = await extract_slots(Model(), "stock_agent", "看看沪深300指数（000300.SH）最近行情", [], {},
        source_query="看看沪深300指数（000300.SH）最近行情")
    assert result["slots"]["scenario"] == "market_overview"
    assert result["slots"]["symbols"] == ["000300.SH"]


@pytest.mark.asyncio
async def test_broad_a_share_universe_is_not_an_index_security():
    from app.agents.slots import extract_slots, SlotExtraction, SlotPatch
    class Model:
        def with_structured_output(self, *a, **kw): return self
        async def ainvoke(self, inputs):
            return SlotExtraction(patch=SlotPatch(set_values={"scenario": "stock_screening", "strategy": "value_quality",
                "horizon": "long", "index_scope": ["A股"]}))
    result = await extract_slots(Model(), "stock_agent", "在A股中按长期价值质量策略选股", [], {})
    assert not result["missing"] and not result["slots"].get("index_scope")


@pytest.mark.asyncio
@pytest.mark.parametrize("history_code,expected", [("000300.SH", True), ("399006.SZ", False)])
async def test_historical_only_result_is_independent_and_never_relabels_another_index(history_code, expected):
    from types import SimpleNamespace
    from app.agents.stock.market_fetch import collect_market_evidence
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    async def history(args):
        return {"code": 0, "data": {"item": [{"thscode": history_code, "date": "2026-10-08", "close": 100},
            {"thscode": history_code, "date": "2026-10-09", "close": 200}]}}
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    response = await collect_market_evidence(store, ctx, [SimpleNamespace(name="fuyao_a_share_index_get_a_share_index_prices_historical", ainvoke=history)],
        {"symbols": ["000300.SH"], "timeframe": {"start": "2026-10-01", "end": "2026-10-11"}})
    payload = store.get(response.evidence_ids[0], ctx)["payload"]
    assert bool(payload["historical_indices"]) == expected
    assert response.failed is not expected


@pytest.mark.asyncio
async def test_auxiliary_runtime_error_does_not_destroy_valid_snapshot():
    from types import SimpleNamespace
    from app.agents.stock.market_fetch import collect_market_evidence
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    async def snapshot(args):
        return {"data": [{"thscode": "000001.SH", "last": 3813.79, "as_of": "2026-10-09"}]}
    async def calendar(args):
        raise RuntimeError("upstream error")
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    response = await collect_market_evidence(store, ctx, [SimpleNamespace(name="fuyao_a_share_index_get_a_share_index_prices_snapshot", ainvoke=snapshot),
        SimpleNamespace(name="fuyao_a_share_get_a_share_calendar_trading_days", ainvoke=calendar)], {})
    assert not response.failed


def test_router_repairs_one_malformed_model_response():
    import asyncio
    from app.agents.graphs.state import Intent, ChatResult
    from app.agents.graphs.task_agent import build_advisor_graph
    from tests.test_agents.test_supervisor_v2 import Model
    class RetryModel(Model):
        repaired = False
        def with_structured_output(self, schema, **kwargs):
            if schema.__name__ == "IntentResult" and not self.repaired:
                self.repaired = True
                class Invalid:
                    async def ainvoke(self, messages, config=None):
                        return {"intents": [{"agent": "unknown", "task": "你好"}]}
                return Invalid()
            return super().with_structured_output(schema, **kwargs)
    model = RetryModel([Intent(agent="chat_agent", task="你好")])
    async def worker(task, state): return ChatResult(summary="你好")
    result = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "你好"}]}))
    assert result["final"].answer and model.repaired


def test_public_warnings_do_not_expose_internal_codes():
    from app.services.agent_service import _collect_warnings
    from app.agents.graphs.state import StockResult
    warnings = _collect_warnings({"stock_result": StockResult(warnings=[
        "missing_market_aggregate", "market_evidence_incomplete", "missing_structured_response"])})
    assert warnings and all("missing_" not in w and "market_evidence" not in w for w in warnings)


@pytest.mark.asyncio
async def test_repeat_guard_allows_different_arguments_and_reuses_identical_queries():
    from types import SimpleNamespace
    from langchain_core.messages import ToolMessage
    from app.agents.stock.agent import _tool_repeat_guard
    from app.agents.stock.tools import bind_stock_execution, reset_stock_execution
    middleware = _tool_repeat_guard()
    called = []
    async def handler(request):
        called.append(request.tool_call["args"])
        return ToolMessage(content="verified", tool_call_id=request.tool_call["id"])
    token = bind_stock_execution(None)
    try:
        for n in range(4):
            request = SimpleNamespace(tool_call={"name": "quote", "args": {"thscode": f"{n:06}.SH"}, "id": str(n)})
            response = await middleware.awrap_tool_call(request, handler)
            assert response.content == "verified"
        repeat = SimpleNamespace(tool_call={"name": "quote", "args": {"thscode": "000000.SH"}, "id": "repeat"})
        response = await middleware.awrap_tool_call(repeat, handler)
        assert response.tool_call_id == "repeat"
        assert len(called) == 4
    finally:
        reset_stock_execution(token)


@pytest.mark.asyncio
async def test_market_overview_fills_breadth_and_sectors_from_auxiliary_source():
    from types import SimpleNamespace
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    from app.agents.stock.market_fetch import collect_market_evidence

    class _FakeAuxiliary:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, name, **params):
            self.calls.append(name)
            if name == "market_breadth":
                return {"ok": True, "data": {"up": 3347, "down": 2159, "unchanged": 130,
                    "source": "eastmoney", "as_of": "2026-10-09T15:00:00+08:00"}}
            return {"ok": True, "data": {"industry": [{"name": "视频媒体", "change_pct": 19.98}],
                "concept": [{"name": "AI语料", "change_pct": 6.07}],
                "source": "eastmoney", "as_of": "2026-10-09T15:00:00+08:00"}}

    async def snapshot(args):
        return {"code": 0, "data": {"timestamp": "2026-10-09T15:00:00+08:00",
            "item": [{"thscode": "000001.SH", "last_price": 3813.79, "price_change_ratio_pct": 0.05}]}}
    async def calendar(args):
        return {"code": 0, "data": {"item": [{"date": "20261009"}]}}
    tools = [SimpleNamespace(name="fuyao_a_share_index_get_a_share_index_prices_snapshot", ainvoke=snapshot),
        SimpleNamespace(name="fuyao_a_share_get_a_share_calendar_trading_days", ainvoke=calendar)]
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    auxiliary = _FakeAuxiliary()
    response = await collect_market_evidence(store, ctx, tools, {}, eastmoney=auxiliary)
    row = store.get(response.evidence_ids[0], ctx)
    payload = row["payload"]
    assert not response.failed
    assert payload["breadth"]["up"] == 3347
    assert payload["sectors"]["industry"][0]["name"] == "视频媒体"
    assert set(payload["sources"]) == {"fuyao", "eastmoney"}
    assert row["source"] == "eastmoney,fuyao"
    assert not any("涨跌家数暂不可用" in item for item in payload["warnings"])
    assert not any("板块行情暂不可用" in item for item in payload["warnings"])
    assert auxiliary.calls == ["market_breadth", "sector_overview"]


@pytest.mark.asyncio
async def test_auxiliary_source_failure_keeps_honest_warnings():
    from types import SimpleNamespace
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    from app.agents.stock.market_fetch import collect_market_evidence

    class _BrokenAuxiliary:
        async def call(self, name, **params):
            return {"ok": False, "code": "unavailable", "data": None, "warning": "boom", "as_of": ""}

    async def snapshot(args):
        return {"code": 0, "data": {"timestamp": "2026-10-09T15:00:00+08:00",
            "item": [{"thscode": "000001.SH", "last_price": 3813.79, "price_change_ratio_pct": 0.05}]}}
    tools = [SimpleNamespace(name="fuyao_a_share_index_get_a_share_index_prices_snapshot", ainvoke=snapshot)]
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    response = await collect_market_evidence(store, ctx, tools, {}, eastmoney=_BrokenAuxiliary())
    payload = store.get(response.evidence_ids[0], ctx)["payload"]
    assert payload["breadth"]["up"] is None
    assert any("涨跌家数暂不可用" in item for item in payload["warnings"])
    assert any("板块行情暂不可用" in item for item in payload["warnings"])
