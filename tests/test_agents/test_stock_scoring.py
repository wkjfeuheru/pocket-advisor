"""硬过滤与三套评分。同一夹具必须得到同一排序。"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.stock.filters import apply_hard_filters
from app.agents.stock.scoring import score_universe

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stock_scoring_golden.json"


def test_hard_filters_drop_st_halt_new_negative_illiquid_and_missing():
    rows = [
        {"thscode": "ST.SH", "is_st": True, "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7, "earnings_quality": 1},
        {"thscode": "HALT.SH", "suspended": True, "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7, "earnings_quality": 1},
        {"thscode": "NEW.SH", "listed_days": 30, "net_assets": 1, "avg_turnover_20": 8e7, "earnings_quality": 1},
        {"thscode": "NEG.SH", "listed_days": 400, "net_assets": -1, "avg_turnover_20": 8e7, "earnings_quality": 1},
        {"thscode": "THIN.SH", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 1_000_000, "earnings_quality": 1},
        {"thscode": "BOARD.SH", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7, "one_word_limit": True, "earnings_quality": 1},
        {"thscode": "MISS.SH", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7},
        {"thscode": "PART.SH", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7, "earnings_quality": 1, "cashflow_quality": None},
        {"thscode": "OK.SH", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7, "earnings_quality": 1, "cashflow_quality": 1, "is_st": False},
    ]
    kept = apply_hard_filters(rows, strategy="value_quality")
    assert [row["thscode"] for row in kept] == ["OK.SH"]


def test_golden_rankings_are_stable_and_ignore_news_sentiment():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = score_universe(payload["rows"], strategy="value_quality", horizon="medium")
    second = score_universe(payload["rows"], strategy="value_quality", horizon="medium")
    assert [row["thscode"] for row in first] == payload["value_quality_medium"]
    assert [row["thscode"] for row in first] == [row["thscode"] for row in second]
    assert [row["thscode"] for row in score_universe(payload["rows"], strategy="growth_quality", horizon="medium")] == payload["growth_quality_medium"]
    assert [row["thscode"] for row in score_universe(payload["rows"], strategy="trend_momentum", horizon="short")] == payload["trend_momentum_short"]
    assert all("news_sentiment" not in (row.get("components") or {}) for row in first)


def test_industry_percentiles_and_horizon_windows_do_not_treat_missing_as_zero():
    rows = [
        {"thscode": "SLOW.SH", "industry": "白酒", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7,
         "ret_5": 1, "ret_10": 1, "ret_20": 1, "momentum": 0, "trend": 1, "liquidity": 1, "volatility": 1},
        {"thscode": "FAST.SH", "industry": "白酒", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7,
         "ret_5": 9, "ret_10": 9, "ret_20": 9, "momentum": 0, "trend": 1, "liquidity": 1, "volatility": 1},
        {"thscode": "BANK.SH", "industry": "银行", "listed_days": 400, "net_assets": 1, "avg_turnover_20": 8e7,
         "ret_5": 100, "ret_10": 100, "ret_20": 100, "momentum": 0, "trend": 1, "liquidity": 1, "volatility": 1},
    ]
    ranked = score_universe(rows, strategy="trend_momentum", horizon="short")
    fast = next(row for row in ranked if row["thscode"] == "FAST.SH")
    slow = next(row for row in ranked if row["thscode"] == "SLOW.SH")
    assert fast["components"]["momentum"] == 100
    assert fast["score"] > slow["score"]
