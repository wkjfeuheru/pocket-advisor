"""③ 信息缺失追问率评测（live：真实模型 + 假数据源 + 脚本化 resume）。

指标：
- ``clarify_recall``：缺参数用例必须以 kind=clarification 的 interrupt 结束。
- ``false_clarify_rate``：信息齐全的用例误触发澄清的比例（越低越好）。
- ``clarify_field_coverage``：澄清载荷 fields 命中预期缺失字段的比例。
- ``hallucination_rate``：该澄清却直接给出「答案」的比例（最严重失败）。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from evals.runners._live import LiveHarness, LiveSkip
from evals.runners._util import accuracy, failures_report, run_async
from evals.runners.loader import load_clarify_cases


def _clarify_fields(interrupt_payload: dict[str, Any] | None) -> list[str]:
    if not interrupt_payload:
        return []
    return [str(item) for item in interrupt_payload.get("fields") or []]


def _judge(case: dict[str, Any], observation: dict[str, Any]) -> tuple[bool, str]:
    expected = case["expect"]
    payload = observation.get("interrupt")
    asked = bool(payload and payload.get("kind") == "clarification")
    fields = _clarify_fields(payload)

    if expected == "clarify":
        if not asked:
            # 允许非澄清 interrupt（如同名歧义 options）视为追问，但字段校验照常
            if payload:
                asked = True
                fields = list(payload.get("fields") or [])
            else:
                return False, "该追问却直接回答（漏追问/幻觉）"
        expected_fields = set(case.get("expect_fields") or [])
        if expected_fields and not expected_fields & set(fields):
            return False, f"澄清字段未命中期望 {sorted(expected_fields)}: {fields}"
        return True, ""
    # expect == "answer"：信息齐全不应追问
    if asked:
        return False, f"信息齐全却追问（误追问）: {fields}"
    return True, ""


async def _run(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    harness = LiveHarness()
    results: list[dict[str, Any]] = []
    should_clarify: list[dict[str, Any]] = []
    should_answer: list[dict[str, Any]] = []
    field_hits: list[dict[str, bool]] = []
    for case in cases:
        observation = await harness.send(case["message"], thread_id=f"clarify-{case['case_id']}")
        passed, note = _judge(case, observation)
        results.append({"case_id": case["case_id"], "expect": case["expect"],
                        "passed": passed, "note": note})
        if case["expect"] == "clarify":
            asked = bool(observation.get("interrupt"))
            should_clarify.append({"passed": asked})
            expected_fields = set(case.get("expect_fields") or [])
            if expected_fields and asked:
                fields = set(observation["interrupt"].get("fields") or [])
                field_hits.append({"passed": bool(expected_fields & fields)})
        else:
            should_answer.append({"passed": not bool(observation.get("interrupt"))})

    def _rate(rows: list[dict[str, bool]]) -> float:
        return accuracy(sum(1 for item in rows if item["passed"]), len(rows))

    missed = sum(1 for item in should_clarify if not item["passed"])
    extra = {
        "clarify_recall": _rate(should_clarify),
        "clarify_cases": len(should_clarify),
        "hallucination_rate": accuracy(missed, len(should_clarify)),
        "false_clarify_rate": accuracy(
            len(should_answer) - sum(1 for item in should_answer if item["passed"]), len(should_answer)
        ),
        "answer_cases": len(should_answer),
        "clarify_field_coverage": _rate(field_hits),
        "field_coverage_cases": len(field_hits),
    }
    failures = sum(1 for item in results if not item["passed"])
    return results, failures, extra


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="信息缺失追问率评测（live）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    cases = load_clarify_cases()
    try:
        results, failures, extra = run_async(lambda: _run(cases))
    except LiveSkip as exc:
        payload = {"clarification_live": {"status": "SKIP", "reason": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    summary = {
        "status": "OK",
        "total_cases": len(cases),
        **extra,
        "failures": failures_report(results),
    }
    payload = {"clarification_live": {"summary": summary, "results": results}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
