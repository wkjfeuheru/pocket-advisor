"""把消毒后的 StockResultV1 收成 AgentResult，去掉买卖指令。"""

from __future__ import annotations

from typing import Any

from app.agents.shared.bans import build_pattern, strip_forbidden
from app.agents.stock.contracts import StockRequest, StockResultV1
from app.agents.stock.evidence import apply_evidence_guard, public_stock_artifact
from app.services.portfolio_service import SIMULATED_DISCLAIMER

_FORBIDDEN = build_pattern((
    "买入", "卖出", "加仓", "减仓", "目标价", "买卖点", "仓位比例", r"仓位\s*\d+\s*%",
))


def _strip_orders(text: str) -> str:
    return strip_forbidden(text, _FORBIDDEN)


def _public_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """公开 artifact 由版本化深层白名单生成，不做浅层删字段。"""
    return public_stock_artifact(payload)


def render_stock_result(result: StockResultV1 | dict[str, Any], request: StockRequest) -> dict[str, Any]:
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    guarded = apply_evidence_guard(payload, request)
    summary = _strip_orders(str(guarded.get("summary") or ""))
    if SIMULATED_DISCLAIMER not in summary:
        summary = f"{summary.rstrip()}\n\n{SIMULATED_DISCLAIMER}".strip()
    guarded["summary"] = summary
    artifact = _public_artifact(guarded)
    candidates = list(guarded.get("candidate_stocks") or [])
    if guarded.get("stock_screening"):
        candidates = [
            {"thscode": item.get("thscode"), "name": item.get("name") or "", "score": item.get("score")}
            for item in (guarded["stock_screening"].get("candidates") or [])
        ]
    return {
        "agent": "stock_agent",
        "content": summary,
        "recommended_product_codes": [],
        "candidate_stocks": candidates,
        "artifacts": [artifact],
        "warnings": list(guarded.get("warnings") or []),
        "source": ",".join(guarded.get("sources") or []) or "fuyao",
        "as_of": guarded.get("as_of") or "",
        "failed": bool(guarded.get("failed")),
        "stock_result": guarded,
    }


__all__ = ["render_stock_result"]
