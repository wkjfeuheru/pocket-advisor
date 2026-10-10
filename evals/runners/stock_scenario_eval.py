"""⑤ 股票场景分类准确率评测（live：真实模型 + 假数据源）。

指标：
- ``scenario_accuracy``：三场景分类正确率。
- ``per_scenario_recall``：各场景的召回。
- ``strategy_accuracy`` / ``horizon_accuracy``：子字段抽取正确率（在标注了期望的用例上）。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from evals.runners._live import LiveHarness, LiveSkip
from evals.runners._util import accuracy, failures_report, run_async
from evals.runners.loader import load_scenario_cases


async def _classify(harness: LiveHarness, message: str) -> dict[str, Any]:
    """直接驱动场景分类（与 stock agent 的二级分类同 prompt 同 schema）。"""
    from app.agents.prompts.task_agent_prompts import STOCK_CLASSIFY_PROMPT
    from app.agents.stock.contracts import ScenarioClassification

    structured = harness.model.with_structured_output(ScenarioClassification, method="function_calling")
    result = await structured.ainvoke([
        {"role": "system", "content": STOCK_CLASSIFY_PROMPT},
        {"role": "user", "content": message},
    ])
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


def _judge(case: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, str]:
    notes: list[str] = []
    passed = True
    if actual.get("scenario") != case["expect_scenario"]:
        passed = False
        notes.append(f"scenario={actual.get('scenario')} 期望 {case['expect_scenario']}")
    strategy = case.get("expect_strategy")
    if strategy is not None and actual.get("strategy") != strategy:
        passed = False
        notes.append(f"strategy={actual.get('strategy')} 期望 {strategy}")
    horizon = case.get("expect_horizon")
    if horizon is not None and actual.get("horizon") != horizon:
        passed = False
        notes.append(f"horizon={actual.get('horizon')} 期望 {horizon}")
    count = case.get("expect_candidate_count")
    if count is not None and int(actual.get("candidate_count") or 0) != count:
        passed = False
        notes.append(f"candidate_count={actual.get('candidate_count')} 期望 {count}")
    if case.get("expect_filter_by_profile") is not None:
        if bool(actual.get("filter_by_profile")) != bool(case["expect_filter_by_profile"]):
            passed = False
            notes.append(f"filter_by_profile={actual.get('filter_by_profile')}")
    symbols = case.get("expect_symbols_contains") or []
    raw_symbols = " ".join(str(item) for item in actual.get("symbols") or [])
    missing_symbols = [item for item in symbols if item not in raw_symbols]
    if missing_symbols:
        passed = False
        notes.append(f"symbols 缺 {missing_symbols}: {raw_symbols!r}")
    industries = case.get("expect_industries_contains") or []
    raw_industries = " ".join(str(item) for item in actual.get("industries") or [])
    missing_industries = [item for item in industries if item not in raw_industries]
    if missing_industries:
        passed = False
        notes.append(f"industries 缺 {missing_industries}: {raw_industries!r}")
    index_scope = case.get("expect_index_scope_contains") or []
    raw_scope = " ".join(str(item) for item in actual.get("index_scope") or [])
    missing_scope = [item for item in index_scope if item not in raw_scope]
    if missing_scope:
        passed = False
        notes.append(f"index_scope 缺 {missing_scope}: {raw_scope!r}")
    return passed, "; ".join(notes)


async def _run(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    harness = LiveHarness()
    results: list[dict[str, Any]] = []
    scenario_rows: dict[str, list[bool]] = {}
    strategy_rows: list[dict[str, bool]] = []
    horizon_rows: list[dict[str, bool]] = []
    for case in cases:
        try:
            actual = await _classify(harness, case["message"])
        except Exception as exc:  # noqa: BLE001
            results.append({"case_id": case["case_id"], "passed": False,
                            "note": f"{type(exc).__name__}: {exc}"})
            scenario_rows.setdefault(case["expect_scenario"], []).append(False)
            continue
        passed, note = _judge(case, actual)
        results.append({"case_id": case["case_id"], "passed": passed, "note": note})
        scenario_rows.setdefault(case["expect_scenario"], []).append(
            actual.get("scenario") == case["expect_scenario"]
        )
        if case.get("expect_strategy"):
            strategy_rows.append({"passed": actual.get("strategy") == case["expect_strategy"]})
        if case.get("expect_horizon"):
            horizon_rows.append({"passed": actual.get("horizon") == case["expect_horizon"]})

    def _rate(rows: list[dict[str, bool]]) -> float:
        return accuracy(sum(1 for item in rows if item["passed"]), len(rows))

    extra = {
        "scenario_accuracy": _rate([{"passed": item["passed"]} for item in results]),
        "per_scenario_recall": {
            name: accuracy(sum(1 for hit in rows if hit), len(rows))
            for name, rows in sorted(scenario_rows.items())
        },
        "strategy_accuracy": _rate(strategy_rows) if strategy_rows else None,
        "horizon_accuracy": _rate(horizon_rows) if horizon_rows else None,
    }
    failures = sum(1 for item in results if not item["passed"])
    return results, failures, extra


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="股票场景分类评测（live）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    cases = load_scenario_cases()
    try:
        results, failures, extra = run_async(lambda: _run(cases))
    except LiveSkip as exc:
        payload = {"stock_scenarios": {"status": "SKIP", "reason": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    summary = {
        "status": "OK",
        "total_cases": len(cases),
        **extra,
        "failures": failures_report(results),
    }
    payload = {"stock_scenarios": {"summary": summary, "results": results}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
