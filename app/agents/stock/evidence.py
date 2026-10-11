"""按版本化白名单生成公开结果。无来源的数字字段删除。

投影机制在 ``app.agents.shared.projection``；这里只保留股票领域的 schema 与数字字段集合。
"""

from __future__ import annotations

from typing import Any

from app.agents.shared.projection import (
    list_schema as _list,
    object_schema as _object,
    project,
)
from app.agents.stock.contracts import StockRequest

PUBLIC_SCHEMA = "stock-public-v1"

_NUMERIC = {
    "change_pct",
    "last",
    "score",
    "pe",
    "pb",
    "roe",
    "gross_margin",
    "leverage",
    "turnover",
    "up",
    "down",
    "unchanged",
    "limit_up",
    "limit_down",
    "continue_board",
    "broken_board",
    "industry_percentile",
    "quality",
    "valuation_inv",
    "stability",
    "growth",
    "earnings_quality",
    "valuation_fit",
    "momentum",
    "trend",
    "liquidity",
    "volatility_inv",
    "rsi",
    "dif",
    "dea",
    "hist",
    "confidence",
}

_COMPONENTS = {
    "quality": None,
    "valuation_inv": None,
    "stability": None,
    "growth": None,
    "earnings_quality": None,
    "valuation_fit": None,
    "momentum": None,
    "trend": None,
    "liquidity": None,
    "volatility_inv": None,
}


_INDEX = _object({
    "thscode": None, "name": None, "last": None, "change_pct": None, "turnover": None, "source": None, "as_of": None,
})
_HISTORY_INDEX = _object({"thscode": None, "name": None, "first_close": None, "last_close": None,
    "change_pct": None, "start_date": None, "as_of": None, "source": None, "coverage": None})
_BREADTH = _object({
    "up": None, "down": None, "unchanged": None, "turnover": None, "limit_up": None,
    "limit_down": None, "continue_board": None, "broken_board": None, "source": None, "as_of": None,
})
_SECTOR_ROW = _object({"name": None, "change_pct": None})
_SECTORS = _object({
    "industry": _list(_SECTOR_ROW),
    "concept": _list(_SECTOR_ROW),
    "source": None,
    "as_of": None,
})
_QUOTE = _object({"last": None, "change_pct": None, "source": None, "as_of": None})
_VALUATION = _object({"pe": None, "pb": None, "industry_percentile": None, "source": None, "as_of": None})
_FUNDAMENTALS = _object({
    "revenue_trend": None, "profit_trend": None, "roe": None, "gross_margin": None,
    "cashflow_quality": None, "leverage": None, "valuation": _VALUATION, "source": None, "as_of": None,
})
_MACD = _object({"dif": None, "dea": None, "hist": None})
_TECHNICALS = _object({"status": None, "macd": _MACD, "rsi": None, "trend": None, "source": None, "as_of": None})
_EVENT = _object({
    "title": None, "source": None, "url": None, "published_at": None,
    "related_symbols": None, "event_tags": None, "confidence": None,
})
_NEWS = _object({"status": None, "events": _list(_EVENT), "source": None, "as_of": None})
_EQUITY = _object({
    "thscode": None, "name": None, "failed": None, "warnings": None, "quote": _QUOTE,
    "fundamentals": _FUNDAMENTALS, "technicals": _TECHNICALS, "news": _NEWS,
    "conflicts": None, "risks": None, "watch": None, "as_of": None, "sources": None,
})
_CANDIDATE = _object({
    "thscode": None, "name": None, "score": None, "components": _object(_COMPONENTS),
    "risks": None, "source": None, "as_of": None, "snapshot_id": None,
})
_SCREENING = _object({
    "failed": None, "candidates": _list(_CANDIDATE), "warnings": None, "sources": None,
    "as_of": None, "live_quotes": None,
})
_MARKET = _object({
    "failed": None, "indices": _list(_INDEX), "breadth": _BREADTH, "sectors": _SECTORS,
    "last_calendar_trade_date": None, "snapshot_time": None,
    "historical_indices": _list(_HISTORY_INDEX),
    "as_of": None, "sources": None, "warnings": None, "sentiment_input": None, "risks": None,
})
_RESULT = _object({
    "scenario": None, "summary": None, "as_of": None, "sources": None, "warnings": None,
    "evidence_quality": None, "failed": None, "market_overview": _MARKET,
    "equity_research": _list(_EQUITY), "stock_screening": _SCREENING,
    "candidate_stocks": _list(_object({"thscode": None, "name": None, "score": None})),
})


def _project(value: Any, spec: Any, *, inherited: bool) -> Any:
    """兼容旧调用点：把领域 schema 交给共享投影机制。"""
    return project(spec, value, numeric_fields=_NUMERIC, inherited=inherited)


def public_stock_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """只保留白名单字段，并给公开结果打上版本。"""
    projected = _project(dict(payload or {}), _RESULT, inherited=False) or {}
    projected["schema_version"] = PUBLIC_SCHEMA
    return projected


def apply_evidence_guard(payload: dict[str, Any], request: StockRequest) -> dict[str, Any]:
    cleaned = public_stock_artifact(payload)
    scenario = request.scenario
    warnings = [str(item) for item in cleaned.get("warnings") or []]
    failed = bool(cleaned.get("failed"))
    if scenario == "market_overview":
        block = cleaned.get("market_overview") or {}
        indices = list(block.get("indices") or [])
        if not indices:
            failed = True
            warnings.append("缺少带来源的指数行情")
        cleaned["market_overview"] = {**block, "indices": indices}
    elif scenario == "equity_research":
        equities = list(cleaned.get("equity_research") or [])
        successful = [item for item in equities if isinstance(item, dict) and not item.get("failed")]
        if not successful:
            failed = True
            warnings.append("没有可展示的个股证据")
        cleaned["equity_research"] = equities
    elif scenario == "stock_screening":
        block = dict(cleaned.get("stock_screening") or {})
        candidates = list(block.get("candidates") or [])[: max(1, min(10, request.candidate_count))]
        if not candidates:
            failed = True
            warnings.append("没有研究候选")
        block["candidates"] = candidates
        cleaned["stock_screening"] = block
        cleaned["candidate_stocks"] = [
            {"thscode": item.get("thscode"), "name": item.get("name") or "", "score": item.get("score")}
            for item in candidates
        ]
    cleaned["failed"] = failed
    cleaned["warnings"] = warnings
    cleaned["evidence_quality"] = "failed" if failed else ("partial" if warnings else "ok")
    return cleaned


__all__ = ["PUBLIC_SCHEMA", "apply_evidence_guard", "public_stock_artifact"]
