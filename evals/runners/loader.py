"""评测数据集加载与用例校验。

数据集是 ``evals/datasets/*.json``，每份文件形如::

    {"cases": [ ... ]}

用例字段缺失或取值非法时直接抛错，避免静默跑出失真的指标。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATASET_DIR = Path(__file__).resolve().parent.parent / "datasets"

_AGENTS = {"stock_agent", "product_agent", "portfolio_agent", "chat_agent"}
_SCENARIOS = {"market_overview", "equity_research", "stock_screening"}
_STRATEGIES = {"value_quality", "growth_quality", "trend_momentum"}
_HORIZONS = {"short", "medium", "long"}
_CLARIFY_FIELDS = {
    "symbols",
    "strategy",
    "horizon",
    "industries",
    "index_scope",
    "candidate_count",
    "filter_by_profile",
}


class DatasetError(RuntimeError):
    """数据集结构或用例字段非法。"""


def _load(name: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / f"{name}.json"
    if not path.exists():
        raise DatasetError(f"数据集不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"数据集 JSON 非法: {path}: {exc}") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise DatasetError(f"数据集缺少非空 cases 数组: {path}")
    return cases


def _require(case: dict[str, Any], key: str, *, dataset: str) -> Any:
    if key not in case or case[key] in (None, ""):
        raise DatasetError(f"{dataset} 用例 {case.get('case_id', '?')} 缺少字段 {key}")
    return case[key]


def load_intent_cases() -> list[dict[str, Any]]:
    cases = _load("intent_routing")
    for case in cases:
        dataset = "intent_routing"
        _require(case, "case_id", dataset=dataset)
        _require(case, "message", dataset=dataset)
        tag = case.get("tag", "clean")
        if tag not in {"clean", "chit_chat", "noise", "trap"}:
            raise DatasetError(f"{dataset}/{case['case_id']} 非法 tag: {tag}")
        if tag in {"clean", "chit_chat"}:
            agents = case.get("expect_agents")
            if not isinstance(agents, list) or not agents:
                raise DatasetError(f"{dataset}/{case['case_id']} clean 用例必须有 expect_agents")
            bad = [item for item in agents if item not in _AGENTS]
            if bad:
                raise DatasetError(f"{dataset}/{case['case_id']} 未知 agent: {bad}")
        elif tag == "noise":
            if not isinstance(case.get("expect_agents"), list):
                raise DatasetError(f"{dataset}/{case['case_id']} noise 用例必须有 expect_agents（可为空数组）")
    return cases


def load_multi_turn_cases() -> list[dict[str, Any]]:
    cases = _load("multi_turn")
    for case in cases:
        dataset = "multi_turn"
        _require(case, "case_id", dataset=dataset)
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            raise DatasetError(f"{dataset}/{case.get('case_id', '?')} messages 必须是非空数组")
        if len(messages) < 2 and case.get("expect_rewrite", True):
            raise DatasetError(
                f"{dataset}/{case.get('case_id', '?')} expect_rewrite=true 需要 >=2 条消息（单轮无上文可改写）"
            )
        _require(case, "expect_contains", dataset=dataset)
        for key in ("expect_contains", "forbidden_anaphora"):
            if not isinstance(case.get(key), list):
                raise DatasetError(f"{dataset}/{case['case_id']} {key} 必须是字符串数组")
        if not isinstance(case.get("expect_rewrite", True), bool):
            raise DatasetError(f"{dataset}/{case['case_id']} expect_rewrite 必须是布尔")
    return cases


def load_clarify_cases() -> list[dict[str, Any]]:
    cases = _load("clarification")
    for case in cases:
        dataset = "clarification"
        _require(case, "case_id", dataset=dataset)
        _require(case, "message", dataset=dataset)
        expect = _require(case, "expect", dataset=dataset)
        if expect not in {"clarify", "answer"}:
            raise DatasetError(f"{dataset}/{case['case_id']} expect 只能是 clarify 或 answer")
        fields = case.get("expect_fields")
        if fields is not None:
            if not isinstance(fields, list):
                raise DatasetError(f"{dataset}/{case['case_id']} expect_fields 必须是数组")
            bad = [item for item in fields if item not in _CLARIFY_FIELDS]
            if bad:
                raise DatasetError(f"{dataset}/{case['case_id']} 非法澄清字段: {bad}")
    return cases


def load_profile_gate_cases() -> list[dict[str, Any]]:
    from app.agents.graphs.state import PROFILE_FIELDS

    cases = _load("profile_gate")
    for case in cases:
        dataset = "profile_gate"
        _require(case, "case_id", dataset=dataset)
        profile = case.get("profile_snapshot")
        if not isinstance(profile, dict):
            raise DatasetError(f"{dataset}/{case['case_id']} profile_snapshot 必须是对象")
        expect = _require(case, "expect_missing", dataset=dataset)
        if not isinstance(expect, list):
            raise DatasetError(f"{dataset}/{case['case_id']} expect_missing 必须是数组")
        bad = [item for item in expect if item not in PROFILE_FIELDS and item != "budget_amount"]
        if bad:
            raise DatasetError(f"{dataset}/{case['case_id']} 非法画像字段: {bad}")
    return cases


def load_scenario_cases() -> list[dict[str, Any]]:
    cases = _load("stock_scenarios")
    for case in cases:
        dataset = "stock_scenarios"
        _require(case, "case_id", dataset=dataset)
        _require(case, "message", dataset=dataset)
        scenario = _require(case, "expect_scenario", dataset=dataset)
        if scenario not in _SCENARIOS:
            raise DatasetError(f"{dataset}/{case['case_id']} 非法场景: {scenario}")
        strategy = case.get("expect_strategy")
        if strategy is not None and strategy not in _STRATEGIES:
            raise DatasetError(f"{dataset}/{case['case_id']} 非法策略: {strategy}")
        horizon = case.get("expect_horizon")
        if horizon is not None and horizon not in _HORIZONS:
            raise DatasetError(f"{dataset}/{case['case_id']} 非法期限: {horizon}")
    return cases
