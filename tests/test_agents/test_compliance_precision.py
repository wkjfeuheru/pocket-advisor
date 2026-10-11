"""合规校验区分展示参数与业务断言，保持摘要和引用语义。"""
import pytest

from app.agents.compliance_guard import StreamingGuard, render_fact, sanitize_stream
from app.agents.stock.assembler import _guard_summary


FACTS = {"quote:0": {"subject": "300750.SZ", "metric": "last", "value": 297.75, "unit": "元", "as_of": "2026-10-11"},
         "quote:1": {"subject": "300750.SZ", "metric": "roe", "value": 12.5, "unit": "%", "as_of": "2026-09-30"}}


def test_periods_and_list_numbers_are_not_business_claims():
    text = "1. 技术面：五日均线走平，近一季表现需观察。\n2. 基本面：近一年财报可作参考。"
    cleaned, warnings = sanitize_stream(text)
    assert cleaned == text
    assert not warnings


def test_unreferenced_business_numbers_are_removed_as_claim_not_broken_sentence():
    text, warnings = sanitize_stream("走势仍需观察。收益达到99%，可以关注。后续留意财报。")
    assert text == "走势仍需观察。后续留意财报。"
    assert warnings


def test_summary_keeps_numbered_layers_and_idiom():
    summary = "1. 基本面：现金流稳健。\n2. 技术面：四舍五入后观察趋势。"
    assert _guard_summary(summary, [{"payload": {"last": 297.75}}]) == (summary, False)


def test_bad_summary_claim_only_removes_unsupported_sentence():
    summary, changed = _guard_summary("基本面：现金流稳健。现价999元。\n技术面：五日均线走平。", [{"payload": {"last": 297.75}}])
    assert changed
    assert "现金流稳健" in summary and "五日均线走平" in summary
    assert "999" not in summary


def test_inline_fact_is_compact_and_metric_mismatch_is_blocked():
    assert render_fact(FACTS["quote:0"]) == "297.75元"
    text, warnings = sanitize_stream("现价 {{quote:0|last}}，ROE {{quote:1|roe}}。", FACTS)
    assert text == "现价 297.75元，ROE 12.5%。"
    assert not warnings
    bad, warnings = sanitize_stream("ROE {{quote:0|last}}。后续关注财报。", FACTS)
    assert "297.75" not in bad and "后续关注财报" in bad
    assert "stream_fact_metric_mismatch" in warnings


@pytest.mark.parametrize("text", ["价格99日元。", "99.元收益。", "营收为一百。"])
def test_presentation_exceptions_do_not_hide_business_claims(text):
    cleaned, warnings = sanitize_stream(text)
    assert cleaned == ""
    assert warnings


def test_repeated_reference_unit_is_not_duplicated():
    text, warnings = sanitize_stream("现价 {{quote:0|last}}元。", FACTS)
    assert text == "现价 297.75元。"
    assert not warnings
    rejected, warnings = sanitize_stream("现价 {{quote:0|last}}%。", FACTS)
    assert rejected == ""
    assert "stream_fact_unit_mismatch" in warnings


def test_fact_subject_mismatch_is_blocked_and_verified_index_name_is_allowed():
    facts = {"sh:0": {"subject": "000001.SH", "subject_name": "上证指数", "metric": "last", "value": 3813.79, "unit": "点"},
        "hs:0": {"subject": "000300.SH", "subject_name": "沪深300", "metric": "last", "value": 4317.25, "unit": "点"}}
    wrong, warnings = sanitize_stream("上证指数点位 {{hs:0|last}}。", facts)
    assert wrong == "" and "stream_fact_subject_mismatch" in warnings
    correct, warnings = sanitize_stream("沪深300点位 {{hs:0|last}}。", facts)
    assert correct == "沪深300点位 4317.25点。" and not warnings


@pytest.mark.parametrize("text", [
    "1. 五日均线走平。\n2. 一季内保持观察。",
    "参考现价 {{quote:0|last}}。" + "后续关注现金流表现。" * 8,
    "ROE {{quote:0|last}}。" + "关注财报。" * 8,
    "当前收益百分之九十九。继续观察。",
])
def test_long_stream_remains_independent_of_chunk_boundaries(text):
    expected = sanitize_stream(text, FACTS)
    for index in range(len(text) + 1):
        guard = StreamingGuard(FACTS)
        output = guard.push(text[:index]) + guard.push(text[index:]) + guard.finalize()
        assert (output, guard.warnings()) == expected


def test_real_node_accepts_period_outline_without_retry_or_fact_list_fallback():
    import asyncio
    from app.agents.graphs.task_agent import build_advisor_graph
    from app.agents.graphs.state import ChatResult, Intent
    from tests.test_agents.test_supervisor_v2 import Model
    text = "1. 技术面：五日均线走平，近一季仍需观察。\n2. 基本面：四舍五入不影响定性结论。"
    model = Model([Intent(agent="chat_agent", task="解释分析方法")], draft=text)
    async def worker(task, state): return ChatResult(summary="分析方法说明")
    values = asyncio.run(build_advisor_graph(model=model, specialists={"chat_agent": worker}).ainvoke(
        {"messages": [{"role": "user", "content": "解释分析方法"}]}))
    assert values["final"].answer.startswith(text)
    assert len(model.inputs) == 2  # 一次骨架规划、一次成文，不为参数或序号重试。
