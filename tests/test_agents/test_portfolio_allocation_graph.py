"""持仓配置分支在真图上的挂起与跨进程 resume。

照 ``test_advisor_graph.py`` 既有的 profile 中断测法：**用同一个 checkpointer 重建第二张图**
再 resume，证明挂起状态完全落在 checkpoint 里，而不是进程内存。

这是"用户选定方案"这条链路的端到端证明：真图 → interrupt → Command(resume) → 采纳。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.graphs.state import IntentResult
from app.agents.graphs.task_agent import build_advisor_graph

from tests.test_agents.test_advisor_graph import ScriptedModel, _intent, _interrupt_value, _specialist


class _RecordingRunner:
    """替代真实持仓配置分支：挂起时交给 ``interrupt``，resume 后返回采纳结果。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.adopted: list[str] = []

    def __call__(self, *, customer_id, state, interrupt):
        self.calls.append({"customer_id": customer_id, "state": dict(state)})
        payload = {
            "kind": "allocation",
            "agent_version": "portfolio-allocation-v1",
            "plans": [
                {
                    "plan_id": "p-light",
                    "tier": "light",
                    "label": "微调",
                    "turnover_rate": 0.05,
                    "estimated_fee": 12.5,
                    "items": [],
                    "limitations": [],
                },
                {
                    "plan_id": "p-moderate",
                    "tier": "moderate",
                    "label": "中度",
                    "turnover_rate": 0.30,
                    "estimated_fee": 40.0,
                    "items": [],
                    "limitations": [],
                },
            ],
        }
        chosen = interrupt(payload)
        self.adopted.append(str(chosen.get("plan_id")))
        return {
            "agent": "portfolio_agent",
            "content": f"已采纳方案 {chosen.get('plan_id')}",
            "artifacts": [{"scenario": "allocation_adopted", "allocation_adopted": chosen}],
            "warnings": [],
            "failed": False,
        }


def _graph(model, runner, saver):
    specialists = {
        "stock_agent": _specialist("stock_agent"),
        "product_agent": _specialist("product_agent"),
        "portfolio_agent": _specialist("portfolio_agent", content="组合说明"),
        "chat_agent": _specialist("chat_agent"),
    }
    return build_advisor_graph(
        model=model,
        specialists=specialists,
        checkpointer=saver,
        risk_of=lambda _code: None,
        allocation_runner=runner,
    )


def _portfolio_model():
    return ScriptedModel(
        IntentResult(intents=[_intent("portfolio_agent")]),
        ["按您的画像说明配置思路。\n推荐代码: 无"],
    )


CONFIG = {"configurable": {"thread_id": "alloc-thread", "customer_id": "CUST000001"}}


def test_the_graph_suspends_on_the_allocation_branch_and_resumes_on_a_second_graph():
    runner = _RecordingRunner()
    saver = MemorySaver()
    first = _graph(_portfolio_model(), runner, saver)

    async def pause():
        await first.ainvoke(
            {"messages": [{"role": "user", "content": "帮我看看持仓怎么调"}]},
            CONFIG,
            durability="sync",
        )
        return await first.aget_state(CONFIG)

    paused = asyncio.run(pause())

    # 挂起在方案选择上，载荷里带两个候选
    payload = _interrupt_value(paused)
    assert payload["kind"] == "allocation"
    assert [p["plan_id"] for p in payload["plans"]] == ["p-light", "p-moderate"]
    assert runner.calls and runner.calls[0]["customer_id"] == "CUST000001"
    assert runner.adopted == []

    # 换一张图（同 checkpointer）resume：证明挂起状态在 checkpoint 里
    second = _graph(_portfolio_model(), runner, saver)

    async def resume():
        await second.ainvoke(
            Command(resume={"plan_id": "p-moderate"}), CONFIG, durability="sync"
        )
        return await second.aget_state(CONFIG)

    done = asyncio.run(resume())

    assert runner.adopted == ["p-moderate"]
    # 采纳结果落在 agent_results 里（分支是并行 Send 出去的子任务）；
    # final_result 由 aggregator 产出，不该指望它复述分支内容。
    results = list(done.values.get("agent_results") or [])
    adopted = [
        artifact
        for result in results
        for artifact in (result.get("artifacts") or [])
        if artifact.get("scenario") == "allocation_adopted"
    ]
    assert adopted, f"没有采纳产物；agent_results={results}"
    assert adopted[0]["allocation_adopted"]["plan_id"] == "p-moderate"


