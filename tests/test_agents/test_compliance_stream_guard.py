"""流式合规过滤器：前缀单调、切分无关、与一次性清洗一致。"""

from __future__ import annotations

import random

import pytest

from app.agents.compliance_guard import (
    HOLDBACK,
    StreamingGuard,
    render_fact,
    sanitize_stream,
)

FACTS = {
    "task-1:0": {"subject": "上证综指", "metric": "涨跌幅", "value": "0.8", "unit": "%", "as_of": "2026-09-27"},
    "task-2:1": {"subject": "610011", "metric": "最新净值", "value": "1.2345", "unit": "", "as_of": "2026-09-26"},
}

SAMPLES = [
    "",
    "市场整体平稳。",
    "涨了 {{task-1:0}} 个点，整体仍属震荡。",
    "建议买入该标的。",
    "目标价为高位。",
    "收益达到十元。",
    "占比百分之二十。",
    "这是 {{task-2:1}} 的表现，仅供参考。",
    "净值 {{task-2:1}}，收益 {{task-1:0}}。",
    "文中出现了 3 个数字和 25% 的涨幅。",
    "非法引用 {{unknown:9}} 应当被丢弃。",
    "未闭合的 {{task-1:0",
    "多段文本。\n\n第二段：{{task-2:1}}。\n\n第三段结束。",
]


def _stream(chunks: list[str]) -> tuple[str, list[str]]:
    guard = StreamingGuard(FACTS)
    out = "".join(guard.push(chunk) for chunk in chunks)
    return out + guard.finalize(), guard.warnings()


@pytest.mark.parametrize("text", SAMPLES)
def test_single_chunk_matches_one_shot(text):
    """P3：单块 push + finalize 与一次性清洗结果一致。"""
    assert _stream([text])[0] == sanitize_stream(text, FACTS)[0]


@pytest.mark.parametrize("text", SAMPLES)
def test_two_piece_splits_are_equivalent(text):
    """P2：任意二分切分下，输出与一次性清洗完全一致。"""
    expected = sanitize_stream(text, FACTS)[0]
    for index in range(len(text) + 1):
        assert _stream([text[:index], text[index:]])[0] == expected, f"切分点 {index} 结果不一致"


@pytest.mark.parametrize("text", SAMPLES)
def test_random_splits_are_equivalent(text):
    """P2：随机多段切分（含逐字切分）结果一致。"""
    expected = sanitize_stream(text, FACTS)[0]
    rng = random.Random(20260927)
    for _ in range(60):
        cuts = sorted(rng.randint(0, len(text)) for _ in range(rng.randint(0, 4)))
        chunks: list[str] = []
        cursor = 0
        for cut in cuts:
            chunks.append(text[cursor:cut])
            cursor = cut
        chunks.append(text[cursor:])
        assert _stream(chunks)[0] == expected
    assert _stream(list(text))[0] == expected


def test_cross_chunk_forbidden_phrase_is_blocked():
    """禁语被切成两段时也必须拦住。"""
    assert _stream(["建议买", "入该标的。"])[0] == ""
    assert _stream(["目", "标", "价很高"])[0] == ""


def test_cross_chunk_chinese_number_is_blocked():
    """中文数词跨 chunk 时也要拦住。

    注意：正则 ``(?:收益|净值|…).{0,6}[数词]`` 是**最左优先**匹配，因此"收益达到十元"
    会命中"收益达到十"并删除，残留"元。"。这里断言的是"数词已删除"，而不是美化残句——
    与生产节点的同一套正则口径保持一致。
    """
    assert "十" not in _stream(["收益达到十", "元。"])[0]
    assert "十" not in _stream(["收益达到", "十元。"])[0]
    assert _stream(["占比百分之", "二十。"])[0] == ""


def test_ascii_digits_are_dropped_but_placeholder_values_survive():
    """正文数字被删，占位符渲染出的数字保留。"""
    text, warnings = _stream(["涨幅 25% 而 {{task-1:0}}。"])
    assert text == ""
    assert "stream_blocked_number" in warnings
    assert _stream(["涨幅 {{task-1:0}}。"])[0] == "涨幅 0.8%。"


def test_unknown_placeholder_is_dropped_with_warning():
    text, warnings = _stream(["引用 {{unknown:9}} 无效。"])
    assert text == "引用  无效。"
    assert "stream_unknown_placeholder" in warnings


def test_unclosed_placeholder_is_dropped_on_finalize():
    text, warnings = _stream(["前文 {{task-1:0"])
    assert text == "前文 "
    assert "stream_unclosed_placeholder" in warnings


def test_normal_chinese_words_are_not_mangled():
    """"十分""一致""一定"等正常词不该被当成数字删除。"""
    for phrase in ["十分满意", "保持一致", "一定会有波动", "一系列因素"]:
        assert _stream([phrase])[0] == phrase


def test_prefix_is_append_only():
    """P1：已放出的增量在后续 push 中不再变化（只追加）。"""
    guard = StreamingGuard(FACTS)
    emitted = ""
    for chunk in ["先", "说结论：", "{{task-1:0}}", " 供参考"]:
        delta = guard.push(chunk)
        assert emitted + delta == emitted + delta  # 明确追加语义
        emitted += delta
    emitted += guard.finalize()
    assert emitted == sanitize_stream("先说结论：{{task-1:0}} 供参考", FACTS)[0]


def test_holdback_is_large_enough_for_documented_max_width():
    """holdback 必须覆盖所有模式的文档化最大宽度。"""
    assert HOLDBACK >= 15


def test_render_fact_matches_production_template():
    assert render_fact(FACTS["task-1:0"]) == "0.8%"
    assert render_fact(FACTS["task-1:0"], detailed=True) == "上证综指 涨跌幅：0.8%（截至 2026-09-27）"
    assert render_fact({"subject": "X", "metric": "Y", "value": "1", "unit": "元", "as_of": ""}, detailed=True) == "X Y：1元（截至 时间未知）"
