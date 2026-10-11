"""v2 编排的离线行为门槛，不调用外部模型或真实行情。"""
from __future__ import annotations

import asyncio
import json

from app.agents.graphs.state import ChatResult, Intent, IntentResult, StockResult
from app.agents.graphs.task_agent import build_advisor_graph
from evals.runners._fakes import ScriptedModel


async def evaluate():
    checks = []
    context = {}
    seen = []
    async def stock(task, state):
        seen.append(state["domain_slots"])
        return StockResult(summary="已查询")
    for query in ("分析茅台", "看近一年", "分析宁德时代"):
        graph = build_advisor_graph(model=ScriptedModel(IntentResult(intents=[Intent(agent="stock_agent", task=query)]), ["已整理查询结果。"]),
                                   specialists={"stock_agent": stock})
        values = await graph.ainvoke({"messages": [{"role": "user", "content": query}], "session_context": context})
        context = values["session_context"]
    checks.append({"case_id": "followup_inherits", "passed": len(seen) == 3 and seen[1].get("symbols") == ["茅台"] and bool(seen[1].get("timeframe"))})
    checks.append({"case_id": "new_object_clears", "passed": len(seen) == 3 and seen[2].get("symbols") == ["宁德时代"] and not seen[2].get("timeframe")})
    calls = []
    async def chat(task, state):
        calls.append((state["task_spec"].task_id, len(state["dependency_results"])))
        return ChatResult(summary="已处理")
    tasks = [Intent(agent="chat_agent", task="解释概念", task_id="a"),
             Intent(agent="chat_agent", task="解释另一概念", task_id="b"),
             Intent(agent="chat_agent", task="综合解释", task_id="c", dependencies=["a", "b"])]
    values = await build_advisor_graph(model=ScriptedModel(IntentResult(intents=tasks), ["已整理。"]), specialists={"chat_agent": chat}).ainvoke(
        {"messages": [{"role": "user", "content": "解释概念并综合比较"}]})
    checks.append({"case_id": "same_domain_isolation", "passed": len(values["outcomes"]) == 3})
    checks.append({"case_id": "dependency_inputs", "passed": dict(calls).get("c") == 2 and values["batch_count"] == 2})
    model = ScriptedModel(IntentResult(intents=[Intent(agent="chat_agent", task="解释风险")]), ["保证收益 99%。", "收益为百分之九十九。"])
    values = await build_advisor_graph(model=model, specialists={"chat_agent": chat}).ainvoke(
        {"messages": [{"role": "user", "content": "解释风险"}]})
    checks.append({"case_id": "fact_gate", "passed": "99" not in values["final"].answer and "百分之九十九" not in values["final"].answer})
    values = await build_advisor_graph(model=ScriptedModel(IntentResult(intents=[Intent(agent="stock_agent", task="帮我选股")]), ["需要补充信息。"]), specialists={}).ainvoke(
        {"messages": [{"role": "user", "content": "帮我选股"}]})
    checks.append({"case_id": "missing_slots_no_worker", "passed": bool(values["session_context"]["pending_tasks"]) and values["batch_count"] == 0})
    return checks


def main(argv=None):
    checks = asyncio.run(evaluate())
    payload = {"supervisor_v2": {"summary": {"total_cases": len(checks),
        "all_pass_rate": sum(c["passed"] for c in checks) / len(checks),
        "failures": [c for c in checks if not c["passed"]]}, "results": checks}}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