def test_without_a_runner_the_portfolio_node_stays_a_plain_specialist():
    """不传 allocation_runner 时必须保持原行为——不能让既有链路突然开始挂起。"""
    model = _portfolio_model()
    saver = MemorySaver()
    graph = build_advisor_graph(
        model=model,
        specialists={
            "stock_agent": _specialist("stock_agent"),
            "product_agent": _specialist("product_agent"),
            "portfolio_agent": _specialist("portfolio_agent", content="组合说明"),
            "chat_agent": _specialist("chat_agent"),
        },
        checkpointer=saver,
        risk_of=lambda _code: None,
    )

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "帮我看看持仓"}]},
            CONFIG,
            durability="sync",
        )
        return await graph.aget_state(CONFIG)

    state = asyncio.run(run())

    # 没有挂起：直接跑完
    assert state.next == ()
    results = list(state.values.get("agent_results") or [])
    contents = " ".join(str(r.get("content") or "") for r in results)
    assert "组合说明" in contents, f"agent_results={results}"


def test_a_missing_customer_id_falls_back_instead_of_interrupting():
    """没有可信客户上下文时不挂起——否则 resume 无法定位账户，用户会卡住。"""
    runner = _RecordingRunner()
    saver = MemorySaver()
    graph = _graph(_portfolio_model(), runner, saver)
    config = {"configurable": {"thread_id": "no-customer-thread"}}

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "帮我看看持仓"}]},
            config,
            durability="sync",
        )
        return await graph.aget_state(config)

    state = asyncio.run(run())

    assert state.next == ()
    assert runner.calls == []
    results = list(state.values.get("agent_results") or [])
    contents = " ".join(str(r.get("content") or "") for r in results)
    assert "组合说明" in contents, f"agent_results={results}"


def test_a_failing_allocation_runner_falls_back_to_the_specialist():
    """分支内部抛**可降级**异常时退回普通分支，而不是把整轮打崩。

    注意 ``AllBranchesFailed`` 是刻意不可降级的（它表示"所有分支都失败了"），
    所以这里用一个普通运行时异常来验证降级路径。
    """

    def _boom(*, customer_id, state, interrupt):
        raise RuntimeError("分支内部故障")

    saver = MemorySaver()
    graph = _graph(_portfolio_model(), _boom, saver)

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "帮我看看持仓"}]},
            CONFIG,
            durability="sync",
        )
        return await graph.aget_state(CONFIG)

    state = asyncio.run(run())

    assert state.next == ()
    results = list(state.values.get("agent_results") or [])
    contents = " ".join(str(r.get("content") or "") for r in results)
    assert "组合说明" in contents, f"降级后应产出普通分支结果；agent_results={results}"


def test_a_non_degradable_error_still_propagates():
    """不可降级的异常必须继续往上抛——降级不能变成吞掉严重错误。"""
    from app.agents.graphs.task_agent import AllBranchesFailed

    def _boom(*, customer_id, state, interrupt):
        raise AllBranchesFailed("分支不可用")

    saver = MemorySaver()
    graph = _graph(_portfolio_model(), _boom, saver)

    async def run():
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "帮我看看持仓"}]},
            CONFIG,
            durability="sync",
        )

    with pytest.raises(AllBranchesFailed):
        asyncio.run(run())
