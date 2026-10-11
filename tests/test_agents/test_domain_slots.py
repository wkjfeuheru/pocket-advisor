"""领域槽位边界：补丁、独立上下文、确定性降级与时间规范化。"""

import asyncio
from datetime import datetime, timezone
import subprocess
import sys
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.slots import (
    SLOT_MODELS, SlotExtraction, SlotPatch, extract_slots, merge_slot_patch,
    normalize_timeframe, structured_task, validate_slots,
)


class UnsupportedModel:
    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError


class ExtractionModel:
    def __init__(self, values, continuation=False, clear_fields=None, append_values=None):
        self.values = values
        self.continuation = continuation
        self.clear_fields = clear_fields or []
        self.append_values = append_values or {}

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Bound:
            async def ainvoke(self, messages):
                return schema(
                    continuation=parent.continuation,
                    patch=SlotPatch(set_values=parent.values, clear_fields=parent.clear_fields,
                                    append_values=parent.append_values),
                )
        return Bound()


def extract(agent, query, existing=None, model=None, pending_fields=None):
    return asyncio.run(extract_slots(model or UnsupportedModel(), agent, query, [], existing or {}, pending_fields=pending_fields))


def test_patch_replace_clear_append_and_no_mutation():
    existing = {"scenario": "equity_research", "symbols": ["贵州茅台"], "metrics": ["估值"]}
    merged = merge_slot_patch(existing, SlotPatch(
        set_values={"symbols": ["宁德时代"]}, clear_fields=["metrics"],
        append_values={"symbols": ["宁德时代", "比亚迪"]},
    ), SLOT_MODELS["stock_agent"])
    assert merged["symbols"] == ["宁德时代", "比亚迪"]
    assert merged.get("metrics", []) == []
    assert existing["symbols"] == ["贵州茅台"]


@pytest.mark.parametrize("field", ["risk_level", "investment_horizon", "investment_goal", "liquidity_need", "user_id"])
def test_patch_cannot_change_protected_profile_fields(field):
    with pytest.raises((ValueError, ValidationError)):
        merge_slot_patch({}, SlotPatch(set_values={field: "R5"}), SLOT_MODELS["stock_agent"])


def test_typed_slots_forbid_unknown_and_invalid_enums():
    for values in ({"horizon": "一年"}, {"surprise": True}):
        with pytest.raises(ValidationError):
            SLOT_MODELS["stock_agent"].model_validate(values)


@pytest.mark.parametrize("query,start,end", [
    ("1y", "2025-10-10", "2026-10-10"),
    ("近一年", "2025-10-10", "2026-10-10"),
    ("3m", "2026-07-10", "2026-10-10"),
    ("2024Q3", "2024-07-01", "2024-09-30"),
    ("2024年第三季度", "2024-07-01", "2024-09-30"),
])
def test_timeframe_uses_calendar_bounds(query, start, end):
    timeframe = normalize_timeframe(query, now=datetime(2026, 10, 10, 1, tzinfo=timezone.utc))
    assert timeframe.start == start
    assert timeframe.end == end
    assert timeframe.timezone == "Asia/Shanghai"


def test_timeframe_anchors_utc_boundary_to_shanghai():
    timeframe = normalize_timeframe("近一年", now=datetime(2026, 10, 9, 18, tzinfo=timezone.utc))
    assert timeframe.end == "2026-10-10"


def test_market_overview_does_not_require_stock_symbol():
    result = extract("stock_agent", "今天大盘怎么样")
    assert result["slots"]["scenario"] == "market_overview"
    assert result["missing"] == []


def test_screening_requires_strategy_and_horizon_without_inventing_defaults():
    result = extract("stock_agent", "帮我选几只股票")
    assert result["slots"]["scenario"] == "stock_screening"
    assert set(result["missing"]) == {"strategy", "horizon"}
    assert result["question"]


def test_product_compare_requires_two_objects():
    result = extract("product_agent", "比较这只基金", model=ExtractionModel({"operation": "compare", "symbols": ["基金甲"]}))
    assert "symbols" in result["missing"]


def test_new_stock_clears_previous_timeframe_but_explicit_same_analysis_keeps_it():
    prior = {"scenario": "equity_research", "symbols": ["贵州茅台"], "timeframe": {"start": "2025-01-01", "end": "2025-12-31"}}
    changed = extract("stock_agent", "分析宁德时代", prior, ExtractionModel({"scenario": "equity_research", "symbols": ["宁德时代"]}))
    assert not changed["slots"].get("timeframe")
    followed = extract("stock_agent", "按同样的分析看宁德时代", prior,
                       ExtractionModel({"scenario": "equity_research", "symbols": ["宁德时代"]}, continuation=True))
    assert followed["slots"]["timeframe"]["start"] == "2025-01-01"


