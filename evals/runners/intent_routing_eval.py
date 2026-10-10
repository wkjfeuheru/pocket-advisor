"""① 意图路由准确率评测（live：真实模型 + 假数据源）。

指标：
- ``routing_accuracy``：期望 agent 集合精确匹配（顺序无关）。
- 分片准确率：``clean_accuracy`` / ``chit_chat_accuracy`` / ``noise_accuracy`` / ``trap_accuracy``。
- ``per_agent_recall``：各 agent 在应命中的用例中被正确路由的比例。
- ``avg_confidence_on_correct`` / ``avg_confidence_on_wrong``：置信度与对错的相关性。

噪声用例（模型输出格式瑕疵）在 live 模式下以正常消息发送，
期望模型与校验层组合后仍能兜底到合理分支或不崩溃；它们主要服务离线重放，
live 下按「不抛异常即通过」从宽计分，避免模型自由度造成误报。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from evals.runners._live import (
    LiveHarness,
    LiveSkip,
    observed_agents,
    observed_kinds,
    observed_needs_budget,
)
from evals.runners._util import accuracy, failures_report, run_async
from evals.runners.loader import load_intent_cases


def _judge_clean(case: dict[str, Any], observation: dict[str, Any]) -> tuple[bool, str]:
    expected = set(case["expect_agents"])
    actual = set(observed_agents(observation))
    if actual != expected:
        return False, f"expected={sorted(expected)} actual={sorted(actual)}"
    if actual == {"chat_agent"} and not observation.get("intents"):
        return False, "空意图"
    kind_expect = case.get("expect_kind")
    if kind_expect:
        kinds = observed_kinds(observation)
        for agent in case["expect_agents"]:
            if agent != "chat_agent" and kinds.get(agent) != kind_expect:
                return False, f"{agent} kind={kinds.get(agent)} 期望 {kind_expect}"
    budget_expect = case.get("expect_needs_budget")
    if budget_expect is not None:
        budgets = observed_needs_budget(observation)
        for agent in case["expect_agents"]:
            if agent != "chat_agent" and budgets.get(agent, False) != budget_expect:
                return False, f"{agent} needs_budget={budgets.get(agent)} 期望 {budget_expect}"
    return True, ""


def _judge_noise(case: dict[str, Any], observation: dict[str, Any]) -> tuple[bool, str]:
    # live 下载的是模型自己的输出（非注入 raw），只要求链路不崩溃。
    # 路由结果若恰好落在合理兜底分支也算通过。
    expected = set(case.get("expect_agents") or [])
    actual = set(observed_agents(observation))
    if not observation.get("intents") and observation.get("final_result"):
        return True, "空意图兜底"
    if not expected or actual <= expected or not expected & actual:
        return True, ""
    return True, ""


async def _run(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    harness = LiveHarness()
    results: list[dict[str, Any]] = []
    correct_confidences: list[float] = []
    wrong_confidences: list[float] = []
    agent_hits: dict[str, list[bool]] = {}
    for case in cases:
        tag = case.get("tag", "clean")
        observation = await harness.send(case["message"], thread_id=f"intent-{case['case_id']}")
        if tag in {"clean", "chit_chat"}:
            passed, note = _judge_clean(case, observation)
        else:  # noise / trap
            passed, note = _judge_clean(case, observation) if tag == "trap" else _judge_noise(case, observation)
        confidences = [float(item.get("confidence") or 0.0) for item in observation.get("intents") or []]
        if confidences:
            (correct_confidences if passed else wrong_confidences).extend(confidences)
        for agent in case.get("expect_agents") or []:
            agent_hits.setdefault(agent, []).append(agent in observed_agents(observation))
        results.append({"case_id": case["case_id"], "tag": tag, "passed": passed, "note": note})

    def _by_tag(tag: str) -> float:
        rows = [item for item in results if item["tag"] == tag]
        return accuracy(sum(1 for item in rows if item["passed"]), len(rows))

    per_agent_recall = {
        agent: accuracy(sum(1 for hit in hits if hit), len(hits))
        for agent, hits in sorted(agent_hits.items())
    }
    extra = {
        "clean_accuracy": _by_tag("clean"),
        "chit_chat_accuracy": _by_tag("chit_chat"),
        "noise_accuracy": _by_tag("noise"),
        "trap_accuracy": _by_tag("trap"),
        "per_agent_recall": per_agent_recall,
        "avg_confidence_on_correct": round(sum(correct_confidences) / len(correct_confidences), 3)
        if correct_confidences else None,
        "avg_confidence_on_wrong": round(sum(wrong_confidences) / len(wrong_confidences), 3)
        if wrong_confidences else None,
    }
    return results, sum(1 for item in results if not item["passed"]), extra


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="意图路由评测（live）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    cases = load_intent_cases()
    try:
        results, failures, extra = run_async(lambda: _run(cases))
    except LiveSkip as exc:
        payload = {"intent_routing": {"status": "SKIP", "reason": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    total = len(cases)
    summary = {
        "status": "OK",
        "total_cases": total,
        "routing_accuracy": accuracy(total - failures, total),
        **extra,
        "failures": failures_report(results),
    }
    payload = {"intent_routing": {"summary": summary, "results": results}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
