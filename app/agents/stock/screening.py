"""研究候选池：硬过滤 + 评分后截取 1–10 只。"""

from __future__ import annotations

from typing import Any

from app.agents.stock.contracts import StockRequest
from app.agents.stock.scoring import score_universe

_CONSERVATIVE_VOL = 0.4
_CONSERVATIVE_DRAWDOWN = 0.3


def _apply_profile(rows: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    risk = str(profile.get("risk_level") or "")
    liquidity = str(profile.get("liquidity_need") or "")
    kept = []
    for row in rows:
        vol = float(row.get("volatility") or 0)
        drawdown = float(row.get("max_drawdown") or 0)
        if risk in {"R1", "R2"} and (vol > _CONSERVATIVE_VOL or drawdown > _CONSERVATIVE_DRAWDOWN):
            continue
        if liquidity == "高" and float(row.get("avg_turnover_20") or 0) < 50_000_000:
            continue
        kept.append(row)
    return kept


def _risks(row: dict[str, Any]) -> list[str]:
    notes = []
    if float(row.get("volatility") or 0) > 0.4:
        notes.append("波动偏高")
    if float(row.get("max_drawdown") or 0) > 0.25:
        notes.append("回撤偏大")
    if row.get("is_st"):
        notes.append("ST")
    return notes


def _scope(rows: list[dict[str, Any]], request: StockRequest, membership: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    universe = list(rows)
    if request.industries:
        allowed = {item.strip() for item in request.industries if item}
        universe = [row for row in universe if str(row.get("industry") or "") in allowed]
    if request.index_scope:
        if not membership:
            return None
        members: set[str] = set()
        for index in request.index_scope:
            found = list((membership or {}).get(index) or [])
            if not found:
                return None
            members.update(found)
        universe = [row for row in universe if str(row.get("thscode")) in members]
    return universe


def screen_candidates(
    request: StockRequest,
    *,
    rows: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    snapshot_id: str = "",
    as_of: str = "",
    membership: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not request.strategy or not request.horizon:
        raise ValueError("missing_strategy_or_horizon")
    universe = _scope(rows, request, membership)
    if universe is None:
        return {
            "failed": True,
            "candidates": [],
            "candidate_stocks": [],
            "recommended_product_codes": [],
            "snapshot_id": snapshot_id,
            "as_of": as_of,
            "warnings": ["index_scope_unavailable"],
            "sources": ["snapshot"],
        }
    scored = score_universe(universe, strategy=request.strategy, horizon=request.horizon)
    if request.filter_by_profile:
        scored = _apply_profile(scored, profile or {})
    scored = scored[: max(1, min(10, request.candidate_count))]
    candidates = []
    for row in scored:
        components = dict(row.get("components") or {})
        candidates.append({
            "thscode": row["thscode"],
            "name": row.get("name") or "",
            "score": row["score"],
            "components": components,
            "evidence": [f"{key}={value:.1f}" for key, value in components.items()],
            "risks": _risks(row),
            "as_of": as_of,
            "snapshot_id": snapshot_id,
            "source": "snapshot",
        })
    return {
        "failed": not candidates,
        "candidates": candidates,
        "candidate_stocks": [
            {"thscode": item["thscode"], "name": item["name"], "score": item["score"]}
            for item in candidates
        ],
        "recommended_product_codes": [],
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "warnings": [],
        "sources": ["snapshot"],
    }


__all__ = ["screen_candidates"]
