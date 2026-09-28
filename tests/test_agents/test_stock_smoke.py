"""股票 ReAct 接入主图后的输出安全与公开 artifact。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from app.agents.stock.contracts import ScenarioClassification


def test_render_output_has_no_trade_instructions_and_keeps_disclaimer():
    from app.agents.stock.contracts import StockRequest
    from app.agents.stock.render import render_stock_result
    from app.services.portfolio_service import SIMULATED_DISCLAIMER

    guarded = {
        "scenario": "equity_research",
        "summary": "建议买入贵州茅台，目标价 3000 元，仓位比例 30%，注意买卖点。",
        "as_of": "2026-09-25T15:00:00+08:00",
        "sources": ["fuyao"],
        "warnings": [],
        "failed": False,
        "equity_research": [{
            "thscode": "600519.SH", "name": "贵州茅台", "failed": False,
            "quote": {"last": 3200.5, "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00"},
            "as_of": "2026-09-25T15:00:00+08:00", "sources": ["fuyao"],
        }],
    }
    rendered = render_stock_result(guarded, StockRequest(scenario="equity_research", symbols=["600519.SH"]))
    content = rendered["content"]
    for banned in ("买入", "卖出", "加仓", "减仓", "目标价", "仓位比例", "仓位 30%", "买卖点"):
        assert banned not in content, f"受控摘要仍包含 {banned}"
    assert SIMULATED_DISCLAIMER in content


def test_public_artifact_drops_unsourced_numbers_and_unknown_fields():
    from app.agents.stock.evidence import public_stock_artifact

    payload = {
        "scenario": "market_overview",
        "as_of": "2026-09-25T15:00:00+08:00",
        "sources": ["fuyao"],
        "warnings": [],
        "market_overview": {
            "failed": False,
            "api_key": "sk-secret",
            "kline": [{"close": 1}],
            "indices": [{
                "thscode": "000001.SH", "name": "上证综指", "last": 3200.5, "change_pct": 1.2,
                "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00", "raw": {"secret": 1},
            }],
            "as_of": "2026-09-25T15:00:00+08:00",
        },
    }
    public = public_stock_artifact(payload)
    assert public["schema_version"] == "stock-public-v1"
    block = public["market_overview"]
    assert "api_key" not in block
    assert "kline" not in block
    assert "raw" not in block["indices"][0]
    assert block["indices"][0]["last"] == 3200.5


def test_unsourced_index_numbers_are_removed():
    from app.agents.stock.evidence import public_stock_artifact

    payload = {
        "scenario": "market_overview", "as_of": "", "sources": [], "warnings": [],
        "market_overview": {
            "failed": False, "indices": [{"thscode": "000001.SH", "name": "上证综指", "last": 3200.5}],
        },
    }
    public = public_stock_artifact(payload)
    assert public["market_overview"]["indices"] == []


def test_public_artifact_projection_is_idempotent():
    from app.agents.stock.evidence import public_stock_artifact

    payload = {
        "scenario": "equity_research", "as_of": "2026-09-25T15:00:00+08:00",
        "sources": ["fuyao"], "warnings": [], "failed": False,
        "equity_research": [{
            "thscode": "600519.SH", "name": "贵州茅台", "failed": False,
            "as_of": "2026-09-25T15:00:00+08:00", "sources": ["fuyao"],
            "quote": {"last": 3200.5, "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00"},
            "technicals": {
                "status": "ok", "trend": "up", "rsi": 55.0,
                "macd": {"dif": 1.0, "dea": 0.5, "hist": 0.5},
                "source": "fuyao", "as_of": "2026-09-25T15:00:00+08:00",
            },
        }],
    }
    once = public_stock_artifact(payload)
    twice = public_stock_artifact(once)
    assert once == twice
    equity = once["equity_research"][0]
    assert equity["technicals"]["rsi"] == 55.0


class _DualScriptedModel:
    """同一个模型对象同时服务意图分类和 aggregator。"""

    def __init__(self, intent, classification, answer: str = "指数偏强，请结合自身情况判断。") -> None:
        self.intent = intent
        self.classification = classification
        self.answer = answer
        self.aggregator_inputs: list = []

    def with_structured_output(self, schema, *, method=None, **kwargs):  # noqa: ANN001
        parent = self

        class _Structured:
            async def ainvoke(self, messages, config=None):  # noqa: ANN001
                if schema is ScenarioClassification:
                    return parent.classification
                return parent.intent

        return _Structured()

    async def astream(self, messages, config=None):  # noqa: ANN001
        self.aggregator_inputs.append(messages)
        yield SimpleNamespace(content=self.answer)


def test_real_stock_react_agent_inside_the_real_advisor_graph():
    """主图集成：ReAct Agent 的证据句柄由服务端组装后进入 aggregator。"""
    from app.agents.builders import build_specialists
    from app.agents.graphs.state import Intent, IntentResult
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.stock.contracts import StockAgentResponse
    from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
    from app.services.portfolio_service import SIMULATED_DISCLAIMER

    model = _DualScriptedModel(
        IntentResult(intents=[Intent(agent="stock_agent", task="看大盘", kind="general")]),
        ScenarioClassification(scenario="market_overview"),
        answer="上证综指偏强，建议买入，目标价 4000，仓位比例 50%。",
    )
    context = EvidenceContext(
        request_id="17171717-1717-1717-1717-171717171717",
        session_id="advisor-stock-integration",
        customer_id="CUST000001",
    )
    evidence_store = MemoryAgentEvidenceStore()
    evidence = evidence_store.put(
        context,
        tool_call_id="local:aggregate_market_snapshot",
        tool_name="aggregate_market_snapshot",
        evidence_type="market_overview",
        payload={
            "failed": False,
            "indices": [{
                "thscode": "000001.SH", "name": "上证综指", "last": 3200.5,
                "change_pct": 0.8, "source": "fuyao", "as_of": "2026-09-27T14:00:00+08:00",
            }],
            "breadth": {}, "sectors": {"industry": [], "concept": []},
            "as_of": "2026-09-27T14:00:00+08:00", "sources": ["fuyao"], "warnings": [],
        },
        source="fuyao",
        as_of="2026-09-27T14:00:00+08:00",
    )

    class _StockAgent:
        async def ainvoke(self, _input, _config):
            return {"structured_response": StockAgentResponse(
                scenario="market_overview",
                summary="上证综指偏强",
                evidence_ids=[evidence["id"]],
                evidence_quality="ok",
                failed=False,
            )}

    graph = build_advisor_graph(
        model=model,
        specialists=build_specialists(model, stock_agent=_StockAgent(), evidence_store=evidence_store),
        checkpointer=MemorySaver(),
        risk_of=lambda code: None,
        context_limit=20,
    )
    config = {"configurable": {
        "thread_id": context.session_id,
        "customer_id": context.customer_id,
        "request_id": context.request_id,
    }}

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "看大盘"}], "profile_snapshot": {"risk_level": "R3"}},
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    values = state.values

    artifacts = [item for result in values["agent_results"] for item in result.get("artifacts") or []]
    assert artifacts, "Stock Agent 没有产出 artifact"
    artifact = artifacts[0]
    assert artifact["scenario"] == "market_overview"
    assert artifact["schema_version"] == "stock-public-v1"
    assert artifact["market_overview"]["indices"]
    assert "api_key" not in str(artifact)
    assert "kline" not in artifact["market_overview"]

    human = next(
        message["content"] for message in model.aggregator_inputs[-1] if message["role"] == "user"
    )
    assert "上证综指" in human
    assert "stock-public-v1" in human
    assert "3200.5" in human
    assert "update_time" not in human
    assert "pct_chg" not in human

    answer = values["final_result"]
    for token in ("买入", "卖出", "加仓", "减仓", "目标价", "买卖点", "仓位比例"):
        assert token not in answer, f"最终回答仍包含 {token}"
    assert SIMULATED_DISCLAIMER in answer