def test_obvious_time_followup_inherits_symbol_and_replaces_timeframe():
    result = extract("stock_agent", "改看近一年", {"scenario": "equity_research", "symbols": ["贵州茅台"]})
    assert result["continuation"] is True
    assert result["slots"]["symbols"] == ["贵州茅台"]
    assert result["slots"]["timeframe"]["timezone"] == "Asia/Shanghai"


def test_scenario_switch_clears_symbol_and_technical_metrics_even_if_model_claims_followup():
    result = extract("stock_agent", "再看大盘", {"scenario": "equity_research", "symbols": ["贵州茅台"], "metrics": ["RSI"]},
                     ExtractionModel({"scenario": "market_overview"}, continuation=True))
    assert result["slots"].get("symbols", []) == []
    assert result["slots"].get("metrics", []) == []


def test_horizon_is_distinct_from_historical_timeframe():
    result = extract("stock_agent", "按价值策略长期选股，看近一年")
    assert result["slots"]["horizon"] == "long"
    assert result["slots"]["strategy"] == "value_quality"
    assert result["slots"]["timeframe"]["start"]
    assert result["missing"] == []


def test_portfolio_unspecified_adjustment_asks_instead_of_generating_allocation():
    result = extract("portfolio_agent", "帮我看看怎么调整持仓")
    assert "operation" in result["missing"]
    assert result["options"]


def test_model_failure_is_reported_and_falls_back_conservatively():
    class Broken:
        def with_structured_output(self, schema, **kwargs):
            class Bound:
                async def ainvoke(self, messages):
                    raise RuntimeError("provider unavailable")
            return Bound()
    result = extract("stock_agent", "分析它", model=Broken())
    assert "symbols" in result["missing"]
    assert result["warnings"]


def test_parallel_domain_extraction_never_mutates_other_context():
    existing = {"scenario": "equity_research", "symbols": ["贵州茅台"]}
    async def run():
        return await asyncio.gather(
            extract_slots(UnsupportedModel(), "stock_agent", "改看近一年", [], existing),
            extract_slots(ExtractionModel({"operation": "compare", "symbols": ["甲", "乙"]}), "product_agent", "比较甲乙", [], {}),
        )
    stock, product = asyncio.run(run())
    assert stock["slots"]["symbols"] == ["贵州茅台"]
    assert product["slots"]["symbols"] == ["甲", "乙"]
    assert existing == {"scenario": "equity_research", "symbols": ["贵州茅台"]}


def test_structured_task_contains_normalized_constraints_and_original_request():
    task = structured_task("stock_agent", {"scenario": "equity_research", "symbols": ["贵州茅台"], "metrics": ["估值"]}, "它估值高吗")
    assert "贵州茅台" in task and "估值" in task and "它估值高吗" in task
    assert validate_slots("stock_agent", {"scenario": "equity_research", "symbols": ["贵州茅台"]}) == []


