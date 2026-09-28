"""把 Fuyao 原始响应收成内部只读字段。未知字段一律丢掉。"""

from __future__ import annotations

import json
from typing import Any

_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "search_security": {
        "thscode": ("thscode", "ts_code", "code"),
        "name": ("name", "sec_name"),
        "kind": ("kind", "type"),
        "as_of": ("as_of", "update_time"),
    },
    "list_indices": {
        "thscode": ("thscode", "ts_code", "code"),
        "name": ("name",),
        "kind": ("kind",),
        "as_of": ("as_of", "update_time"),
    },
    "list_industries": {
        "thscode": ("thscode", "code"),
        "name": ("name",),
        "kind": ("kind",),
        "as_of": ("as_of", "update_time"),
    },
    "quote": {
        "thscode": ("thscode", "ts_code", "code"),
        "name": ("name", "ticker"),
        "last": ("last", "price", "close", "last_price"),
        "change_pct": ("change_pct", "pct_chg", "price_change_ratio_pct"),
        "as_of": ("as_of", "update_time", "timestamp"),
    },
    "kline": {
        "close": ("close",),
        "high": ("high",),
        "low": ("low",),
        "volume": ("volume", "vol"),
        "as_of": ("as_of", "trade_date", "update_time"),
    },
    "financials": {
        "revenue_trend": ("revenue_trend",),
        "profit_trend": ("profit_trend",),
        "roe": ("roe",),
        "gross_margin": ("gross_margin",),
        "cashflow_quality": ("cashflow_quality",),
        "leverage": ("leverage",),
        "pe": ("pe",),
        "pb": ("pb",),
        "industry_percentile": ("industry_percentile",),
        "as_of": ("as_of", "end_date", "update_time"),
    },
    "valuation": {
        "pe": ("pe",),
        "pb": ("pb",),
        "industry_percentile": ("industry_percentile",),
        "as_of": ("as_of", "update_time"),
    },
    "market_breadth": {
        "up": ("up",),
        "down": ("down",),
        "unchanged": ("unchanged",),
        "turnover": ("turnover",),
        "limit_up": ("limit_up",),
        "limit_down": ("limit_down",),
        "continue_board": ("continue_board",),
        "broken_board": ("broken_board",),
        "as_of": ("as_of", "update_time"),
    },
    "sector_quote": {
        "industry": ("industry",),
        "concept": ("concept",),
        "as_of": ("as_of", "update_time"),
    },
    "trade_calendar": {
        "trade_date": ("trade_date",),
        "session": ("session", "is_open"),
        "as_of": ("as_of", "update_time"),
    },
    "company_profile": {
        "thscode": ("thscode", "ts_code", "code"),
        "name": ("name",),
        "as_of": ("as_of", "update_time"),
    },
}


# 上游常用「列表装在信封里」的返回形状。目录类操作必须保留这些行，
# 否则实体解析拿不到任何股票/指数/行业。
_LIST_ENVELOPE_KEYS = ("items", "data", "list", "records", "rows")


def _pick(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in payload and payload[alias] is not None:
            return payload[alias]
    return None


def _members(payload: dict[str, Any]) -> dict[str, Any]:
    members = payload.get("members")
    if members is None:
        members = payload.get("con_code") or []
    if isinstance(members, str):
        members = [members]
    return {
        "thscode": str(payload.get("thscode") or payload.get("index_code") or ""),
        "members": [str(item) for item in members],
        "source": "fuyao",
        "as_of": str(payload.get("as_of") or payload.get("update_time") or ""),
    }


def normalize_payload(operation: str, payload: Any) -> Any:
    if payload is None:
        return None
    if operation == "index_members" and isinstance(payload, dict):
        return _members(payload)
    if isinstance(payload, list):
        text_payloads = []
        for item in payload:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                text_payloads.append(json.loads(text))
            except json.JSONDecodeError:
                continue
        if text_payloads:
            normalized = [normalize_payload(operation, item) for item in text_payloads]
            flattened = []
            for item in normalized:
                if isinstance(item, list):
                    flattened.extend(item)
                elif item is not None:
                    flattened.append(item)
            return flattened
        return [
            normalize_payload(operation, item)
            for item in payload
            if isinstance(item, dict)
        ]
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("item")
        if isinstance(rows, list):
            timestamp = str(data.get("timestamp") or payload.get("as_of") or "")
            return normalize_payload(operation, [
                {**row, "timestamp": row.get("timestamp") or timestamp}
                for row in rows
                if isinstance(row, dict)
            ])
    for key in _LIST_ENVELOPE_KEYS:
        nested = payload.get(key)
        if isinstance(nested, list):
            return normalize_payload(operation, nested)
    spec = _SPECS.get(operation)
    if spec is None:
        return {"source": "fuyao", "as_of": str(payload.get("as_of") or "")}
    mapped = {"source": str(payload.get("source") or "fuyao")}
    for target, aliases in spec.items():
        value = _pick(payload, aliases)
        if value is not None:
            mapped[target] = value
    mapped.setdefault("as_of", "")
    return mapped


__all__ = ["normalize_payload"]
