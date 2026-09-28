"""股票请求/结果契约，以及 AgentResult 与商品推荐字段分离。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.graphs.state import empty_result


def test_stock_request_accepts_three_scenarios_and_defaults():
    from app.agents.stock.contracts import StockRequest

    request = StockRequest(scenario="market_overview")
    assert request.symbols == []
    assert request.candidate_count == 5
    assert request.filter_by_profile is False
    screening = StockRequest(
        scenario="stock_screening",
        strategy="value_quality",
        horizon="medium",
        candidate_count=8,
        industries=["银行"],
        index_scope=["000300.SH"],
        filter_by_profile=True,
    )
    assert screening.strategy == "value_quality"
    research = StockRequest(scenario="equity_research", symbols=["600519.SH"])
    assert research.symbols == ["600519.SH"]


def test_stock_request_rejects_unknown_scenario_and_out_of_range_count():
    from app.agents.stock.contracts import StockRequest

    with pytest.raises(ValidationError):
        StockRequest(scenario="day_trade")
    with pytest.raises(ValidationError):
        StockRequest(scenario="stock_screening", candidate_count=0)
    with pytest.raises(ValidationError):
        StockRequest(scenario="stock_screening", candidate_count=11)
    with pytest.raises(ValidationError):
        StockRequest(scenario="stock_screening", strategy="momentum_only")
    with pytest.raises(ValidationError):
        StockRequest(scenario="stock_screening", horizon="intraday")


def test_stock_result_v1_has_public_fields_and_one_scenario_block():
    from app.agents.stock.contracts import StockResultV1

    result = StockResultV1(
        scenario="market_overview",
        summary="指数多数上涨",
        as_of="2026-09-27T14:00:00+08:00",
        sources=["fuyao"],
        warnings=[],
        evidence_quality="ok",
        market_overview={"indices": []},
    )
    dumped = result.model_dump()
    for key in ("scenario", "summary", "as_of", "sources", "warnings", "evidence_quality"):
        assert key in dumped
    assert dumped["market_overview"] == {"indices": []}
    assert dumped["equity_research"] is None
    assert dumped["stock_screening"] is None


def test_empty_result_separates_stock_candidates_from_product_codes():
    payload = empty_result("stock_agent", "行情暂不可用", source="fuyao")
    assert payload["recommended_product_codes"] == []
    assert payload["candidate_stocks"] == []
    assert payload["artifacts"] == []
    assert payload["failed"] is True


def test_stock_agent_response_forbids_unknown_fields():
    from app.agents.stock.contracts import StockAgentResponse

    parsed = StockAgentResponse(
        scenario="market_overview",
        summary="市场数据不足",
        evidence_ids=[],
        warnings=["缺少实时证据"],
        evidence_quality="failed",
        failed=True,
    )
    assert parsed.failed is True
    with pytest.raises(ValidationError):
        StockAgentResponse(
            scenario="market_overview",
            summary="",
            evidence_ids=[],
            evidence_quality="failed",
            failed=True,
            fabricated_price=123,
        )
