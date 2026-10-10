"""⑨ 并发扇出基准（纯离线、零模型调用）。

指标：
- ``two_domain_speedup`` / ``three_domain_speedup`` / ``four_domain_speedup``：
  N 意图 Send 并行 vs 串行执行的延迟比（sleep fakes，每分支固定延迟）。
- ``sequential_ms`` / ``parallel_ms``（4 意图）：绝对延迟参考。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

from evals.runners._fakes import ScriptedModel, fake_specialist, full_specialists
from app.agents.graphs.state import Intent, IntentResult
from langgraph.checkpoint.memory import MemorySaver

_BRANCH_DELAY = 0.1


def _sleep_specialists(names: list[str]) -> dict:
    return {name: fake_specialist(name, delay=_BRANCH_DELAY) for name in names}


def _slow_specialists(names: list[str]) -> dict:
    """把未参与计时的 agent 换成零延迟替身，只测目标分支。"""
    from evals.runners._fakes import full_specialists

    base = full_specialists()
    for name in names:
        base[name] = fake_specialist(name, delay=_BRANCH_DELAY)
    return base


def _model_for(agents: list[str]) -> ScriptedModel:
    return ScriptedModel(
        IntentResult(intents=[
            Intent(agent=name, task=f"任务{name}", kind="general") for name in agents
        ]),
        ["汇总完成。\n推荐代码: 无"],
    )


def _serial_latency(count: int) -> float:
    """串行基线：依次 await N 个 sleep 分支。"""
    async def run() -> None:
        for _ in range(count):
            await asyncio.sleep(_BRANCH_DELAY)

    start = time.perf_counter()
    asyncio.run(run())
    return (time.perf_counter() - start) * 1000


def _parallel_latency(count: int, repeats: int = 5) -> tuple[float, float]:
    """主图 Send 并行延迟：1 次预热（含编译）+ repeats 次串行采样取中位数。"""
    agents = ["stock_agent", "product_agent", "portfolio_agent", "chat_agent"][:count]
    samples: list[float] = []

    async def run_once(index: int) -> None:
        model = _model_for(agents)
        from app.agents.graphs.task_agent import build_advisor_graph

        graph = build_advisor_graph(
            model=model,
            specialists=_slow_specialists(agents),
            checkpointer=MemorySaver(),
            risk_of=lambda _code: None,
        )
        config = {"configurable": {"thread_id": f"fanout-{count}-{index}"}}
        start = time.perf_counter()
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "基准请求"}], "profile_snapshot": {"risk_level": "R3"}},
            config,
            durability="sync",
        )
        elapsed = (time.perf_counter() - start) * 1000
        if index >= 0:  # index 0 为预热样本
            samples.append(elapsed)

    async def run() -> None:
        await run_once(-1)  # 预热：编译图、初始化事件循环
        for index in range(repeats):
            await run_once(index)

    asyncio.run(run())
    return statistics.median(samples), samples


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="并发扇出基准（离线）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    summary: dict[str, Any] = {}
    for count in (2, 3, 4):
        serial = _serial_latency(count)
        parallel, samples = _parallel_latency(count)
        summary[f"{count}_domain_speedup"] = round(serial / parallel, 3) if parallel else 0.0
        summary[f"{count}_domain_serial_ms"] = round(serial, 1)
        summary[f"{count}_domain_parallel_ms"] = round(parallel, 1)
    summary["four_domain_speedup"] = summary["4_domain_speedup"]

    payload = {"fanout": {"summary": summary}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
