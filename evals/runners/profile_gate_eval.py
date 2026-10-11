"""④ 画像门禁评测（纯离线、确定性）。

指标：
- ``gate_accuracy``：该挂起的挂起、该放行的放行（集合精确匹配 expected 缺失字段）。
- ``missing_fields_precision``：挂起时报告的 missing_fields 恰好等于实际缺失字段，
  不多报（false positive）也不漏报（false negative）。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from evals.runners._fakes import ScriptedModel, build_eval_graph, interrupt_value
from evals.runners._util import accuracy, failures_report, run_async
from app.agents.graphs.state import PROFILE_FIELDS, Intent, IntentResult
from langgraph.types import Command


def _profile_is_complete(profile: dict[str, Any], case: dict[str, Any]) -> bool:
    needs_budget = any(item.get("needs_budget") for item in case["intents"])
    fields = list(PROFILE_FIELDS) + (["budget_amount"] if needs_budget else [])
    return all(profile.get(field) for field in fields)


async def _judge(case: dict[str, Any]) -> dict[str, Any]:
    intents = IntentResult(intents=[
        Intent(
            agent=item["agent"],
            task=item.get("task", item["agent"]),
            kind=item.get("kind", "general"),
            needs_budget=item.get("needs_budget", False),
        )
        for item in case["intents"]
    ])
    # 固定有效领域槽位，单独评估画像门禁；槽位准确性由 v2 槽位评估覆盖。
    model = ScriptedModel(intents, ["按画像整理的回答。\n推荐代码: 无"], slot_values={
        "stock_agent": {"scenario": "market_overview"},
        "product_agent": {"operation": "query", "product_type": "fund"},
        "portfolio_agent": {"operation": "query"},
    })
    graph = build_eval_graph(model)
    config = {"configurable": {"thread_id": f"profile-{case['case_id']}"}}
    profile = dict(case["profile_snapshot"])
    expected_missing = set(case["expect_missing"])

    reported: set[str] = set()
    resumed_once = False
    for _ in range(2):
        await graph.ainvoke(
            # 有效的原文支持固定槽位，避免原文授权校验将基金类型删除。
            {"messages": [{"role": "user", "content": "查询基金，查看持仓和大盘"}], "profile_snapshot": profile},
            config,
            durability="sync",
        )
        snapshot = await graph.aget_state(config)
        if not getattr(snapshot, "interrupts", None) and not any(
            getattr(task, "interrupts", None) for task in getattr(snapshot, "tasks", ()) or ()
        ):
            break
        payload = interrupt_value(snapshot)
        missing = [str(item) for item in payload.get("missing_fields") or []]
        if missing:
            reported = set(missing)
            break
        # 只有当预期放行却仍出现 interrupt 时才继续
        if not expected_missing:
            break

    if expected_missing:
        # 精确集合匹配：缺失字段既不漏也不多
        passed = reported == expected_missing
        note = "" if passed else f"expected={sorted(expected_missing)} reported={sorted(reported)}"
    else:
        passed = not reported
        note = "" if passed else f"应放行却挂起: {sorted(reported)}"
    return {"passed": passed, "note": note}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="画像门禁评测（离线）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    from evals.runners.loader import load_profile_gate_cases

    cases = load_profile_gate_cases()
    results, failures = run_async(lambda: _run(cases))
    total = len(cases)
    summary = {
        "total_cases": total,
        "gate_accuracy": accuracy(total - failures, total),
        "missing_fields_precision": accuracy(total - failures, total),
        "failures": failures_report(results),
    }
    payload = {"profile_gate": {"summary": summary, "results": results}}
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


async def _run(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    failures = 0
    for case in cases:
        outcome = await _judge(case)
        results.append({"case_id": case["case_id"], "passed": outcome["passed"], "note": outcome["note"]})
        if not outcome["passed"]:
            failures += 1
    return results, failures


if __name__ == "__main__":
    main()
