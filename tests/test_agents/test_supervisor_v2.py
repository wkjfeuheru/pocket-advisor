"""真实 v2 主图的任务隔离、依赖和输出门禁。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents.graphs.state import (
    ChatResult, Intent, IntentResult, ProductResult, TaskSpec, WorkerOutcome,
    merge_outcomes, validate_task_plan,
)


def test_result_replay_is_idempotent_and_same_domain_tasks_are_independent():
    a = WorkerOutcome(task_id="a", agent="product_agent", attempt=1, result=ProductResult(summary="A"))
    b = WorkerOutcome(task_id="b", agent="product_agent", attempt=1, result=ProductResult(summary="B"))
    values = merge_outcomes({}, {"a:1": a, "b:1": b})
    assert merge_outcomes(values, {"a:1": a}) == values
    with pytest.raises(ValueError):
        merge_outcomes(values, {"a:1": b})


def test_unknown_or_cyclic_dependencies_are_rejected():
    with pytest.raises(ValueError):
        validate_task_plan([TaskSpec(task_id="a", agent="chat_agent", task="x", dependencies=["missing"])])
    with pytest.raises(ValueError):
        validate_task_plan([
            TaskSpec(task_id="a", agent="chat_agent", task="x", dependencies=["b"]),
            TaskSpec(task_id="b", agent="chat_agent", task="y", dependencies=["a"]),
        ])


class Model:
    def __init__(self, intents, draft="你好", decision=None, pending_action="switch"):
        self.intents = intents
        self.draft = draft
        self.decision = decision
        self.pending_action = pending_action
        self.inputs = []

    def with_structured_output(self, schema, **kwargs):
        parent = self
        class Structured:
            async def ainvoke(self, messages, config=None):
                if schema is IntentResult:
                    return IntentResult(intents=parent.intents, pending_action=parent.pending_action)
                if schema.__name__ == "SupervisorDecision":
                    return schema.model_validate(parent.decision or {})
                if schema.__name__ == "AnswerPlan":
                    parent.inputs.append(messages)
                    return schema.model_validate({"sections": [{"points": [{"point": parent.draft}]}]})
                raise NotImplementedError(schema.__name__)
        return Structured()

    async def astream(self, messages, config=None):
        """可见阶段：按骨架成文（测试脚本直接用 draft 作为正文）。"""
        self.inputs.append(messages)
        yield SimpleNamespace(content=self.draft)


def test_same_domain_parallel_and_dependency_results_reach_worker():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        seen.append((state["task_spec"].task_id, state["dependency_results"]))
        return ChatResult(summary="已核查")
    model = Model([
        Intent(agent="chat_agent", task="A", task_id="a"),
        Intent(agent="chat_agent", task="B", task_id="b"),
        Intent(agent="chat_agent", task="C", task_id="c", dependencies=["a", "b"]),
    ])
    graph = build_advisor_graph(model=model, specialists={"chat_agent": worker})
    values = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "回答三个问题"}]}))
    assert len(values["outcomes"]) == 3
    assert values["batch_count"] == 2
    assert len(dict(seen)["c"]) == 2
    assert "回答三个问题" in str(model.inputs)


def test_unreferenced_numbers_and_promises_never_reach_final_answer():
    from app.agents.graphs.task_agent import build_advisor_graph
    async def worker(task, state):
        return ChatResult(summary="可解释风险")
    model = Model([Intent(agent="chat_agent", task="解释风险")], draft="保证收益达到 99%")
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "解释风险"}]},
    ))
    assert "99" not in values["final"].answer
    assert "保证收益" not in values["final"].answer


def test_missing_stock_slots_finishes_with_persisted_pending_task():
    from app.agents.graphs.task_agent import build_advisor_graph
    async def forbidden(task, state):
        raise AssertionError("缺失参数时不能进入 worker")
    model = Model([Intent(agent="stock_agent", task="帮我选股")])
    values = asyncio.run(build_advisor_graph(model=model, specialists={"stock_agent": forbidden}).ainvoke(
        {"messages": [{"role": "user", "content": "帮我选股"}]},
    ))
    assert values["session_context"]["pending_tasks"]
    assert values["batch_count"] == 0
    assert "策略" in values["final"].answer or "期限" in values["final"].answer


def test_cross_turn_symbol_timeframe_and_new_object_isolation():
    from app.agents.graphs.task_agent import build_advisor_graph
    observed = []
    async def worker(task, state):
        observed.append(state["domain_slots"])
        from app.agents.graphs.state import StockResult
        return StockResult(summary="已查询")
    context = {}
    for index, query in enumerate(["分析茅台", "看近一年", "分析宁德时代"]):
        graph = build_advisor_graph(model=Model([Intent(agent="stock_agent", task=query)]), specialists={"stock_agent": worker})
        values = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": query}], "session_context": context},
                                         {"configurable": {"request_id": str(index)}}))
        context = values["session_context"]
    assert observed[0]["symbols"] == ["茅台"]
    assert observed[1]["symbols"] == ["茅台"] and observed[1]["timeframe"]
    assert observed[2]["symbols"] == ["宁德时代"] and "timeframe" not in observed[2]
    assert len(context["slots"]["stock_agent"]["contexts"]) == 2


def test_normal_chat_completes_pending_slots_and_can_switch_topic():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        seen.append(state["domain_slots"])
        from app.agents.graphs.state import StockResult
        return StockResult(summary="已查询")
    first = asyncio.run(build_advisor_graph(model=Model([Intent(agent="stock_agent", task="分析股票")]), specialists={"stock_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "分析股票"}]}, {"configurable": {"request_id": "first"}}))
    pending = first["session_context"]
    assert pending["pending_tasks"]
    resumed = asyncio.run(build_advisor_graph(model=Model([Intent(agent="chat_agent", task="茅台")], pending_action="continue"), specialists={"stock_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "茅台"}], "session_context": pending}, {"configurable": {"request_id": "second"}}))
    assert seen[-1]["symbols"] == ["茅台"]
    assert not resumed["session_context"]["pending_tasks"]
    async def chat(task, state): return ChatResult(summary="你好")
    changed = asyncio.run(build_advisor_graph(model=Model([Intent(agent="chat_agent", task="你好")]), specialists={"chat_agent": chat}).ainvoke(
        {"messages": [{"role": "user", "content": "你好"}], "session_context": pending}, {"configurable": {"request_id": "third"}}))
    assert not changed["session_context"]["pending_tasks"]
    assert len(seen) == 1


def test_failed_dependency_skips_dependent_but_independent_task_completes():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        key = state["task_spec"].task_id
        seen.append(key)
        return ChatResult(status="failed" if key == "a" else "ok", summary="不可用" if key == "a" else "已完成")
    model = Model([Intent(agent="chat_agent", task="A", task_id="a"),
                   Intent(agent="chat_agent", task="B", task_id="b", dependencies=["a"]),
                   Intent(agent="chat_agent", task="C", task_id="c")])
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "三项查询"}]}))
    assert set(seen) == {"a", "c"}
    assert "未完成：B" in values["final"].answer


def test_identical_retry_without_new_method_is_rejected():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        seen.append(task)
        return ChatResult(status="degraded", summary="缺少证据")
    model = Model([Intent(agent="chat_agent", task="查询", task_id="a")],
                  decision={"retry_tasks": ["a"], "retry_queries": {"a": "查询"}, "reason": "再试一次"})
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "查询"}]}))
    assert seen == ["查询"]
    assert values["batch_count"] == 1


def test_execution_batch_budget_stops_long_dependency_chain():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        seen.append(state["task_spec"].task_id)
        return ChatResult(summary="完成")
    intents = [Intent(agent="chat_agent", task=key, task_id=key, dependencies=[] if index == 0 else [str(index-1)])
               for index, key in enumerate(map(str, range(5)))]
    values = asyncio.run(build_advisor_graph(model=Model(intents), specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "五项依赖任务"}]}))
    assert seen == ["0", "1", "2"]
    assert values["batch_count"] == 3 and "执行预算" in values["final"].answer


def test_chinese_financial_numbers_cannot_bypass_fact_references():
    from app.agents.graphs.task_agent import build_advisor_graph
    async def worker(task, state): return ChatResult(summary="风险说明")
    model = Model([Intent(agent="chat_agent", task="解释风险")], draft="该产品收益为百分之九十九。")
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "解释风险"}]}))
    assert "百分之九十九" not in values["final"].answer


def test_cancel_pending_task_takes_priority_over_followup_words():
    from app.agents.graphs.task_agent import build_advisor_graph
    async def worker(task, state): return ChatResult(summary="已取消")
    first = asyncio.run(build_advisor_graph(model=Model([Intent(agent="stock_agent", task="帮我选股")]), specialists={}).ainvoke(
        {"messages": [{"role": "user", "content": "帮我选股"}]}))
    second = asyncio.run(build_advisor_graph(model=Model([Intent(agent="chat_agent", task="取消刚才的选股")]), specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "取消刚才的选股"}], "session_context": first["session_context"]}))
    assert not second["session_context"]["pending_tasks"]


def test_punctuation_does_not_create_a_new_retry_method():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def worker(task, state):
        seen.append(task)
        return ChatResult(status="degraded", summary="无证据")
    model = Model([Intent(agent="chat_agent", task="查询", task_id="a")],
                  decision={"retry_tasks": ["a"], "retry_queries": {"a": "查询。"}, "reason": "再试一次"})
    asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "查询"}]}))
    assert seen == ["查询"]


def test_continue_without_answer_does_not_authorize_configuration():
    from app.agents.graphs.task_agent import build_advisor_graph
    calls = []
    def allocation(**kwargs):
        calls.append(kwargs)
        from app.agents.graphs.state import PortfolioResult
        return PortfolioResult(summary="完成")
    context = {}
    for index, query in enumerate(["重新配置", "继续"]):
        model = Model([Intent(agent="portfolio_agent", task=query)], pending_action="continue" if index else "switch")
        values = asyncio.run(build_advisor_graph(model=model, specialists={}, allocation_runner=allocation).ainvoke(
            {"messages": [{"role": "user", "content": query}], "session_context": context},
            {"configurable": {"request_id": str(index), "customer_id": "CUST1"}}))
        context = values["session_context"]
    assert not calls and context["pending_tasks"]


def test_same_domain_tasks_extract_their_own_symbol_and_timeframe():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = {}
    async def worker(task, state):
        seen[state["task_spec"].task_id] = state["domain_slots"]
        from app.agents.graphs.state import StockResult
        return StockResult(summary="查询完成")
    model = Model([Intent(agent="stock_agent", task="分析茅台近一年", task_id="a"),
                   Intent(agent="stock_agent", task="分析宁德时代近三个月", task_id="b")])
    values = asyncio.run(build_advisor_graph(model=model, specialists={"stock_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "分析茅台近一年，分析宁德时代近三个月"}]}))
    assert seen["a"]["symbols"] == ["茅台"] and seen["b"]["symbols"] == ["宁德时代"]
    assert seen["a"]["timeframe"]["start"] != seen["b"]["timeframe"]["start"]
    assert len(values["session_context"]["slots"]["stock_agent"]["contexts"]) == 2


def test_v2_profile_and_allocation_resume_on_new_graph_without_reexecuting_readonly_work():
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.graphs.state import PortfolioResult
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command, interrupt
    saver = MemorySaver()
    calls = []
    chosen = []
    async def chat(task, state):
        calls.append(task)
        return ChatResult(summary="诊断信息已查询")
    def allocation(**kwargs):
        answer = kwargs["interrupt"]({"kind": "allocation", "plans": [{"plan_id": "p1"}]})
        chosen.append(answer["plan_id"])
        return PortfolioResult(summary="用户已选择方案")
    model = Model([Intent(agent="chat_agent", task="说明风险", task_id="info"),
                   Intent(agent="portfolio_agent", task="生成配置方案", task_id="plan", dependencies=["info"], kind="personalized")])
    config = {"configurable": {"thread_id": "v2-pause", "customer_id": "CUST1", "request_id": "r1"}}
    def graph(): return build_advisor_graph(model=model, specialists={"chat_agent": chat}, allocation_runner=allocation, checkpointer=saver)
    async def run():
        first = graph()
        await first.ainvoke({"messages": [{"role": "user", "content": "先说明风险再生成配置方案"}]}, config)
        snapshot = await first.aget_state(config)
        assert snapshot.interrupts[0].value["kind"] == "profile"
        second = graph()
        profile = {"risk_level": "R2", "investment_horizon": "中期", "investment_goal": "收益", "liquidity_need": "中"}
        await second.ainvoke(Command(resume=profile), config)
        snapshot = await second.aget_state(config)
        assert snapshot.interrupts[0].value["kind"] == "allocation"
        third = graph()
        await third.ainvoke(Command(resume={"plan_id": "p1"}), config)
        return (await third.aget_state(config)).values
    values = asyncio.run(run())
    assert calls == ["说明风险"] and chosen == ["p1"]
    assert values["outcomes"]["plan:1"].result.status == "ok"


def test_multiple_pending_tasks_preserve_all_dependency_definitions_and_only_answer_one():
    from app.agents.graphs.task_agent import build_advisor_graph
    seen = []
    async def chat(task, state):
        seen.append(state["task_spec"].task_id)
        return ChatResult(summary="已完成查询")
    async def stock(task, state):
        seen.append(state["task_spec"].task_id)
        from app.agents.graphs.state import StockResult
        return StockResult(summary="已完成分析")
    tasks = [Intent(agent="chat_agent", task="查询前置A", task_id="a"),
             Intent(agent="stock_agent", task="分析股票", task_id="b", dependencies=["a"]),
             Intent(agent="chat_agent", task="查询前置C", task_id="c"),
             Intent(agent="product_agent", task="比较基金", task_id="d", dependencies=["c"])]
    first = asyncio.run(build_advisor_graph(model=Model(tasks), specialists={"chat_agent": chat, "stock_agent": stock}).ainvoke(
        {"messages": [{"role": "user", "content": "查询前置后分析股票并比较基金"}]}))
    assert len(first["session_context"]["pending_tasks"]) == 2
    second = asyncio.run(build_advisor_graph(model=Model([Intent(agent="chat_agent", task="茅台")], pending_action="continue"),
                        specialists={"chat_agent": chat, "stock_agent": stock}).ainvoke(
        {"messages": [{"role": "user", "content": "茅台"}], "session_context": first["session_context"]}))
    assert seen.count("c") == 1
    assert "b" in seen
    assert [item["task"]["task_id"] for item in second["session_context"]["pending_tasks"]] == ["d"]
    assert second["session_context"]["pending_tasks"][0]["dependency_tasks"][0]["task_id"] == "c"


def test_worker_clarification_fields_keep_second_deferred_task_pending():
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.evidence import NeedsClarification
    from app.agents.graphs.state import StockResult
    calls = []
    async def worker(task, state):
        key = state["task_spec"].task_id
        calls.append(key)
        if not state["domain_slots"].get("horizon"):
            raise NeedsClarification({"fields": ["horizon"], "question": "请提供研究期限。"})
        return StockResult(summary="已分析")
    intents = [Intent(agent="stock_agent", task="分析茅台", task_id="a"),
               Intent(agent="stock_agent", task="分析宁德时代", task_id="b")]
    first = asyncio.run(build_advisor_graph(model=Model(intents), specialists={"stock_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "分析茅台和宁德时代"}]}))
    assert all(p["missing"] == ["horizon"] for p in first["session_context"]["pending_tasks"])
    second = asyncio.run(build_advisor_graph(model=Model([Intent(agent="stock_agent", task="短期")], pending_action="continue"), specialists={"stock_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "短期"}], "session_context": first["session_context"]}))
    assert calls.count("b") == 1
    assert [p["task"]["task_id"] for p in second["session_context"]["pending_tasks"]] == ["b"]


def test_pending_product_answer_does_not_replace_stock_dependency_slots():
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.graphs.state import StockResult
    stock_slots, product_slots = [], []
    async def stock(task, state):
        stock_slots.append(state["domain_slots"])
        return StockResult(summary="已分析")
    async def product(task, state):
        product_slots.append(state["domain_slots"])
        return ProductResult(summary="已比较")
    intents = [Intent(agent="stock_agent", task="分析茅台", task_id="a"),
               Intent(agent="product_agent", task="比较基金", task_id="b", dependencies=["a"])]
    first = asyncio.run(build_advisor_graph(model=Model(intents), specialists={"stock_agent": stock, "product_agent": product}).ainvoke(
        {"messages": [{"role": "user", "content": "分析茅台再比较基金"}]}))
    second = asyncio.run(build_advisor_graph(model=Model([Intent(agent="product_agent", task="华夏基金、易方达基金")], pending_action="continue"),
                        specialists={"stock_agent": stock, "product_agent": product}).ainvoke(
        {"messages": [{"role": "user", "content": "华夏基金、易方达基金"}], "session_context": first["session_context"]}))
    assert len(stock_slots) == 2 and all(s["symbols"] == ["茅台"] for s in stock_slots)
    assert product_slots[0]["symbols"] == ["华夏基金", "易方达基金"]
    assert not second["session_context"]["pending_tasks"]


def test_explicit_generation_answer_to_operation_question_is_accepted():
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.graphs.state import PortfolioResult
    first = asyncio.run(build_advisor_graph(model=Model([Intent(agent="portfolio_agent", task="重新配置")]), specialists={}).ainvoke(
        {"messages": [{"role": "user", "content": "重新配置"}]}))
    calls = []
    def allocation(**kwargs):
        calls.append(kwargs)
        return PortfolioResult(summary="方案已生成")
    profile = {"risk_level": "R2", "investment_horizon": "中期", "investment_goal": "收益", "liquidity_need": "中"}
    values = asyncio.run(build_advisor_graph(model=Model([Intent(agent="portfolio_agent", task="生成方案")], pending_action="continue"),
                        specialists={}, allocation_runner=allocation).ainvoke(
        {"messages": [{"role": "user", "content": "生成方案"}], "session_context": first["session_context"], "profile_snapshot": profile},
        {"configurable": {"customer_id": "CUST1"}}))
    assert len(calls) == 1 and not values["session_context"]["pending_tasks"]


def test_feedback_additional_same_domain_tasks_have_distinct_contexts():
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.graphs.state import StockResult
    async def chat(task, state): return ChatResult(summary="已解释")
    async def stock(task, state): return StockResult(summary="已分析")
    model = Model([Intent(agent="chat_agent", task="解释概念", task_id="a")], decision={
        "additional_tasks": [{"agent": "stock_agent", "task": "分析茅台", "task_id": "b"},
                             {"agent": "stock_agent", "task": "分析宁德时代", "task_id": "c"}]})
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": chat, "stock_agent": stock}).ainvoke(
        {"messages": [{"role": "user", "content": "解释概念并分析茅台和宁德时代"}]}))
    contexts = values["session_context"]["slots"]["stock_agent"]["contexts"]
    assert len(contexts) == 2 and "" not in contexts
    assert {tuple(item["slots"]["symbols"]) for item in contexts.values()} == {("茅台",), ("宁德时代",)}
