"""按 agent 分派公开 artifact 投影器。"""

from __future__ import annotations

from app.agents.artifacts import project_artifact, public_artifacts

STOCK_PAYLOAD = {
    "scenario": "market_overview",
    "as_of": "2026-09-25T15:00:00+08:00",
    "sources": ["fuyao"],
    "warnings": [],
    "market_overview": {
        "failed": False,
        "indices": [{
            "thscode": "000001.SH", "name": "上证综指", "last": 3200.5,
            "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00",
        }],
        "raw_upstream": {"api_key": "sk-secret"},
    },
}

PORTFOLIO_PAYLOAD = {
    "scenario": "allocation_adopted",
    "allocation_adopted": {
        "plan_id": "p-light",
        "tier": "light",
        "tier_label": "微调",
        "unit_nav": 1.02,
        "trade_count": 2,
        "weights": {"110011": 0.6},
        "secret": "sk-secret",
    },
    "raw_upstream": {"api_key": "sk-secret"},
}


def test_stock_artifacts_go_through_the_stock_projector():
    projected = project_artifact("stock_agent", STOCK_PAYLOAD)

    assert projected["schema_version"] == "stock-public-v1"
    assert projected["market_overview"]["indices"][0]["last"] == 3200.5
    assert "raw_upstream" not in projected


def test_portfolio_artifacts_keep_the_adopted_summary_and_drop_weights():
    projected = project_artifact("portfolio_agent", PORTFOLIO_PAYLOAD)

    assert projected["schema_version"] == "portfolio-public-v1"
    assert projected["scenario"] == "allocation_adopted"
    assert projected["allocation_adopted"]["plan_id"] == "p-light"
    assert projected["allocation_adopted"]["unit_nav"] == 1.02
    assert "weights" not in projected["allocation_adopted"]
    assert "secret" not in projected["allocation_adopted"]
    assert "raw_upstream" not in projected


def test_unknown_agents_never_leak_the_raw_payload():
    assert project_artifact("chat_agent", {"secret": "sk-secret"}) == {}
    assert project_artifact("", {"secret": "sk-secret"}) == {}
    assert project_artifact("product_agent", {"secret": "sk-secret"}) == {}
    assert project_artifact("portfolio_agent", {"warnings": []}) == {}


def test_public_artifacts_projects_every_result_with_its_own_agent():
    from app.agents.graphs.state import PortfolioResult, StockResult
    from app.agents.portfolio.contracts import AllocationOutcome
    from app.agents.stock.contracts import CandidateStock, StockResultV1

    stock = StockResult(
        status="ok",
        summary="候选池",
        result=StockResultV1(
            scenario="stock_screening",
            summary="候选池",
            as_of="2026-09-25T15:00:00+08:00",
            sources=["fuyao"],
            stock_screening={
                "candidates": [{
                    "thscode": "600519.SH", "name": "贵州茅台", "score": 80.0,
                    "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00",
                }],
            },
        ),
        candidates=[CandidateStock(thscode="600519.SH", name="贵州茅台", score=80.0)],
    )
    portfolio = PortfolioResult(
        status="ok",
        summary="已采纳",
        adopted=AllocationOutcome(plan_id="p-light", tier="light", unit_nav=1.02, trade_count=2),
    )

    artifacts, candidates = public_artifacts([stock, portfolio])

    assert [item["schema_version"] for item in artifacts] == [
        "stock-public-v1", "portfolio-public-v1",
    ]
    assert artifacts[0]["stock_screening"]["candidates"][0]["thscode"] == "600519.SH"
    assert candidates == [{"thscode": "600519.SH", "name": "贵州茅台", "score": 80.0}]


def test_public_artifacts_ignores_malformed_results():
    from app.agents.graphs.state import PortfolioResult
    from app.agents.portfolio.contracts import AllocationOutcome

    artifacts, candidates = public_artifacts([
        None,
        "x",
        PortfolioResult(
            status="ok",
            summary="已采纳",
            adopted=AllocationOutcome(plan_id="p-light", unit_nav=1.02, trade_count=2),
        ),
        PortfolioResult(status="ok", summary="挂起中，无采纳结果"),
    ])

    assert [item["schema_version"] for item in artifacts] == ["portfolio-public-v1"]
    assert candidates == []
