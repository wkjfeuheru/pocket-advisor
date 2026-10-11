"""把消毒后的 StockResultV1 收成结构化的 StockResult，去掉买卖指令。"""

from __future__ import annotations

from typing import Any

from app.agents.graphs.state import CandidateStock, StockResult
from app.agents.shared.bans import build_pattern, strip_forbidden
from app.agents.stock.contracts import StockRequest, StockResultV1
from app.agents.stock.evidence import apply_evidence_guard
from app.services.portfolio_service import SIMULATED_DISCLAIMER

_FORBIDDEN = build_pattern((
    "买入", "卖出", "加仓", "减仓", "目标价", "买卖点", "仓位比例", r"仓位\s*\d+\s*%",
))


def _strip_orders(text: str) -> str:
    return strip_forbidden(text, _FORBIDDEN)


def _to_candidate(item: Any) -> CandidateStock:
    """只保留公开三元组；候选池的内层字段（components/risks 等）不进 StockResult。"""
    if not isinstance(item, dict):
        item = {}
    return CandidateStock(
        thscode=str(item.get("thscode") or ""),
        name=str(item.get("name") or ""),
        score=item.get("score"),
    )


def render_stock_result(result: StockResultV1 | dict[str, Any], request: StockRequest) -> StockResult:
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    guarded = apply_evidence_guard(payload, request)
    summary = _strip_orders(str(guarded.get("summary") or ""))
    if SIMULATED_DISCLAIMER not in summary:
        summary = f"{summary.rstrip()}\n\n{SIMULATED_DISCLAIMER}".strip()
    guarded["summary"] = summary
    candidates = list(guarded.get("candidate_stocks") or [])
    if guarded.get("stock_screening"):
        candidates = list(guarded["stock_screening"].get("candidates") or [])
    warnings = list(guarded.get("warnings") or [])
    quality = str(guarded.get("evidence_quality") or "")
    return StockResult(
        status="degraded" if warnings or quality == "failed" else "ok",
        summary=summary,
        warnings=warnings,
        source=",".join(guarded.get("sources") or []) or "fuyao",
        as_of=str(guarded.get("as_of") or ""),
        result=StockResultV1.model_validate(
            {key: value for key, value in guarded.items() if key in StockResultV1.model_fields}
        ),
        candidates=[_to_candidate(item) for item in candidates],
    )


__all__ = ["render_stock_result"]
