"""runner 共享工具：asyncio 驱动、异步用例评测与结果统计。"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from evals.runners.loader import DatasetError


def run_async(main: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(main())


async def eval_async_cases(
    cases: list[dict[str, Any]],
    judge: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int]:
    """逐条异步评测。judge 返回 {passed, note}，异常按单条失败处理不中断整体。"""
    results: list[dict[str, Any]] = []
    failures = 0
    for case in cases:
        try:
            outcome = await judge(case)
            passed = bool(outcome.get("passed"))
            note = str(outcome.get("note", ""))
        except DatasetError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整场评测
            passed, note = False, f"{type(exc).__name__}: {exc}"
        results.append({"case_id": case.get("case_id", "?"), "passed": passed, "note": note})
        if not passed:
            failures += 1
    return results, failures


def accuracy(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


def failures_report(results: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"case_id": item["case_id"], "note": item["note"]}
        for item in results
        if not item["passed"]
    ][:limit]
