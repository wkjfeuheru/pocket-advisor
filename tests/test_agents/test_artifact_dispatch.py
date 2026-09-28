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
    results = [
        {"agent": "stock_agent", "artifacts": [STOCK_PAYLOAD],
         "candidate_stocks": [{"thscode": "600519.SH", "name": "贵州茅台", "score": 80.0}]},
        {"agent": "portfolio_agent", "artifacts": [PORTFOLIO_PAYLOAD], "candidate_stocks": []},
    ]

    artifacts, candidates = public_artifacts(results)

    assert [item["schema_version"] for item in artifacts] == [
        "stock-public-v1", "portfolio-public-v1",
    ]
    assert candidates == [{"thscode": "600519.SH", "name": "贵州茅台", "score": 80.0}]


def test_public_artifacts_ignores_malformed_results():
    artifacts, candidates = public_artifacts([
        None,
        {"agent": "portfolio_agent", "artifacts": [None, "x", PORTFOLIO_PAYLOAD]},
    ])

    assert [item["schema_version"] for item in artifacts] == ["portfolio-public-v1"]
    assert candidates == []
