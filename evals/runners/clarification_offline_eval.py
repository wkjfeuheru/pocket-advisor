"""③ 信息缺失追问率评测（离线确定性部分）。

离线可测的确定性守卫链：
- ``offline_missing_params``：``screen_candidates`` 缺 strategy/horizon 必须抛
  ``missing_strategy_or_horizon``（最后一道防线，模型忘了追问也会被拦下）。
- ``clarify_contract_pass_rate``：``request_stock_clarification`` 的澄清载荷契约——
  kind=clarification、agent_version 版本戳、字段白名单校验（非法字段必须拒绝）。

live 部分（clarify_recall / false_clarify_rate / clarify_field_coverage）见
``clarification_live.py``。
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from app.agents.stock.contracts import StockRequest
from app.agents.stock.screening import screen_candidates
from app.crud.stock_feature_crud import MemoryStockFeatureStore

_UNIVERSE = [
    {"thscode": "000001.SZ", "name": "平安银行", "industry": "银行", "trade_status": "交易",
     "listed_days": 1000, "avg_turnover_20": 100_000_000, "net_assets": 1,
     "earnings_quality": 0.8, "cashflow_quality": 0.9, "valuation": 8, "stability": 0.9},
    {"thscode": "600000.SH", "name": "浦发银行", "industry": "银行", "trade_status": "交易",
     "listed_days": 1000, "avg_turnover_20": 100_000_000, "net_assets": 1,
     "earnings_quality": 0.7, "cashflow_quality": 0.7, "valuation": 12, "stability": 0.7},
]


def _screen_without_params(strategy: str | None, horizon: str | None) -> str:
    request = StockRequest(
        scenario="stock_screening",
        strategy=strategy,  # type: ignore[arg-type]
        horizon=horizon,  # type: ignore[arg-type]
    )
    try:
        screen_candidates(request, rows=list(_UNIVERSE))
    except ValueError as exc:
        return str(exc)
    return ""


def _check_clarify_contract() -> tuple[bool, str]:
    """直接调用澄清工具（打断替换为捕获），校验载荷契约。"""
    from app.agents.stock import tools as stock_tools
    from app.agents.stock.evidence_store import MemoryAgentEvidenceStore

    captured: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"strategy": "value_quality"}

    original = stock_tools.interrupt
    stock_tools.interrupt = fake_interrupt
    try:
        tool_map = {
            item.name: item
            for item in stock_tools.build_stock_local_tools(store=MemoryAgentEvidenceStore())
        }
        answer = tool_map["request_stock_clarification"].invoke({
            "reason": "missing_strategy",
            "question": "请选择策略",
            "fields": ["strategy"],
            "options": [{"field": "strategy", "id": "value_quality", "label": "价值质量"}],
        })
        if captured.get("kind") != "clarification":
            return False, f"kind={captured.get('kind')}"
        if not captured.get("agent_version"):
            return False, "缺少 agent_version 版本戳"
        if captured.get("fields") != ["strategy"]:
            return False, f"fields={captured.get('fields')}"
        if answer != {"strategy": "value_quality"}:
            return False, f"resume 回传丢失: {answer}"

        # 非白名单字段必须拒绝
        try:
            tool_map["request_stock_clarification"].invoke({
                "reason": "bad", "question": "x", "fields": ["credit_card_number"],
            })
        except ValueError:
            pass
        else:
            return False, "非法澄清字段未被拒绝"
        return True, ""
    finally:
        stock_tools.interrupt = original


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="澄清确定性守卫评测（离线）")
    parser.add_argument("--json", default="", help="结果另存路径")
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []

    # 1) screen_candidates 是缺参的最终防线
    for label, strategy, horizon in [
        ("both_missing", None, None),
        ("strategy_missing", None, "medium"),
        ("horizon_missing", "value_quality", None),
    ]:
        code = _screen_without_params(strategy, horizon)
        checks.append({
            "case_id": f"guard-{label}",
            "passed": code == "missing_strategy_or_horizon",
            "note": "" if code == "missing_strategy_or_horizon" else f"got={code or 'no-error'}",
        })

    # 2) 参数齐全时正常出结果（误拦截对照）
    request = StockRequest(scenario="stock_screening", strategy="value_quality", horizon="medium")
    try:
        result = screen_candidates(
            request, rows=list(_UNIVERSE),
            profile={"risk_level": "R3"},
            snapshot_id="snap", as_of="2026-09-28",
        )
        passed = bool(result.get("candidates"))
        note = "" if passed else "齐全参数被误拦截"
    except ValueError as exc:
        passed, note = False, f"齐全参数报错: {exc}"
    checks.append({"case_id": "guard-complete-params", "passed": passed, "note": note})

    # 3) 澄清载荷契约
    contract_ok, contract_note = _check_clarify_contract()
    checks.append({"case_id": "clarify-contract", "passed": contract_ok, "note": contract_note})

    total = len(checks)
    failures = sum(1 for item in checks if not item["passed"])
    summary = {
        "total_cases": total,
        "offline_missing_params": round(
            sum(1 for item in checks if item["case_id"].startswith("guard-") and item["passed"]) / 4, 4
        ),
        "clarify_contract_pass_rate": 1.0 if contract_ok else 0.0,
        "all_pass_rate": round((total - failures) / total, 4),
        "failures": [item for item in checks if not item["passed"]],
    }
    payload = {"clarification_offline": {"summary": summary, "results": checks}}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)
    return payload


if __name__ == "__main__":
    main()
