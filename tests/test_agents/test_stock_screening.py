"""研究候选池：必须有策略和周期；默认全 A；股票代码不进商品推荐。"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.stock.contracts import StockRequest
from app.agents.stock.screening import screen_candidates
from app.crud.stock_feature_crud import MemoryStockFeatureStore, SnapshotStaleError


def _row(thscode: str, name: str, **kwargs) -> dict:
    base = {
        "thscode": thscode,
        "name": name,
        "listed_days": 400,
        "net_assets": 10.0,
        "avg_turnover_20": 80_000_000,
        "is_st": False,
        "suspended": False,
        "industry": "白酒",
        "earnings_quality": 0.5,
        "cashflow_quality": 0.5,
        "valuation": 20,
        "stability": 0.5,
        "growth": 0.2,
        "valuation_fit": 0.5,
        "momentum": 0.1,
        "trend": 0.1,
        "liquidity": 0.8,
        "volatility": 0.2,
        "max_drawdown": 0.1,
    }
    base.update(kwargs)
    return base


def test_screening_requires_strategy_and_horizon():
    with pytest.raises(ValueError, match="missing"):
        screen_candidates(StockRequest(scenario="stock_screening"), rows=[_row("AAA.SH", "A")])


def test_default_universe_returns_five_candidates_and_not_product_codes():
    rows = [
        _row("AAA.SH", "A", earnings_quality=0.9, cashflow_quality=0.8, valuation=8),
        _row("BBB.SH", "B", earnings_quality=0.6, cashflow_quality=0.6, valuation=15),
        _row("CCC.SH", "C", earnings_quality=0.2, cashflow_quality=0.2, valuation=40),
        _row("DDD.SH", "D", earnings_quality=0.4, cashflow_quality=0.4, valuation=25),
        _row("EEE.SH", "E", earnings_quality=0.5, cashflow_quality=0.5, valuation=18),
        _row("FFF.SH", "F", earnings_quality=0.3, cashflow_quality=0.3, valuation=30),
    ]
    request = StockRequest(scenario="stock_screening", strategy="value_quality", horizon="medium")
    result = screen_candidates(request, rows=rows, snapshot_id="snap-1", as_of="2026-09-26")
    assert result["failed"] is False
    assert len(result["candidates"]) == 5
    item = result["candidates"][0]
    assert {"thscode", "name", "score", "components", "evidence", "risks", "as_of", "snapshot_id"} <= set(item)
    assert result["candidate_stocks"][0]["thscode"] == item["thscode"]
    assert result["recommended_product_codes"] == []
    assert "600519.SH" not in result["recommended_product_codes"]


def test_industry_and_index_scope_filter_the_universe():
    rows = [
        _row("AAA.SH", "A", industry="白酒"),
        _row("BBB.SH", "B", industry="银行"),
    ]
    request = StockRequest(
        scenario="stock_screening",
        strategy="value_quality",
        horizon="medium",
        industries=["白酒"],
        index_scope=["000300.SH"],
        candidate_count=10,
    )
    result = screen_candidates(
        request,
        rows=rows,
        membership={"000300.SH": ["AAA.SH"]},
        snapshot_id="snap-1",
        as_of="2026-09-26",
    )
    assert [item["thscode"] for item in result["candidates"]] == ["AAA.SH"]


def test_profile_filter_does_not_put_stocks_into_product_codes():
    rows = [
        _row("LOW.SH", "稳健", volatility=0.1, max_drawdown=0.05, avg_turnover_20=90_000_000),
        _row("HIGH.SH", "波动", volatility=0.8, max_drawdown=0.6, avg_turnover_20=90_000_000),
    ]
    request = StockRequest(
        scenario="stock_screening",
        strategy="value_quality",
        horizon="medium",
        filter_by_profile=True,
        candidate_count=10,
    )
    result = screen_candidates(
        request,
        rows=rows,
        profile={"risk_level": "R1", "liquidity_need": "高"},
        snapshot_id="snap-1",
        as_of="2026-09-26",
    )
    codes = [item["thscode"] for item in result["candidates"]]
    assert "LOW.SH" in codes
    assert "HIGH.SH" not in codes
    assert result["recommended_product_codes"] == []
    assert result["candidate_stocks"][0]["thscode"] != ""


def test_missing_index_membership_does_not_fall_back_to_the_full_market():
    rows = [_row("AAA.SH", "A"), _row("BBB.SH", "B")]
    request = StockRequest(
        scenario="stock_screening",
        strategy="value_quality",
        horizon="medium",
        index_scope=["000300.SH"],
    )
    result = screen_candidates(request, rows=rows, membership={}, snapshot_id="snap-1", as_of="2026-09-26")
    assert result["failed"] is True
    assert result["candidates"] == []
    assert "index_scope_unavailable" in result["warnings"]


def test_stale_snapshot_fails_screening_only():
    store = MemoryStockFeatureStore()
    store.refresh(date(2026, 9, 20), rows=[_row("AAA.SH", "A")])
    with pytest.raises(SnapshotStaleError):
        store.latest_ready(as_of=date(2026, 9, 27), required=True)
