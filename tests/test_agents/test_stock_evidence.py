"""证据门禁、渲染禁令，以及股票代码与商品适配性隔离。"""

from __future__ import annotations

from app.agents.stock.contracts import StockRequest, StockResultV1
from app.agents.stock.evidence import apply_evidence_guard
from app.agents.stock.render import render_stock_result
from app.services.portfolio_service import SIMULATED_DISCLAIMER


def test_unsourced_numbers_are_dropped():
    payload = {
        "scenario": "market_overview",
        "as_of": "2026-09-27T14:00:00+08:00",
        "sources": ["fuyao"],
        "warnings": [],
        "market_overview": {
            "indices": [
                {"thscode": "000001.SH", "name": "上证综指", "change_pct": 1.2, "source": "fuyao", "as_of": "2026-09-27T14:00:00+08:00"},
                {"thscode": "000300.SH", "name": "沪深300", "change_pct": 0.8},
            ],
        },
        "failed": False,
    }
    guarded = apply_evidence_guard(payload, StockRequest(scenario="market_overview"))
    codes = {item["thscode"] for item in guarded["market_overview"]["indices"]}
    assert "000001.SH" in codes
    assert "000300.SH" not in codes


def test_missing_required_block_fails():
    market = apply_evidence_guard(
        {"scenario": "market_overview", "market_overview": {"indices": []}, "failed": False, "warnings": []},
        StockRequest(scenario="market_overview"),
    )
    assert market["failed"] is True
    research = apply_evidence_guard(
        {"scenario": "equity_research", "equity_research": [], "failed": False, "warnings": []},
        StockRequest(scenario="equity_research", symbols=["600519.SH"]),
    )
    assert research["failed"] is True
    screening = apply_evidence_guard(
        {"scenario": "stock_screening", "stock_screening": {"candidates": []}, "failed": False, "warnings": []},
        StockRequest(scenario="stock_screening", strategy="value_quality", horizon="medium"),
    )
    assert screening["failed"] is True


def test_candidate_count_is_trimmed():
    request = StockRequest(scenario="stock_screening", strategy="value_quality", horizon="medium", candidate_count=2)
    guarded = apply_evidence_guard(
        {
            "scenario": "stock_screening",
            "failed": False,
            "warnings": [],
            "stock_screening": {
                "candidates": [
                    {"thscode": "AAA.SH", "score": 90, "source": "snapshot", "as_of": "2026-09-26"},
                    {"thscode": "BBB.SH", "score": 80, "source": "snapshot", "as_of": "2026-09-26"},
                    {"thscode": "CCC.SH", "score": 70, "source": "snapshot", "as_of": "2026-09-26"},
                ],
            },
        },
        request,
    )
    assert [item["thscode"] for item in guarded["stock_screening"]["candidates"]] == ["AAA.SH", "BBB.SH"]


def test_render_strips_trade_orders_and_keeps_disclaimer():
    result = StockResultV1(
        scenario="equity_research",
        summary="建议买入并加仓，目标价 2000，仓位 20%。历史涨跌幅仅供观察。",
        as_of="2026-09-27",
        sources=["fuyao"],
        equity_research=[{"thscode": "600519.SH", "name": "贵州茅台", "failed": False}],
    )
    rendered = render_stock_result(result, StockRequest(scenario="equity_research", symbols=["600519.SH"]))
    text = rendered["content"]
    assert SIMULATED_DISCLAIMER in text
    for token in ("买入", "卖出", "加仓", "减仓", "目标价", "仓位 20%"):
        assert token not in text
    assert "历史涨跌幅" in text
    assert rendered["artifacts"]
    assert "kline" not in str(rendered["artifacts"]).lower()
    assert rendered["recommended_product_codes"] == []


def test_public_artifact_drops_nested_unsourced_and_unknown_fields():
    guarded = apply_evidence_guard(
        {
            "scenario": "market_overview",
            "failed": False,
            "warnings": [],
            "as_of": "2026-09-27T14:00:00+08:00",
            "sources": ["fuyao"],
            "market_overview": {
                "indices": [{
                    "thscode": "000001.SH",
                    "name": "上证综指",
                    "change_pct": 1.2,
                    "source": "fuyao",
                    "as_of": "2026-09-27T14:00:00+08:00",
                    "raw_payload": {"secret": 1},
                    "nested": {"change_pct": 9.9},
                }],
                "breadth": {
                    "up": 10,
                    "down": 2,
                    "source": "fuyao",
                    "as_of": "2026-09-27T14:00:00+08:00",
                    "internal": 1,
                },
                "sectors": {
                    "industry": [{"name": "银行", "change_pct": 2.1, "secret": 1}],
                    "concept": [{"name": "人工智能", "change_pct": 3.2}],
                    "source": "fuyao",
                    "as_of": "2026-09-27T14:00:00+08:00",
                },
                "tool_response": {"mcp": True},
            },
        },
        StockRequest(scenario="market_overview"),
    )
    index = guarded["market_overview"]["indices"][0]
    assert guarded["schema_version"] == "stock-public-v1"
    assert index["change_pct"] == 1.2
    assert "raw_payload" not in index
    assert "nested" not in index
    assert "internal" not in guarded["market_overview"]["breadth"]
    assert "tool_response" not in guarded["market_overview"]
    sector = guarded["market_overview"]["sectors"]["industry"][0]
    assert sector == {"name": "银行", "change_pct": 2.1}
    unsourced = apply_evidence_guard(
        {
            "scenario": "market_overview",
            "failed": False,
            "warnings": [],
            "market_overview": {
                "indices": [{"thscode": "000001.SH", "change_pct": 1.2, "source": "fuyao", "as_of": "2026-09-27T14:00:00+08:00"}],
                "breadth": {"up": 10, "down": 2},
            },
        },
        StockRequest(scenario="market_overview"),
    )
    assert "up" not in (unsourced["market_overview"].get("breadth") or {})


def test_llm_numbers_without_source_are_removed_from_guard():
    guarded = apply_evidence_guard(
        {
            "scenario": "market_overview",
            "failed": False,
            "warnings": [],
            "summary": "指数上涨 3.14",
            "market_overview": {
                "indices": [{"thscode": "000001.SH", "change_pct": 3.14, "invented": True}],
            },
        },
        StockRequest(scenario="market_overview"),
    )
    assert guarded["market_overview"]["indices"] == []
    assert guarded["failed"] is True
