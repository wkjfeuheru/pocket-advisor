"""结构化 worker 结果的契约守卫：``extra="forbid"`` 与 checkpoint 序列化往返。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.graphs.state import (
    ChatResult,
    PortfolioResult,
    ProductResult,
    StockResult,
)
from app.agents.portfolio.contracts import AllocationOffer, AllocationOutcome
from app.agents.stock.contracts import CandidateStock, StockResultV1


def _samples() -> list[tuple[type, object]]:
    """每个 worker 结果模型各取一个含子结构的实例。"""
    return [
        (
            StockResult,
            StockResult(
                status="ok",
                summary="指数平稳",
                source="fuyao",
                as_of="2026-09-28T10:00:00+08:00",
                result=StockResultV1(scenario="market_overview", summary="指数平稳"),
                candidates=[CandidateStock(thscode="600519.SH", name="贵州茅台", score=80.0)],
            ),
        ),
        (ProductResult, ProductResult(status="ok", summary="两只产品", recommended_codes=["110011"])),
        (
            PortfolioResult,
            PortfolioResult(
                status="ok",
                summary="已采纳",
                recommended_codes=["110011"],
                offer=AllocationOffer(plans=[{"plan_id": "p-light"}], nav_as_of="2026-09-28"),
                adopted=AllocationOutcome(plan_id="p-light", tier="light", unit_nav=1.01),
            ),
        ),
        (ChatResult, ChatResult(status="degraded", summary="普通问答", warnings=["w"])),
    ]


def test_worker_results_forbid_unknown_fields():
    """多传未声明键必须报错——否则类型化就只是纸面的。"""
    for model, instance in _samples():
        with pytest.raises(ValidationError):
            model.model_validate({**instance.model_dump(), "unexpected": 1})


def test_worker_results_survive_jsonplus_serialization_round_trip():
    """关键风险：状态里存 pydantic 模型，必须能被 PgSaver 的序列化器往返。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    for model, instance in _samples():
        type_name, data = serde.dumps_typed(instance)
        restored = serde.loads_typed((type_name, data))
        assert type(restored) is model, f"{model.__name__} 反序列化后类型变了"
        assert restored == instance
