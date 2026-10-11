"""投顾主工作流：并行、降级、画像挂起和适配性。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.graphs.state import (
    ChatResult,
    Intent,
    IntentResult,
    PortfolioResult,
    ProductResult,
    RewrittenQuery,
    StockResult,
)
from app.agents.graphs.task_agent import (
    AllBranchesFailed,
    build_legacy_advisor_graph as build_advisor_graph,
    public_token,
)
from app.services.portfolio_service import SIMULATED_DISCLAIMER


class ScriptedModel:
    def __init__(self, intent: IntentResult, answers: list[str], rewritten: RewrittenQuery | None = None) -> None:
        self.intent = intent
        self.answers = list(answers)
        self.rewritten = rewritten
        self.structured_method = None
        self.compliance_inputs: list = []
        self.rewrite_inputs: list = []

    def with_structured_output(self, schema, *, method=None, **kwargs):
        self.structured_method = method
        parent = self

        class _Structured:
            async def ainvoke(self, messages, config=None):
                if schema is RewrittenQuery:
                    parent.rewrite_inputs.append(list(messages))
                    return parent.rewritten
                assert schema is IntentResult or schema == IntentResult
                return parent.intent

        return _Structured()

    async def astream(self, messages, config=None):
        self.compliance_inputs.append(messages)
        text = self.answers.pop(0) if self.answers else "没有更多材料。"
        yield SimpleNamespace(content=text)


def _specialist(
    name: str,
    *,
    content: str = "",
    codes: list[str] | None = None,
    error: Exception | None = None,
    warning: str = "",
    failed: bool = False,
    candidates: list[dict] | None = None,
    stock_result=None,
):
    """按 agent 名产出对应类型的结构化结果，模拟 worker 边界。"""

    async def run(task: str, state: dict):
        if error is not None:
            raise error
        status = "failed" if failed else "ok"
        summary = content or f"{name}:{task}"
        warnings = [warning] if warning else []
        common = {"status": status, "summary": summary, "warnings": warnings,
                  "source": "fake", "as_of": "2026-09-27T00:00:00Z"}
        if name == "stock_agent":
            from app.agents.stock.contracts import CandidateStock

            return StockResult(
                **common,
                result=stock_result,
                candidates=[CandidateStock.model_validate(item) for item in (candidates or [])],
            )
        if name == "product_agent":
            return ProductResult(**common, recommended_codes=list(codes or []))
        if name == "portfolio_agent":
            return PortfolioResult(**common, recommended_codes=list(codes or []))
        return ChatResult(**common)

    return run


def _graph(model: ScriptedModel, specialists: dict, risk: dict[str, int] | None = None):
    return build_advisor_graph(
        model=model,
        specialists=specialists,
        checkpointer=MemorySaver(),
        risk_of=lambda code: None if risk is None else risk.get(code),
        context_limit=20,
    )


def _intent(agent: str, kind: str = "general", needs_budget: bool = False) -> Intent:
    return Intent(agent=agent, task=f"处理{agent}", kind=kind, needs_budget=needs_budget)


def test_classify_uses_function_calling_and_runs_agents_in_parallel():
    model = ScriptedModel(
        IntentResult(intents=[_intent("stock_agent"), _intent("product_agent")]),
        ["股票平稳，商品可比较。\n推荐代码: 无"],
    )
    seen: list[str] = []

    async def stock(task, state):
        seen.append("stock")
        return await _specialist("stock_agent", content="行情可用")(task, state)

    async def product(task, state):
        seen.append("product")
        return await _specialist("product_agent", content="产品可用", codes=["110011"])(task, state)

    graph = _graph(model, {
        "stock_agent": stock,
        "product_agent": product,
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    }, {"110011": 2})

    async def run():
        config = {"configurable": {"thread_id": "thread-1"}}
        await graph.ainvoke(
            {
                "messages": [{"role": "user", "content": "看下股票和这只基金"}],
                "profile_snapshot": {
                    "risk_level": "R3",
                    "investment_horizon": "中期",
                    "investment_goal": "均衡增长",
                    "liquidity_need": "中",
                },
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    assert model.structured_method == "function_calling"
    assert set(seen) == {"stock", "product"}
    values = state.values
    assert isinstance(values["stock_result"], StockResult)
    assert isinstance(values["product_result"], ProductResult)
    assert values.get("portfolio_result") is None and values.get("chat_result") is None
    assert SIMULATED_DISCLAIMER in values["final"].answer
    human = next(
        message["content"]
        for message in model.compliance_inputs[-1]
        if isinstance(message, dict) and message.get("role") == "user"
    )
    assert "行情可用" in human and "产品可用" in human
    assert "function-calling" not in human
    assert "tool_calls" not in human


def test_partial_failure_still_aggregates_and_total_failure_stops():
    ok = ScriptedModel(IntentResult(intents=[_intent("stock_agent"), _intent("product_agent")]), ["只保留商品结论。\n推荐代码: 无"])
    graph = _graph(ok, {
        "stock_agent": _specialist("stock_agent", error=RuntimeError("upstream body secret")),
        "product_agent": _specialist("product_agent", content="商品正常"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run_ok():
        config = {"configurable": {"thread_id": "partial"}}
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "两个问题"}]},
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run_ok())
    values = state.values
    assert values["stock_result"].status == "failed"
    assert values["product_result"].status == "ok"
    assert "upstream body" not in values["final"].answer
    assert any("stock_agent 暂不可用" in item for item in values["stock_result"].warnings)

    bad = ScriptedModel(IntentResult(intents=[_intent("stock_agent"), _intent("chat_agent")]), [])
    broken = _graph(bad, {
        "stock_agent": _specialist("stock_agent", error=RuntimeError("a")),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent", error=RuntimeError("b")),
    })

    async def run_bad():
        await broken.ainvoke(
            {"messages": [{"role": "user", "content": "都会失败"}]},
            {"configurable": {"thread_id": "all-fail"}},
            durability="sync",
        )

    with pytest.raises(AllBranchesFailed):
        asyncio.run(run_bad())


def test_profile_interrupt_resumes_on_a_second_graph_without_rewriting_profile():
    model = ScriptedModel(
        IntentResult(intents=[_intent("portfolio_agent", kind="personalized", needs_budget=True)]),
        ["按您的画像说明配置思路。\n推荐代码: 无"],
    )
    saver = MemorySaver()
    specialists = {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent", content="组合说明"),
        "chat_agent": _specialist("chat_agent"),
    }
    first = build_advisor_graph(model=model, specialists=specialists, checkpointer=saver, risk_of=lambda _code: None)
    config = {"configurable": {"thread_id": "same-thread"}}

    async def pause():
        await first.ainvoke(
            {
                "messages": [{"role": "user", "content": "帮我做个性化配置"}],
                "profile_snapshot": {"risk_level": "R2"},
            },
            config,
            durability="sync",
        )
        return await first.aget_state(config)

    paused = asyncio.run(pause())
    payload = _interrupt_value(paused)
    assert "investment_horizon" in payload["missing_fields"]
    assert "budget_amount" in payload["missing_fields"]
    assert payload["profile"]["risk_level"] == "R2"
    assert "investment_goal" not in payload["profile"] or not payload["profile"].get("investment_goal")

    second = build_advisor_graph(model=model, specialists=specialists, checkpointer=saver, risk_of=lambda _code: None)
    confirmed = {
        "risk_level": "R2",
        "investment_horizon": "中期",
        "investment_goal": "均衡增长",
        "liquidity_need": "中",
        "budget_amount": 10000,
    }

    async def resume():
        await second.ainvoke(Command(resume=confirmed), config, durability="sync")
        return await second.aget_state(config)

    done = asyncio.run(resume())
    assert done.values["profile_snapshot"]["budget_amount"] == 10000
    assert SIMULATED_DISCLAIMER in done.values["final"].answer
    assert model.compliance_inputs


def test_suitability_blocks_higher_risk_and_rewrites_once():
    model = ScriptedModel(
        IntentResult(intents=[_intent("product_agent", kind="general")]),
        [
            "可以看看高风险产品。\n推荐代码: HIGH01",
            "只保留适配产品。\n推荐代码: LOW001",
        ],
    )
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent", content="两只产品", codes=["HIGH01", "LOW001"]),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    }, {"HIGH01": 5, "LOW001": 1})

    async def run():
        config = {"configurable": {"thread_id": "suitability"}}
        await graph.ainvoke(
            {
                "messages": [{"role": "user", "content": "比较两只基金"}],
                "profile_snapshot": {"risk_level": "R2"},
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    suitability = state.values["suitability"]
    assert suitability.blocked == ["HIGH01"]
    assert "LOW001" in suitability.allowed
    assert "HIGH01" not in (state.values["final"].recommendations or [])
    assert state.values["review"].rewrite_count == 1
    assert SIMULATED_DISCLAIMER in state.values["final"].answer


def test_second_violation_falls_back_to_a_safe_template():
    model = ScriptedModel(
        IntentResult(intents=[_intent("product_agent")]),
        ["推荐代码: HIGH01", "仍然推荐代码: HIGH01"],
    )
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent", codes=["HIGH01"]),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    }, {"HIGH01": 5})

    async def run():
        config = {"configurable": {"thread_id": "safe"}}
        await graph.ainvoke(
            {
                "messages": [{"role": "user", "content": "推荐"}],
                "profile_snapshot": {"risk_level": "R1"},
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    assert "HIGH01" not in state.values["final"].answer
    assert state.values["final"].recommendations == []
    assert SIMULATED_DISCLAIMER in state.values["final"].answer


def test_public_token_keeps_only_compliance_node_text():
    expert = SimpleNamespace(content="EXPERT_SECRET")
    final = SimpleNamespace(content="最终答案")
    assert public_token(expert, {"langgraph_node": "stock_agent"}) is None
    assert public_token(final, {"langgraph_node": "compliance_node"}) == "最终答案"


def test_stock_failed_flag_does_not_fail_turn_when_product_succeeds():
    model = ScriptedModel(
        IntentResult(intents=[_intent("stock_agent"), _intent("product_agent")]),
        ["商品结论可用。\n推荐代码: 110011"],
    )
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent", failed=True, warning="行情不可用"),
        "product_agent": _specialist("product_agent", content="产品可用", codes=["110011"]),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    }, {"110011": 2})

    async def run():
        config = {"configurable": {"thread_id": "stock-failed"}}
        await graph.ainvoke(
            {
                "messages": [{"role": "user", "content": "看股票和基金"}],
                "profile_snapshot": {"risk_level": "R3"},
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    values = state.values
    assert values["stock_result"].status == "failed"
    assert values["product_result"].status == "ok"
    assert SIMULATED_DISCLAIMER in values["final"].answer


def test_stock_and_product_failed_flags_fail_the_turn():
    model = ScriptedModel(IntentResult(intents=[_intent("stock_agent"), _intent("product_agent")]), [])
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent", failed=True),
        "product_agent": _specialist("product_agent", failed=True),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "都会失败"}]},
            {"configurable": {"thread_id": "both-failed"}},
            durability="sync",
        )

    with pytest.raises(AllBranchesFailed):
        asyncio.run(run())


def test_suitability_ignores_candidate_stocks():
    model = ScriptedModel(
        IntentResult(intents=[_intent("stock_agent"), _intent("product_agent")]),
        ["研究候选不是商品推荐。\n推荐代码: 无"],
    )
    graph = _graph(model, {
        "stock_agent": _specialist(
            "stock_agent",
            content="候选池",
            candidates=[{"thscode": "600519.SH", "name": "贵州茅台", "score": 90}],
        ),
        "product_agent": _specialist("product_agent", content="无商品", codes=[]),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    }, {"600519.SH": 5})

    async def run():
        config = {"configurable": {"thread_id": "stock-not-product"}}
        await graph.ainvoke(
            {
                "messages": [{"role": "user", "content": "选股"}],
                "profile_snapshot": {"risk_level": "R1"},
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    suitability = state.values["suitability"]
    assert "600519.SH" not in (suitability.blocked or [])
    assert "600519.SH" not in (suitability.allowed or [])
    assert "600519.SH" not in (state.values["final"].recommendations or [])


def test_compliance_node_gets_stock_summary_and_strips_trade_language():
    from app.agents.stock.contracts import StockResultV1

    model = ScriptedModel(
        IntentResult(intents=[_intent("stock_agent")]),
        ["建议买入并在买卖点加仓，目标价 2000，仓位比例 20%。\n推荐代码: 无"],
    )
    graph = _graph(model, {
        "stock_agent": _specialist(
            "stock_agent",
            content="上证综指上涨 0.8%，来源 fuyao。",
            stock_result=StockResultV1(
                scenario="market_overview",
                summary="指数偏强",
                as_of="2026-09-27T14:00:00+08:00",
                sources=["fuyao"],
                market_overview={
                    "indices": [{
                        "thscode": "000001.SH",
                        "change_pct": 0.8,
                        "source": "fuyao",
                        "as_of": "2026-09-27T14:00:00+08:00",
                    }],
                },
            ),
        ),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run():
        config = {"configurable": {"thread_id": "stock-guard"}}
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "看大盘"}], "profile_snapshot": {"risk_level": "R3"}},
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    human = next(message["content"] for message in model.compliance_inputs[-1] if message["role"] == "user")
    assert "上证综指上涨" in human
    assert "指数偏强" in human or "000001.SH" in human
    assert "000001.SH" in human
    text = state.values["final"].answer
    for token in ("买入", "卖出", "加仓", "减仓", "目标价", "买卖点", "仓位比例"):
        assert token not in text
    assert SIMULATED_DISCLAIMER in text


def _interrupt_value(snapshot):
    direct = getattr(snapshot, "interrupts", None) or ()
    if direct:
        return getattr(direct[0], "value", direct[0])
    for task in getattr(snapshot, "tasks", ()) or ():
        found = getattr(task, "interrupts", None) or ()
        if found:
            return getattr(found[0], "value", found[0])
    raise AssertionError("expected interrupt")


def test_followup_question_is_rewritten_and_routed():
    """多轮追问：指代消解后的任务进入意图分类，并路由到正确分支。"""
    model = ScriptedModel(
        IntentResult(intents=[Intent(agent="product_agent", task="查询110011基金近期业绩", kind="general")]),
        ["该基金近期业绩平稳。\n推荐代码: 无"],
        rewritten=RewrittenQuery(task="查询110011基金近期业绩", changed=True),
    )
    seen: list[str] = []

    async def product(task, state):
        seen.append(task)
        return await _specialist("product_agent", content="基金业绩可用")(task, state)

    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": product,
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run():
        config = {"configurable": {"thread_id": "rewrite-1"}}
        await graph.ainvoke(
            {
                "messages": [
                    {"role": "user", "content": "看看110011基金"},
                    {"role": "assistant", "content": "110011 是XX成长混合基金。"},
                    {"role": "user", "content": "那它最近表现怎么样？"},
                ],
                "profile_snapshot": {"risk_level": "R3"},
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    assert len(model.rewrite_inputs) == 1
    assert model.structured_method == "function_calling"
    assert seen == ["查询110011基金近期业绩"]
    assert state.values["resolved_task"] == "查询110011基金近期业绩"


def test_first_turn_skips_rewrite():
    """首轮没有上文可消解，不触发改写调用。"""
    model = ScriptedModel(
        IntentResult(intents=[_intent("chat_agent")]),
        ["普通问答。\n推荐代码: 无"],
    )
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run():
        config = {"configurable": {"thread_id": "rewrite-first"}}
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "什么是基金定投"}]},
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    assert model.rewrite_inputs == []
    assert state.values.get("resolved_task", "") == ""


def test_rewrite_failure_degrades_to_original_message():
    """改写调用失败不失败整轮：按原文继续并记录 warning。"""
    model = ScriptedModel(
        IntentResult(intents=[_intent("chat_agent")]),
        ["按原文回答。\n推荐代码: 无"],
    )
    parent = model

    class _BrokenStructured:
        async def ainvoke(self, messages, config=None):
            parent.rewrite_inputs.append(list(messages))
            raise RuntimeError("rewrite upstream down")

    original = model.with_structured_output

    def with_structured_output(schema, *, method=None, **kwargs):
        if schema is RewrittenQuery:
            return _BrokenStructured()
        return original(schema, method=method, **kwargs)

    model.with_structured_output = with_structured_output  # type: ignore[method-assign]
    graph = _graph(model, {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent"),
    })

    async def run():
        config = {"configurable": {"thread_id": "rewrite-broken"}}
        await graph.ainvoke(
            {
                "messages": [
                    {"role": "user", "content": "看看110011基金"},
                    {"role": "assistant", "content": "110011 是XX成长混合基金。"},
                    {"role": "user", "content": "那它最近表现怎么样？"},
                ],
            },
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())
    assert state.values["final"].answer
    warnings = [str(item) for item in state.values.get("turn_warnings") or []]
    assert any("问题理解" in item for item in warnings)
    assert parent.rewrite_inputs  # 改写确实被调用过


def test_results_do_not_leak_across_turns_when_channels_are_reset():
    """显式写 None 会清掉上一轮 checkpoint 里的通道值——_graph_input 依赖的行为。"""
    saver = MemorySaver()
    specialists = {
        "stock_agent": _specialist("stock_agent", content="行情可用"),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent"),
        "chat_agent": _specialist("chat_agent", content="普通回答"),
    }
    stock_graph = build_advisor_graph(
        model=ScriptedModel(IntentResult(intents=[_intent("stock_agent")]), ["行情轮\n推荐代码: 无"]),
        specialists=specialists, checkpointer=saver, risk_of=lambda _code: None,
    )
    chat_graph = build_advisor_graph(
        model=ScriptedModel(IntentResult(intents=[_intent("chat_agent")]), ["问答轮\n推荐代码: 无"]),
        specialists=specialists, checkpointer=saver, risk_of=lambda _code: None,
    )
    config = {"configurable": {"thread_id": "no-leak"}}
    reset = {
        "resolved_task": None, "intents": [],
        "stock_result": None, "product_result": None,
        "portfolio_result": None, "chat_result": None,
        "suitability": None, "review": None, "final": None, "turn_warnings": [],
    }

    async def run():
        await stock_graph.ainvoke(
            {"messages": [{"role": "user", "content": "看大盘"}], "profile_snapshot": {"risk_level": "R3"}},
            config, durability="sync",
        )
        first = await stock_graph.aget_state(config)
        await chat_graph.ainvoke(
            {"messages": [{"role": "user", "content": "你好"}],
             "profile_snapshot": {"risk_level": "R3"}, **reset},
            config, durability="sync",
        )
        return first, await chat_graph.aget_state(config)

    first, second = asyncio.run(run())
    assert first.values.get("stock_result") is not None
    assert second.values.get("stock_result") is None
    assert "问答轮" in second.values["final"].answer
