"""Stock Agent 的服务端原子工具。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import Any

from langchain_core.tools import tool
from app.agents.evidence import NeedsClarification, current_execution, validate_tool_arguments

from app.agents.stock.contracts import StockRequest
from app.agents.stock.evidence_store import EvidenceContext, compact_evidence_preview
from app.agents.stock.market import build_market_overview, session_from_calendar
from app.agents.stock.screening import screen_candidates
from app.agents.stock.technical import compute_technicals
from app.crud.stock_feature_crud import SnapshotStaleError

STOCK_AGENT_VERSION = "stock-react-v1"
_CLARIFICATION_FIELDS = {
    "symbols",
    "strategy",
    "horizon",
    "industries",
    "index_scope",
    "candidate_count",
    "filter_by_profile",
}

_SAFE_OPTIONS = {
    "strategy": {"value_quality": "价值质量", "growth_quality": "成长质量", "trend_momentum": "趋势动量"},
    "horizon": {"short": "短期", "medium": "中期", "long": "长期"},
    "candidate_count": {str(count): str(count) for count in range(1, 11)},
    "filter_by_profile": {"true": "使用画像过滤", "false": "不使用画像过滤"},
}


def _trusted_clarification_options(execution: Any, options: list[dict], fields: list[str]) -> list[dict]:
    securities = {str(code): str(code) for code in execution.domain_slots.get("symbols") or []}
    indices = {str(code): str(code) for code in execution.domain_slots.get("index_scope") or []}

    def metadata(value, target):
        if isinstance(value, list):
            for item in value:
                metadata(item, target)
        elif isinstance(value, dict):
            code = value.get("thscode") or value.get("ts_code") or value.get("code")
            if code:
                target[str(code)] = str(value.get("name") or code)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    metadata(item, target)

    for row in execution.rows:
        if row.get("evidence_type") == "security_meta":
            metadata(row["payload"], securities)
            if "index" in row.get("tool_name", ""):
                metadata(row["payload"], indices)
    trusted = {**_SAFE_OPTIONS, "symbols": securities, "index_scope": indices}
    result = []
    for option in options:
        field_name = option.get("field") or (fields[0] if len(fields) == 1 else "")
        label = trusted.get(field_name, {}).get(option["id"])
        if label is not None:
            result.append({"id": option["id"], "label": label, "field": field_name})
    return result


@dataclass(frozen=True)
class StockExecution:
    context: EvidenceContext
    profile: dict[str, Any] = field(default_factory=dict)
    #: 本次执行的工具调用计数，用于单工具重复上限（内容可变，字段本身冻结）。
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_results: dict[str, Any] = field(default_factory=dict)
    tool_locks: dict[str, asyncio.Lock] = field(default_factory=dict)


_EXECUTION: ContextVar[StockExecution | None] = ContextVar("stock_execution", default=None)


def bind_stock_execution(context: EvidenceContext, *, profile: dict[str, Any] | None = None) -> Token:
    return _EXECUTION.set(StockExecution(context=context, profile=dict(profile or {})))


def reset_stock_execution(token: Token) -> None:
    _EXECUTION.reset(token)


def optional_stock_execution() -> StockExecution | None:
    """未绑定执行上下文时返回 None，供中间件做无状态兜底。"""
    return _EXECUTION.get()


def current_stock_execution() -> StockExecution:
    value = _EXECUTION.get()
    if value is None:
        raise RuntimeError("stock_execution_not_bound")
    return value


def _save(store: Any, *, name: str, evidence_type: str, payload: Any, source: str = "", as_of: str = "") -> dict:
    context = current_stock_execution().context
    row = store.put(
        context,
        tool_call_id=f"local:{name}",
        tool_name=name,
        evidence_type=evidence_type,
        payload=payload,
        source=source,
        as_of=as_of,
    )
    execution = current_execution()
    if execution is not None:
        execution.rows.append(row)
    return compact_evidence_preview(row["payload"], row["id"])


def _data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _symbol(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("thscode") or payload.get("ts_code") or payload.get("symbol") or "")
    return ""


def build_stock_local_tools(*, store: Any, tushare: Any = None, warehouse: Any = None) -> list[Any]:
    @tool
    def request_stock_clarification(
        reason: str,
        question: str,
        fields: list[str],
        options: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """当标的有歧义，或选股策略/期限缺失时，向用户请求结构化澄清。"""
        cleaned_fields = [str(item) for item in fields if str(item).strip()]
        if not cleaned_fields or any(item not in _CLARIFICATION_FIELDS for item in cleaned_fields):
            raise ValueError("invalid_clarification_fields")
        cleaned_options = []
        for item in options or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            packed = {"id": str(item["id"]), "label": str(item.get("label") or item["id"])}
            if item.get("field"):
                option_field = str(item["field"])
                if option_field not in cleaned_fields:
                    raise ValueError("invalid_clarification_option_field")
                packed["field"] = option_field
            cleaned_options.append(packed)
        payload = {
            "kind": "clarification",
            "agent_version": STOCK_AGENT_VERSION,
            "reason": str(reason),
            "question": str(question),
            "fields": cleaned_fields,
            "options": cleaned_options,
            "request": {},
        }
        execution = current_execution()
        if execution is not None:
            payload["options"] = _trusted_clarification_options(execution, cleaned_options, cleaned_fields)
            payload["question"] = "请补充已确认任务的必要条件，或从可核验选项中选择。"
            payload["reason"] = "missing_" + "_".join(cleaned_fields)
            execution.pending_clarification = payload
        raise NeedsClarification(payload)

    @tool
    def calculate_technical_indicators(kline_evidence_id: str) -> dict[str, Any]:
        """从同一请求的 K 线证据句柄计算技术指标。"""
        source = store.get(kline_evidence_id, current_stock_execution().context)
        payload = source["payload"]
        bars = _data(payload)
        if isinstance(bars, dict):
            bars = bars.get("rows") or bars.get("items") or []
        result = compute_technicals(list(bars or []))
        result.update({
            "thscode": _symbol(payload),
            "source": source.get("source") or "fuyao",
            "as_of": source.get("as_of") or "",
        })
        return _save(store, name="calculate_technical_indicators", evidence_type="technicals",
                     payload=result, source=result["source"], as_of=result["as_of"])

    @tool
    def analyze_stock_fundamentals(
        financial_evidence_ids: list[str],
        valuation_evidence_id: str = "",
    ) -> dict[str, Any]:
        """合并同一请求中的财务和估值证据，不生成没有来源的数字。"""
        ids = [*financial_evidence_ids, *([valuation_evidence_id] if valuation_evidence_id else [])]
        rows = store.list(ids, current_stock_execution().context)
        merged: dict[str, Any] = {}
        symbol = ""
        sources: list[str] = []
        as_of = ""
        for row in rows:
            data = _data(row["payload"])
            if isinstance(data, list):
                data = next((item for item in data if isinstance(item, dict)), None)
            if isinstance(data, dict):
                merged.update(data)
                found_symbol = _symbol(row["payload"]) or _symbol(data)
                if found_symbol and symbol and found_symbol != symbol:
                    raise ValueError("mixed_symbol_evidence")
                symbol = symbol or found_symbol
            sources.append(str(row.get("source") or ""))
            as_of = max(as_of, str(row.get("as_of") or ""))
        result = {
            "thscode": symbol,
            "revenue_trend": merged.get("revenue_trend"),
            "profit_trend": merged.get("profit_trend"),
            "roe": merged.get("roe"),
            "gross_margin": merged.get("gross_margin"),
            "cashflow_quality": merged.get("cashflow_quality"),
            "leverage": merged.get("leverage"),
            "valuation": {
                "pe": merged.get("pe"), "pb": merged.get("pb"),
                "industry_percentile": merged.get("industry_percentile"),
                "source": next((item for item in sources if item), "fuyao"), "as_of": as_of,
            },
            "source": next((item for item in sources if item), "fuyao"),
            "as_of": as_of,
        }
        return _save(store, name="analyze_stock_fundamentals", evidence_type="fundamentals",
                     payload=result, source=result["source"], as_of=as_of)

    @tool
    async def collect_stock_events(thscode: str, days: int = 30) -> dict[str, Any]:
        """收集指定股票的新闻和公告事件，不保存新闻正文。"""
        violation = validate_tool_arguments("collect_stock_events", {"thscode": thscode})
        if violation:
            execution = current_execution()
            if execution is not None:
                execution.warnings.append(violation)
            raise ValueError(violation)
        if tushare is None or not hasattr(tushare, "list_events"):
            payload = {"thscode": thscode, "status": "unavailable", "events": []}
            return await asyncio.to_thread(
                partial(
                    _save,
                    store,
                    name="collect_stock_events",
                    evidence_type="events",
                    payload=payload,
                    source="tushare",
                )
            )
        envelope = await tushare.list_events(thscode, days=max(1, min(int(days), 365)))
        events = list(envelope.get("data") or []) if envelope.get("ok") else []
        payload = {"thscode": thscode, "status": "ok" if envelope.get("ok") else "unavailable",
                   "events": events, "source": "tushare", "as_of": str(envelope.get("as_of") or "")}
        return await asyncio.to_thread(
            partial(
                _save,
                store,
                name="collect_stock_events",
                evidence_type="events",
                payload=payload,
                source="tushare",
                as_of=payload["as_of"],
            )
        )

    @tool
    def aggregate_market_snapshot(evidence_ids: list[str]) -> dict[str, Any]:
        """把指数、市场广度、板块和交易日历证据组成市场概览。"""
        rows = store.list(evidence_ids, current_stock_execution().context)
        indices: list[dict] = []
        breadth = None
        sectors = None
        calendar = None
        for row in rows:
            payload = _data(row["payload"])
            if isinstance(payload, list):
                candidates = payload
            elif isinstance(payload, dict):
                candidates = payload.get("items") or payload.get("rows") or []
                if {"up", "down"} & set(payload):
                    breadth = dict(payload)
                if "industry" in payload or "concept" in payload:
                    sectors = dict(payload)
                if "session" in payload or "is_open" in payload:
                    calendar = dict(payload)
                if _symbol(payload) and ("last" in payload or "price" in payload):
                    candidates = [payload]
            else:
                candidates = []
            for item in candidates if isinstance(candidates, list) else []:
                if not isinstance(item, dict) or not _symbol(item):
                    continue
                if row.get("evidence_type") != "quote" or "index" not in str(row.get("tool_name") or "").lower():
                    continue
                indices.append({
                    "thscode": _symbol(item), "name": item.get("name") or _symbol(item),
                    "last": item.get("last", item.get("price")), "change_pct": item.get("change_pct"),
                    "turnover": item.get("turnover"),
                    "source": row.get("source") or "fuyao", "as_of": item.get("as_of") or row.get("as_of") or "",
                })
        from datetime import datetime, timezone
        result = build_market_overview(
            indices=indices or None, breadth=breadth, sectors=sectors,
            now=datetime.now(timezone.utc), session=session_from_calendar(calendar),
            upstream_error="" if indices and breadth is not None and sectors is not None else "market_evidence_incomplete",
        )
        return _save(store, name="aggregate_market_snapshot", evidence_type="market_overview",
                     payload=result, source="fuyao", as_of=str(result.get("as_of") or ""))

    @tool
    def score_stock_candidates(
        strategy: str,
        horizon: str,
        industries: list[str] | None = None,
        index_scope: list[str] | None = None,
        candidate_count: int = 5,
        filter_by_profile: bool = False,
        membership_evidence_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """使用已发布快照确定性评分并排序研究候选，模型不得改序。"""
        execution = current_execution()
        if execution is not None:
            actual = {"strategy": strategy, "horizon": horizon, "industries": list(industries or []),
                      "index_scope": list(index_scope or []), "candidate_count": candidate_count,
                      "filter_by_profile": filter_by_profile}
            for key, value in actual.items():
                if key in execution.domain_slots and value != execution.domain_slots[key]:
                    execution.warnings.append(f"slot_override_rejected:{key}")
                    raise ValueError(f"slot_override_rejected:{key}")
        if warehouse is None:
            payload = {"failed": True, "candidates": [], "warnings": ["stock_snapshot_stale"],
                       "sources": ["snapshot"], "as_of": ""}
        else:
            try:
                snapshot = warehouse.latest_ready(date.today(), required=True)
                request = StockRequest(
                    scenario="stock_screening", strategy=strategy, horizon=horizon,
                    industries=list(industries or []), index_scope=list(index_scope or []),
                    candidate_count=candidate_count, filter_by_profile=filter_by_profile,
                )
                membership: dict[str, list[str]] | None = None
                if request.index_scope:
                    membership = {}
                    for row in store.list(list(membership_evidence_ids or []), current_stock_execution().context):
                        data = _data(row["payload"])
                        if not isinstance(data, dict):
                            continue
                        index_code = str(data.get("thscode") or data.get("index_code") or "")
                        members = data.get("members") or data.get("items") or []
                        codes = []
                        for item in members:
                            if isinstance(item, str):
                                codes.append(item)
                            elif isinstance(item, dict):
                                code = str(item.get("thscode") or item.get("ts_code") or "")
                                if code:
                                    codes.append(code)
                        if index_code and codes:
                            membership[index_code] = codes
                payload = screen_candidates(
                    request, rows=warehouse.universe_rows(snapshot["id"]),
                    profile=current_stock_execution().profile,
                    snapshot_id=snapshot["id"], as_of=str(snapshot.get("source_as_of") or snapshot["trade_date"]),
                    membership=membership,
                )
            except (SnapshotStaleError, ValueError) as exc:
                payload = {"failed": True, "candidates": [], "warnings": [getattr(exc, "code", str(exc))],
                           "sources": ["snapshot"], "as_of": ""}
        return _save(store, name="score_stock_candidates", evidence_type="stock_screening",
                     payload=payload, source="snapshot", as_of=str(payload.get("as_of") or ""))

    return [
        request_stock_clarification,
        calculate_technical_indicators,
        analyze_stock_fundamentals,
        collect_stock_events,
        aggregate_market_snapshot,
        score_stock_candidates,
    ]


__all__ = [
    "STOCK_AGENT_VERSION",
    "bind_stock_execution",
    "build_stock_local_tools",
    "current_stock_execution",
    "reset_stock_execution",
]
