"""按完整语句核验流式正文，区分分析参数、展示编号与业务数值。"""
from __future__ import annotations
import re
from collections.abc import Mapping
from typing import Any

PLACEHOLDER = re.compile(r"\{\{([\w:.-]+)(?:\|([\w.-]+))?\}\}")
FORBIDDEN = re.compile(r"保证收益|稳赚|必赚|保收益|承诺收益|建议(?:立即)?(?:买入|卖出|加仓|减仓)|目标价|买卖点")
HOLDBACK = 24  # 兼容常量；安全边界为完整语句，不依赖字符窗口。
_BOUNDARY = re.compile(r"[。！？；\n]|(?<!\d)\.(?=\s)")
_PRESENTATION = re.compile(
    r"(?m)^\s*(?:[-*]\s+)?(?:\d{1,3}[.)、]|[一二三四五六七八九十]+[、）)])(?=\s|[\u4e00-\u9fff])(?!\s*(?:元|美元|日元|万元|亿元|倍|股|%|％))"
    r"|[零〇一二三四五六七八九十百两\d]{1,4}(?:交易日|日(?!元)|天|周|个月|月|季度|季|年)"
    r"|20\d{2}[Qq][1-4]|四舍五入"
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?")
_CHINESE_CLAIM = re.compile(
    r"百分之[零〇一二三四五六七八九十百千万亿两点]{1,16}"
    r"|[零〇一二三四五六七八九十百千万亿两]{1,16}(?:元|美元|万元|亿元|个百分点|倍|股|笔|成)"
    r"|(?:收益|净值|价格|仓位|权重|费率|回报|资产|余额).{0,6}[零〇一二三四五六七八九十百千万亿两]"
    r"|(?:营收|利润|市盈率|市净率|ROE|PE|PB)\s*(?:为|是|达到|约为|：|:)?\s*[零〇一二三四五六七八九十百千万亿两]"
)
_UNIT_SUFFIX = re.compile(r"\s*(个百分点|万元|亿元|美元|日元|元|倍|股|笔|成|%|％)")
_ALIASES = {
    "last": ("现价", "最新价", "当前价格", "last"), "close": ("收盘价", "close"),
    "change_pct": ("涨跌幅", "涨幅", "change_pct"), "roe": ("净资产收益率", "ROE", "roe"),
    "pe": ("市盈率", "PE", "pe"), "pb": ("市净率", "PB", "pb"),
    "rsi": ("RSI", "rsi"), "nav": ("净值", "nav"),
    "turnover": ("成交额", "成交金额", "turnover"),
    "report_period": ("报告期", "数据日期", "截止日期", "report_period"),
}


def numeric_claims(text: str, *, labels=()) -> list[str]:
    """列出未引用的业务数字；分析参数与展示编号不属于数值断言。"""
    plain = PLACEHOLDER.sub(lambda m: " " * len(m.group()), text)
    for label in sorted({label for label in labels if re.search(r"[\u4e00-\u9fff]", label)}, key=len, reverse=True):
        if label:
            plain = plain.replace(label, " " * len(label))
    plain = _PRESENTATION.sub(lambda m: " " * len(m.group()), plain)
    return [m.group() for m in _NUMBER.finditer(plain)] + [m.group() for m in _CHINESE_CLAIM.finditer(plain)]


def render_fact(fact: Mapping[str, Any], *, detailed: bool = False) -> str:
    """正文只展开数值和单位；独立事实摘要才展示标的、指标和时间。"""
    value = str(fact.get("value", "")) + str(fact.get("unit") or "")
    if not detailed:
        return value
    metric = str(fact.get("metric") or "")
    label = _ALIASES.get(metric, (metric,))[0]
    return f"{fact.get('subject_name') or fact.get('subject', '')} {label}：{value}（截至 {fact.get('as_of') or '时间未知'}）"


def _context_metric(prefix: str) -> str | None:
    nearest, last_position = None, -1
    for metric, aliases in _ALIASES.items():
        for label in aliases:
            pattern = re.compile(r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", re.IGNORECASE)
            for match in pattern.finditer(prefix[-60:]):
                if match.start() > last_position:
                    nearest, last_position = metric, match.start()
    return nearest


def _canonical_metric(metric: str) -> str:
    lowered = metric.lower()
    return next((key for key, names in _ALIASES.items() if lowered in {name.lower() for name in names}), lowered)


class StreamingGuard:
    """缓存到句界，整句核验后发布；任意切分不会破坏数字或占位符。"""
    def __init__(self, facts: Mapping[str, Mapping[str, Any]] | None = None):
        self._facts = dict(facts or {})
        self._buffer = ""
        self._reasons: list[str] = []

    def _note(self, reason):
        self._reasons.append(reason)

    def push(self, delta: str) -> str:
        self._buffer += delta or ""
        cut = 0
        protected = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(self._buffer)]
        unclosed = self._buffer.rfind("{{")
        if unclosed > self._buffer.rfind("}}"):
            protected.append((unclosed, len(self._buffer)))
        for match in _BOUNDARY.finditer(self._buffer):
            if not any(start <= match.start() < end for start, end in protected):
                cut = match.end()
        head, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._clean(head)

    def finalize(self) -> str:
        text, self._buffer = self._buffer, ""
        start = text.rfind("{{")
        if start > text.rfind("}}"):
            self._note("stream_unclosed_placeholder")
            text = text[:start]
        return self._clean(text)

    def warnings(self) -> list[str]:
        return list(dict.fromkeys(self._reasons))

    def _clean(self, text: str) -> str:
        pieces, start = [], 0
        protected = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
        for match in _BOUNDARY.finditer(text):
            if any(left <= match.start() < right for left, right in protected):
                continue
            pieces.append(self._sentence(text[start:match.end()]))
            start = match.end()
        pieces.append(self._sentence(text[start:]))
        return "".join(pieces)

    def _sentence(self, text: str) -> str:
        if re.search(r"\b(?:missing|invalid|unsupported|security|tool|market|evidence|stock|stream)_[a-z_]+\b", text):
            self._note("stream_blocked_internal_code")
            return ""
        if FORBIDDEN.search(text):
            self._note("stream_blocked_forbidden")
            return ""
        labels = {str(fact.get("subject_name") or ""): str(fact.get("subject") or "") for fact in self._facts.values()
                  if fact.get("subject_name") and fact.get("subject_name") != fact.get("subject")}
        claims = numeric_claims(text, labels=labels)
        if claims:
            self._note("stream_blocked_number" if any(_NUMBER.fullmatch(c) for c in claims) else "stream_blocked_numeral")
            return ""
        for match in PLACEHOLDER.finditer(text):
            fact = self._facts.get(match.group(1))
            if fact is None:
                self._note("stream_unknown_placeholder")
                return ""
            metric = _canonical_metric(str(fact.get("metric") or ""))
            declared = _canonical_metric(match.group(2) or "")
            inferred = _context_metric(text[:match.start()])
            named = [(text[:match.start()].rfind(label), subject) for label, subject in labels.items()
                     if label in text[:match.start()]]
            if named and max(named)[1] != str(fact.get("subject") or ""):
                self._note("stream_fact_subject_mismatch")
                return ""
            if (declared and declared != metric) or (inferred and inferred != metric):
                self._note("stream_fact_metric_mismatch")
                return ""
        pieces, start = [], 0
        for match in PLACEHOLDER.finditer(text):
            fact = self._facts.get(match.group(1))
            pieces.append(text[start:match.start()])
            start = match.end()
            if fact is None:
                continue
            suffix = _UNIT_SUFFIX.match(text, start)
            if suffix:
                if suffix.group(1).replace("％", "%") != str(fact.get("unit") or "").replace("％", "%"):
                    self._note("stream_fact_unit_mismatch")
                    return ""
                start = suffix.end()
            pieces.append(render_fact(fact))
        pieces.append(text[start:])
        return "".join(pieces)


def sanitize_stream(text: str, facts=None) -> tuple[str, list[str]]:
    guard = StreamingGuard(facts)
    return guard.push(text) + guard.finalize(), guard.warnings()


__all__ = ["HOLDBACK", "PLACEHOLDER", "StreamingGuard", "render_fact", "sanitize_stream", "numeric_claims", "FORBIDDEN"]
