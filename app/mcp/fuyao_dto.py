"""把 Fuyao 原始响应收成内部只读字段。未知字段一律丢掉。"""

from __future__ import annotations

import json
import re
import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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
        "turnover": ("turnover", "amount"),
        "volume": ("volume", "vol"),
        "as_of": ("as_of", "update_time", "timestamp"),
    },
    "kline": {
        "thscode": ("thscode", "ts_code", "symbol"),
        "close": ("close", "close_price"),
        "high": ("high", "high_price"),
        "low": ("low", "low_price"),
        "open": ("open", "open_price"),
        "volume": ("volume", "vol"),
        "turnover": ("turnover",),
        "as_of": ("as_of", "trade_date", "date", "date_ms", "timestamp", "update_time"),
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
        "pe": ("pe", "pe_ttm", "pe_mrq"),
        "pb": ("pb", "pb_mrq"),
        "ps": ("ps_ttm",),
        "industry_percentile": ("industry_percentile",),
        "as_of": ("as_of", "update_time", "timestamp"),
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
        "trade_date": ("trade_date", "calendar_date", "date", "date_ms"),
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

# Fuyao 财务指标以 ``abilities[].indicators[].{index_id,value}`` 返回，
# 这里按下游分析工具消费的语义字段做一次显式映射。
_ABILITY_INDICATORS: dict[str, tuple[str, ...]] = {
    "roe": ("index_weighted_avg_roe", "index_deduct_weighted_avg_roe"),
    "gross_margin": ("sale_gross_margin",),
    "net_margin": ("sale_net_interest_ratio",),
    "leverage": ("assets_debt_ratio",),
    "revenue_trend": ("calculate_operating_income_yoy_growth_ratio",),
    "profit_trend": ("calculate_parent_holder_net_profit_yoy_growth_ratio",),
    "operating_profit_trend": ("calculate_operating_profit_yoy_growth_ratio",),
}


def _as_iso(value: Any, *, date_only: bool = False) -> Any:
    """区分业务日期与带时刻的快照截止时间，不丢失盘中精度。"""
    text = str(value if value is not None else "").strip()
    quarter = re.fullmatch(r"(20\d{2})-([1-4])", text)
    if quarter:
        year, month = int(quarter.group(1)), int(quarter.group(2)) * 3
        return f"{year:04}-{month:02}-{calendar.monthrange(year, month)[1]:02}"
    if text.isdigit() and len(text) == 8:
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return value
    if text.isdigit() and 10 <= len(text) <= 13:
        try:
            stamp = datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), tz=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
            return stamp.date().isoformat() if date_only else stamp.isoformat()
        except (OverflowError, OSError, ValueError):
            return value
    return value


def _normalize_financials(payload: dict[str, Any]) -> dict[str, Any]:
    """展平 Fuyao 财务指标的 abilities/indicators 结构，只保留可核验字段。"""
    data = payload.get("data")
    data = data if isinstance(data, dict) else payload
    indicators: dict[str, Any] = {}
    for ability in data.get("abilities") or []:
        if not isinstance(ability, dict):
            continue
        for item in ability.get("indicators") or []:
            if isinstance(item, dict) and item.get("index_id"):
                indicators[str(item["index_id"])] = item.get("value")
    mapped: dict[str, Any] = {"source": str(payload.get("source") or "fuyao")}
    for target, names in _ABILITY_INDICATORS.items():
        value = next((indicators[name] for name in names if indicators.get(name) is not None), None)
        if value is not None:
            mapped[target] = value
    mapped["as_of"] = _as_iso(data.get("report") or data.get("timestamp") or "")
    return mapped


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
    if isinstance(payload, dict) and "message" in payload and "code" in payload and str(payload["code"]) not in {"0", "200", ""}:
        return {"failed": True, "warning": "upstream_data_unavailable", "source": "fuyao", "as_of": ""}
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
    if operation == "financials":
        inner = payload.get("data")
        source = inner if isinstance(inner, dict) else payload
        if isinstance(source.get("abilities"), list):
            return _normalize_financials(payload)
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("item")
        if isinstance(rows, list):
            timestamp = str(data.get("timestamp") or payload.get("as_of") or "")
            return normalize_payload(operation, [
                {**({"thscode": data["thscode"]} if data.get("thscode") else {}), **row, "timestamp": row.get("timestamp") or timestamp}
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
            mapped[target] = _as_iso(value, date_only=target == "trade_date" or operation == "kline") if target in {"as_of", "trade_date"} else value
    mapped.setdefault("as_of", "")
    return mapped


__all__ = ["normalize_payload"]