def test_domain_contracts_can_be_imported_before_shared_helpers():
    result = subprocess.run([sys.executable, "-c", "from app.agents.stock.slots import StockSlots; from app.agents.product.slots import ProductSlots; from app.agents.portfolio.slots import PortfolioSlots"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_product_time_followup_keeps_compare_operation():
    result = extract("product_agent", "改看近一年", {"operation": "compare", "symbols": ["甲", "乙"]})
    assert result["slots"]["operation"] == "compare"
    assert result["slots"]["symbols"] == ["甲", "乙"]


def test_portfolio_time_followup_keeps_diagnosis_operation():
    result = extract("portfolio_agent", "改看近一年", {"operation": "diagnose"})
    assert result["slots"]["operation"] == "diagnose"


def test_timeframe_string_from_model_is_normalized_before_schema_validation():
    result = extract("stock_agent", "分析贵州茅台近一年", model=ExtractionModel({"scenario": "equity_research", "symbols": ["贵州茅台"], "timeframe": "1y"}))
    assert result["slots"]["timeframe"]["timezone"] == "Asia/Shanghai"
    assert result["missing"] == []


def test_clear_and_append_protected_fields_are_also_rejected():
    for patch in (SlotPatch(clear_fields=["risk_level"]), SlotPatch(append_values={"investment_goal": ["增长"]})):
        with pytest.raises(ValueError):
            merge_slot_patch({}, patch, SLOT_MODELS["stock_agent"])


def test_unresolved_pronoun_does_not_borrow_unrelated_old_object():
    result = extract("stock_agent", "分析它", {"scenario": "equity_research", "symbols": ["贵州茅台"]},
                     ExtractionModel({"scenario": "equity_research"}, continuation=False))
    assert "symbols" in result["missing"]


def test_model_can_recognize_short_followup_without_pronoun():
    result = extract("stock_agent", "短期", {"scenario": "stock_screening", "strategy": "value_quality"},
                     ExtractionModel({"horizon": "short"}, continuation=True))
    assert result["slots"]["scenario"] == "stock_screening"
    assert result["missing"] == []


def test_answer_to_missing_symbol_keeps_existing_research_scenario():
    result = extract("stock_agent", "贵州茅台", {"scenario": "equity_research", "metrics": ["估值"]},
                     ExtractionModel({"symbols": ["贵州茅台"]}, continuation=True), pending_fields=["symbols"])
    assert result["slots"]["scenario"] == "equity_research"
    assert result["slots"]["metrics"] == ["估值"]
    assert result["missing"] == []


def test_screening_followup_accepts_strategy_and_horizon_without_reselecting_scenario():
    result = extract("stock_agent", "价值质量，短期", {"scenario": "stock_screening"},
                     ExtractionModel({"strategy": "value_quality", "horizon": "short"}, continuation=True), pending_fields=["strategy", "horizon"])
    assert result["slots"]["scenario"] == "stock_screening"
    assert result["missing"] == []


def test_merge_rejects_invalid_time_order_and_non_array_append():
    with pytest.raises(ValueError):
        merge_slot_patch({}, SlotPatch(set_values={"timeframe": {"start": "2026-10-10", "end": "2025-10-10"}}), SLOT_MODELS["stock_agent"])
    with pytest.raises(ValueError):
        merge_slot_patch({"horizon": "short"}, SlotPatch(append_values={"horizon": ["long"]}), SLOT_MODELS["stock_agent"])


def test_disconnected_query_does_not_carry_previous_screening_strategy():
    result = extract("stock_agent", "分析贵州茅台", {"scenario": "stock_screening", "strategy": "value_quality", "horizon": "short"})
    assert result["slots"]["scenario"] == "equity_research"
    assert "strategy" not in result["slots"] and "horizon" not in result["slots"]


def test_pending_symbol_accepts_plain_answer_in_scripted_model_fallback():
    result = extract("stock_agent", "贵州茅台", {"scenario": "equity_research", "metrics": ["估值"]}, pending_fields=["symbols"])
    assert result["slots"]["symbols"] == ["贵州茅台"]
    assert result["slots"]["metrics"] == ["估值"]
    assert result["continuation"]


def test_pending_strategy_ignores_unrelated_bare_answer():
    result = extract("stock_agent", "随便", {"scenario": "stock_screening"}, pending_fields=["strategy", "horizon"])
    assert result["slots"] == {"scenario": "stock_screening"}
    assert set(result["missing"]) == {"strategy", "horizon"}


def test_new_scenario_during_pending_does_not_complete_previous_task():
    result = extract("stock_agent", "看看今天大盘", {"scenario": "stock_screening"}, pending_fields=["strategy", "horizon"])
    assert result["slots"]["scenario"] == "market_overview"
    assert not result["continuation"]


def test_explicit_append_followup_adds_raw_stock_name_without_replacing_previous():
    result = extract("stock_agent", "再加比亚迪", {"scenario": "equity_research", "symbols": ["贵州茅台"]})
    assert result["slots"]["symbols"] == ["贵州茅台", "比亚迪"]


def test_explicit_timeframe_clear_keeps_other_research_conditions():
    result = extract("stock_agent", "去掉时间范围", {"scenario": "equity_research", "symbols": ["贵州茅台"], "timeframe": {"start": "2025-01-01", "end": "2025-12-31"}})
    assert "timeframe" not in result["slots"]
    assert result["slots"]["symbols"] == ["贵州茅台"]


def test_exact_user_time_followup_keeps_previous_stock():
    result = extract("stock_agent", "看近一年", {"scenario": "equity_research", "symbols": ["茅台"]})
    assert result["slots"]["symbols"] == ["茅台"]
    assert result["slots"]["timeframe"]["timezone"] == "Asia/Shanghai"


def test_roe_followup_is_metric_and_never_replaces_stock_symbol():
    result = extract("stock_agent", "再看 ROE", {"scenario": "equity_research", "symbols": ["茅台"], "metrics": ["估值"]})
    assert result["slots"]["symbols"] == ["茅台"]
    assert "ROE" in result["slots"]["metrics"]
    assert "估值" in result["slots"]["metrics"]


def test_same_domain_parallel_tasks_use_own_hint_with_raw_source_authorization():
    async def run():
        source = "分析茅台近一年，再分析宁德时代近三个月"
        return await asyncio.gather(
            extract_slots(UnsupportedModel(), "stock_agent", "分析茅台近一年", [], {}, source_query=source),
            extract_slots(UnsupportedModel(), "stock_agent", "分析宁德时代近三个月", [], {}, source_query=source),
        )
    first, second = asyncio.run(run())
    assert first["slots"]["symbols"] == ["茅台"]
    assert second["slots"]["symbols"] == ["宁德时代"]
    assert first["slots"]["timeframe"]["start"] != second["slots"]["timeframe"]["start"]


def test_classifier_invented_entity_is_rejected_even_if_extractor_repeats_it():
    result = asyncio.run(extract_slots(ExtractionModel({"scenario": "equity_research", "symbols": ["宁德时代"]}),
        "stock_agent", "分析宁德时代", [], {}, source_query="分析茅台"))
    assert "symbols" in result["missing"]
    assert "宁德时代" not in result["slots"].get("symbols", [])


def test_hint_cannot_invent_historical_range_or_metrics():
    result = asyncio.run(extract_slots(ExtractionModel({"scenario": "equity_research", "symbols": ["茅台"], "timeframe": "1y", "metrics": ["ROE"]}),
        "stock_agent", "分析茅台近一年ROE", [], {}, source_query="分析茅台"))
    assert "timeframe" not in result["slots"]
    assert "ROE" not in result["slots"].get("metrics", [])


def test_dependency_fact_subject_can_supply_stock_without_user_repeating_it():
    result = asyncio.run(extract_slots(ExtractionModel({"scenario": "equity_research", "symbols": ["600519.SH"]}),
        "stock_agent", "分析持仓里的股票", [], {}, source_query="先查持仓再分析其中股票",
        dependency_results=[{"status": "ok", "facts": [{"subject": "600519.SH", "evidence_id": "holding", "metric": "shares", "value": 100}], "summary": "持仓已查"}]))
    assert result["slots"]["symbols"] == ["600519.SH"]
    assert result["missing"] == []


def test_dependency_data_codes_support_deterministic_scripted_fallback():
    result = asyncio.run(extract_slots(UnsupportedModel(), "stock_agent", "分析其中股票", [], {},
        source_query="先查持仓再分析其中股票", dependency_results=[{"status": "ok", "data": {"holdings": [{"code": "600519.SH"}]}}]))
    assert result["slots"]["symbols"] == ["600519.SH"]
    assert result["missing"] == []


def test_dependency_summary_alone_cannot_authorize_unverified_entity():
    result = asyncio.run(extract_slots(ExtractionModel({"scenario": "equity_research", "symbols": ["600519.SH"]}),
        "stock_agent", "分析其中股票", [], {}, source_query="先查持仓再分析其中股票",
        dependency_results=[{"status": "ok", "summary": "贵州茅台600519.SH"}]))
    assert "symbols" in result["missing"]


def test_parallel_task_time_authorization_accepts_second_range_without_comma():
    result = asyncio.run(extract_slots(UnsupportedModel(), "stock_agent", "分析宁德时代3m", [], {},
        source_query="分析茅台1y和宁德时代3m"))
    assert result["slots"]["timeframe"]["start"]
    assert result["slots"]["symbols"] == ["宁德时代"]


def test_dependency_with_more_than_three_stocks_asks_instead_of_arbitrarily_selecting():
    result = asyncio.run(extract_slots(UnsupportedModel(), "stock_agent", "分析其中股票", [], {},
        source_query="先查持仓再分析其中股票", dependency_results=[{"data": {"codes": ["000001.SZ", "000002.SZ", "600000.SH", "600519.SH"]}}]))
    assert "symbols" in result["missing"]


def test_tool_message_history_never_enters_slot_extraction_context():
    class HistorySensitiveModel:
        def with_structured_output(self, schema, **kwargs):
            class Bound:
                async def ainvoke(self, messages):
                    if "secret-tool-payload" in json.dumps(messages):
                        raise RuntimeError("raw tool transcript reached extraction")
                    return schema(patch=SlotPatch(set_values={"scenario": "equity_research", "symbols": ["茅台"]}))
            return Bound()
    result = asyncio.run(extract_slots(HistorySensitiveModel(), "stock_agent", "分析茅台", [
        SimpleNamespace(type="tool", content="secret-tool-payload"),
        {"role": "tool", "content": "secret-tool-payload"},
    ], {}))
    assert result["warnings"] == []
    assert result["slots"]["symbols"] == ["茅台"]


def test_pending_product_names_keep_comparison_instead_of_defaulting_to_list_query():
    result = extract("product_agent", "华夏基金、易方达基金", {"operation": "compare", "product_type": "fund"}, pending_fields=["symbols"])
    assert result["slots"]["symbols"] == ["华夏基金", "易方达基金"]
    assert result["slots"]["operation"] == "compare"
    assert result["slots"]["product_type"] == "fund"
    assert result["continuation"]
    assert result["missing"] == []


def test_pending_bare_name_from_task_hint_still_requires_real_user_source():
    result = asyncio.run(extract_slots(UnsupportedModel(), "stock_agent", "宁德时代", [],
        {"scenario": "equity_research"}, pending_fields=["symbols"], source_query="茅台"))
    assert "宁德时代" not in result["slots"].get("symbols", [])
    assert result["missing"]
