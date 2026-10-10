"""② 多轮改写准确率评测（live：真实模型 + 假数据源）。

指标：
- ``rewrite_accuracy``：需要改写的用例，resolved_task 包含全部期望实体
  且不残留指代词。
- ``no_rewrite_precision``：expect_rewrite=false 的用例，模型应原样放行
  （resolved_task 为空或与原句等价）。
- ``routing_after_rewrite_accuracy``：改写后的任务被路由到期望分支。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from evals.runners._live import LiveHarness, LiveSkip, observed_agents
from evals.runners._util import accuracy, failures_report, run_async
from evals.runners.loader import load_multi_turn_cases

_ANAPHORA = ("它", "它们", "那只", "这只", "那里面", "这些", "上面提到的")


def _judge(case: dict[str, Any], observation: dict[str, Any]) -> tuple[bool, str]:
    resolved = observation.get("resolved_task") or ""
    last_user = next(
        (m["content"] for m in reversed(case["messages"]) if m.get("role") == "user"), ""
    )
    if not case.get("expect_rewrite", True):
        # 原样放行：resolved_task 为空，或与最后一条用户消息等价（无指代引入）
        if not resolved or resolved.strip() == last_user.strip():
            return True, ""
        residual = [word for word in _ANAPHORA if word in resolved]
        if residual:
            return False, f"无需改写却引入/保留指代 {residual}: {resolved}"
        return True, f"改写但无指代（允许）: {resolved}"

    if not resolved:
        return False, "应改写但 resolved_task 为空"
    missing = [kw for kw in case.get("expect_contains") or [] if kw not in resolved]
    if missing:
        return False, f"缺关键实体 {missing}: {resolved}"
    residual = [word for word in case.get("forbidden_anaphora") or [] if word in resolved]
    if residual:
        return False, f"残留指代 {residual}: {resolved}"
    return True, ""


async def _run(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    harness = LiveHarness()
    results: list[dict[str, Any]] = []
    rewrite_rows: list[dict[str, bool]] = []
    no_rewrite_rows: list[dict[str, bool]] = []
    routing_rows: list[dict[str, bool]] = []
    for case in cases:
        observation = await harness.send_messages(
            case["messages"], thread_id=f"rewrite-{case['case_id']}"
        )
        passed, note = _judge(case, observation)
        row = {"case_id": case["case_id"], "passed": passed, "note": note}
        if case.get("expect_rewrite", True):
            rewrite_rows.append({"passed": passed})
            expected_agents = set(case.get("expect_agents") or [])
            if expected_agents:
                routing_rows.append({"passed": expected_agents & set(observed_agents(observation)) == expected_agents})
        else:
            no_rewrite_rows.append({"passed": passed})
        results.append(row)

    def _rate(rows: list[dict[str, bool]]) -> float:
        return accuracy(sum(1 for item in rows if item["passed"]), len(rows))

    extra = {
        "rewrite_accuracy": _rate(rewrite_rows),
        "rewrite_cases": len(rewrite_rows),
        "no_rewrite_precision": _rate(no_rewrite_rows),
        "no_rewrite_cases": len(no_rewrite_rows),
        "routing_after_rewrite_accuracy": _rate(routing_rows),
    }
    failures = sum(1 for item in results if not item["passed"])
    return results, failures, extra


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="多轮改写评测（live）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    cases = load_multi_turn_cases()
    try:
        results, failures, extra = run_async(lambda: _run(cases))
    except LiveSkip as exc:
        payload = {"multi_turn": {"status": "SKIP", "reason": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    summary = {
        "status": "OK",
        "total_cases": len(cases),
        **extra,
        "failures": failures_report(results),
    }
    payload = {"multi_turn": {"summary": summary, "results": results}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
