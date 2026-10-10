"""评测总入口：跑全部 runner，汇总到 ``.cache/evals/results/summary.json``。

用法::

    python -m evals.runners.run_all            # 离线（快、零成本、CI 安全）
    python -m evals.runners.run_all --live     # 追加 live 指标（需要模型密钥）
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / ".cache" / "evals" / "results"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _safe(section: str, runner_main) -> dict[str, Any]:
    """单个 runner 崩溃不拖垮整场：记录 ERROR 段后继续。"""
    try:
        return runner_main([])
    except Exception as exc:  # noqa: BLE001 - 汇总层隔离单点失败
        return {section: {"status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}}


def _run_offline() -> dict[str, Any]:
    from evals.runners import (
        clarification_offline_eval,
        fanout_benchmark,
        profile_gate_eval,
    )

    summary: dict[str, Any] = {}
    summary.update(_safe("profile_gate", profile_gate_eval.main))
    summary.update(_safe("clarification_offline", clarification_offline_eval.main))
    summary.update(_safe("fanout", fanout_benchmark.main))
    return summary


def _run_live() -> dict[str, Any]:
    from evals.runners import (
        clarification_live_eval,
        intent_routing_eval,
        multi_turn_eval,
        stock_scenario_eval,
    )

    summary: dict[str, Any] = {}
    summary.update(_safe("intent_routing", intent_routing_eval.main))
    summary.update(_safe("multi_turn", multi_turn_eval.main))
    summary.update(_safe("clarification_live", clarification_live_eval.main))
    summary.update(_safe("stock_scenarios", stock_scenario_eval.main))
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="FinanceAgent 评测总入口")
    parser.add_argument("--live", action="store_true", help="追加 live 指标（需要 ADVISOR_MODEL_API_KEY）")
    parser.add_argument("--json", default="", help="结果另存路径（默认 .cache/evals/results/summary.json）")
    args = parser.parse_args()

    summary: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "live" if args.live else "offline",
            "python": platform.python_version(),
        }
    }
    summary.update(_run_offline())
    if args.live:
        summary.update(_run_live())

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(args.json) if args.json else RESULTS_DIR / "summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=== summary written to {target} ===")
    return summary


if __name__ == "__main__":
    main()
