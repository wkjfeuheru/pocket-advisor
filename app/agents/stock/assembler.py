"""根据证据句柄组装公开 StockResultV1，不信任模型生成的数值。"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from app.agents.graphs.state import StockResult
from app.agents.stock.contracts import StockAgentResponse, StockRequest, StockResultV1
from app.agents.stock.evidence_store import EvidenceAccessError
from app.agents.stock.render import render_stock_result
from app.agents.compliance_guard import numeric_claims

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?")


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("payload")
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return dict(value["data"])
    return dict(value) if isinstance(value, dict) else {}


def _symbol(payload: dict[str, Any]) -> str:
    return str(payload.get("thscode") or payload.get("ts_code") or payload.get("symbol") or "")


def _generic_failure(response: StockAgentResponse, warning: str) -> StockResult:
    """无证据时的降级结果：有产出（通用定性说明），但不算整轮失败。"""
    block = {"market_overview": {"failed": True, "indices": [], "breadth": {}, "sectors": {}}} if response.scenario == "market_overview" else (
        {"stock_screening": {"failed": True, "candidates": []}} if response.scenario == "stock_screening" else {"equity_research": []})
    return StockResult(
        status="degraded",
        summary="当前缺少可核验数据，暂不能完成该查询。",
        warnings=list(dict.fromkeys([*response.warnings, warning])),
        result=StockResultV1(scenario=response.scenario, summary="当前缺少可核验数据，暂不能完成该查询。",
            warnings=list(dict.fromkeys([*response.warnings, warning])), evidence_quality="failed",
            **block),
    )


def _numbers(value: Any) -> set[str]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float)):
        return {format(float(value), ".12g")}
    if isinstance(value, str):
        return {format(float(item), ".12g") for item in _NUMBER.findall(value)}
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found.update(_numbers(item))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for item in value:
            found.update(_numbers(item))
        return found
    return set()


def _guard_summary(summary: str, evidence_rows: list[dict[str, Any]]) -> tuple[str, bool]:
    supported = _numbers([row.get("payload") for row in evidence_rows])
    changed, pieces = False, []
    for part in re.findall(r"[^。！？；\n]*[。！？；\n]|[^。！？；\n]+$", summary):
        claims = numeric_claims(part)
        if any(not _NUMBER.fullmatch(value) or format(float(value), ".12g") not in supported for value in claims):
            changed = True
            continue
        pieces.append(part)
    text = "".join(pieces).strip()
    if not text and changed:
        text = "已获取相关证据，具体数值请查看结构化结果。"
    return text if changed else summary, changed


def assemble_stock_result(response: StockAgentResponse | dict[str, Any], context: Any, store: Any) -> StockResult:
    parsed = response if isinstance(response, StockAgentResponse) else StockAgentResponse.model_validate(response)
    rows = []
    try:
        rows = store.list(list(dict.fromkeys(parsed.evidence_ids)), context)
    except EvidenceAccessError:
        return _generic_failure(parsed, "evidence_not_available")
    if not rows:
        return _generic_failure(parsed, "missing_stock_evidence")

    sources = sorted({str(row.get("source") or "") for row in rows if row.get("source")})
    as_of = max((str(row.get("as_of") or "") for row in rows), default="")
    warnings = list(parsed.warnings)
    kwargs: dict[str, Any] = {}

    if parsed.scenario == "market_overview":
        found = next((_payload(row) for row in reversed(rows) if row.get("evidence_type") == "market_overview"), None)
        if not found or found.get("failed") or not (found.get("indices") or found.get("historical_indices")):
            return _generic_failure(parsed, "missing_market_aggregate")
        kwargs["market_overview"] = found
        warnings.extend(found.get("warnings") or [])
    elif parsed.scenario == "stock_screening":
        found = next((_payload(row) for row in reversed(rows) if row.get("evidence_type") == "stock_screening"), None)
        if not found or not found.get("candidates"):
            return _generic_failure(parsed, "missing_screening_result")
        kwargs["stock_screening"] = found
    else:
        grouped: dict[str, dict[str, Any]] = defaultdict(dict)
        for row in rows:
            payload = _payload(row)
            kind = str(row.get("evidence_type") or "")
            if kind == "quote":
                quotes = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
                for quote in quotes:
                    if not isinstance(quote, dict):
                        continue
                    code = _symbol(quote) or _symbol(payload)
                    if not code:
                        continue
                    item = grouped[code]
                    item.setdefault("thscode", code)
                    item.setdefault("name", str(quote.get("name") or code))
                    item.setdefault("warnings", [])
                    item.setdefault("sources", []).append(str(row.get("source") or ""))
                    item["as_of"] = max(
                        str(item.get("as_of") or ""),
                        str(row.get("as_of") or quote.get("as_of") or ""),
                    )
                    item["quote"] = {
                        **quote,
                        "source": row.get("source") or "fuyao",
                        "as_of": row.get("as_of") or quote.get("as_of") or "",
                    }
                continue
            code = _symbol(payload)
            if not code:
                continue
            item = grouped[code]
            item.setdefault("thscode", code)
            item.setdefault("name", str(payload.get("name") or code))
            item.setdefault("warnings", [])
            item.setdefault("sources", [])
            item["sources"].append(str(row.get("source") or ""))
            item["as_of"] = max(str(item.get("as_of") or ""), str(row.get("as_of") or ""))
            if kind == "fundamentals":
                item["fundamentals"] = payload
            elif kind == "technicals":
                item["technicals"] = payload
            elif kind == "events":
                item["news"] = payload
        equities = []
        for item in grouped.values():
            quote = item.get("quote") or {}
            fundamentals = item.get("fundamentals") or {}
            technicals = item.get("technicals") or {}
            news = item.get("news") or {}
            has_quote = bool(
                quote.get("source")
                and quote.get("as_of")
                and any(isinstance(quote.get(key), (int, float)) for key in ("last", "change_pct"))
            )
            has_fundamentals = bool(
                fundamentals.get("source")
                and fundamentals.get("as_of")
                and any(
                    fundamentals.get(key) is not None
                    for key in (
                        "revenue_trend",
                        "profit_trend",
                        "roe",
                        "gross_margin",
                        "cashflow_quality",
                        "leverage",
                    )
                )
            )
            has_technicals = bool(
                technicals.get("status") == "ok"
                and technicals.get("source")
                and technicals.get("as_of")
            )
            has_news = bool(news.get("status") == "ok" and news.get("events"))
            has_evidence = has_quote or has_fundamentals or has_technicals or has_news
            item["failed"] = not has_evidence
            item.setdefault("conflicts", [])
            item.setdefault("risks", [])
            item.setdefault("watch", ["关注后续财报与公告"])
            item["sources"] = sorted({source for source in item["sources"] if source})
            if has_evidence:
                equities.append(item)
        if not equities:
            return _generic_failure(parsed, "missing_equity_evidence")
        kwargs["equity_research"] = equities

    guarded_summary, summary_changed = _guard_summary(parsed.summary, rows)
    if summary_changed:
        warnings.append("unsupported_narrative_numbers_removed")
    result = StockResultV1(
        scenario=parsed.scenario,
        summary=guarded_summary,
        as_of=as_of,
        sources=sources,
        warnings=warnings,
        evidence_quality=parsed.evidence_quality,
        **kwargs,
    )
    request = StockRequest(scenario=parsed.scenario, candidate_count=10)
    return render_stock_result(result, request)


__all__ = ["assemble_stock_result"]
