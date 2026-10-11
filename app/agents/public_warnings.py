"""公开提示只表达用户可理解的限制，诊断代码保留在内部结果和日志。"""
from __future__ import annotations
import re

_MESSAGES = {
    "missing_market_aggregate": "部分市场数据暂不可用，当前仅能分析已核验的指数行情。",
    "market_evidence_incomplete": "部分市场数据暂不可用，当前仅能分析已核验的指数行情。",
    "missing_structured_response": "本次研究结果未能完整生成，可稍后重试。",
    "missing_stock_evidence": "行情证据暂不可用，暂不能判断当前市场走势。",
    "missing_equity_evidence": "个股证据暂不可用，暂不能完成个股分析。",
    "unsupported_timeframe": "当前数据未覆盖所要求的完整时间区间，不能据此判断该区间的走势。",
    "unsupported_limitations": "部分附加条件尚未得到数据验证。",
    "tool_repeat_limit": "部分查询未能完成，已保留取得的有效数据。",
    "upstream_data_unavailable": "数据源暂未返回有效数据。",
    "stock_snapshot_stale": "选股数据尚未更新，暂不能生成当前候选排名。",
    "missing_screening_result": "当前选股数据不足，暂不能生成候选排名。",
    "invalid_fact_reference": "部分数值未通过证据核验，已停止展示。",
    "unsupported_narrative_numbers_removed": "部分分析数值无法核验，已保留可验证内容。",
}
_INTERNAL = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def public_warnings(items, *, limit: int = 4) -> list[str]:
    result = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        text = next((message for code, message in _MESSAGES.items() if code in text), text)
        if _INTERNAL.search(text):
            text = "部分数据暂不可用，相关结论未纳入本次回答。"
        if text not in result:
            result.append(text)
    return result[:limit]
