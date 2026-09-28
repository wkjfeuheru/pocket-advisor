"""选股硬过滤。"""

from __future__ import annotations

from typing import Any

MIN_LISTED_DAYS = 120
MIN_TURNOVER = 50_000_000
STRATEGY_KEYS = {
    "value_quality": ("earnings_quality", "cashflow_quality"),
    "growth_quality": ("growth", "earnings_quality"),
    "trend_momentum": ("momentum", "trend"),
}


def apply_hard_filters(rows: list[dict[str, Any]], *, strategy: str) -> list[dict[str, Any]]:
    required = STRATEGY_KEYS.get(strategy, ())
    kept = []
    for row in rows:
        name = str(row.get("name") or "")
        if row.get("is_st") or name.startswith("ST") or name.startswith("*ST"):
            continue
        if row.get("suspended"):
            continue
        if int(row.get("listed_days") or 0) < MIN_LISTED_DAYS:
            continue
        assets = row.get("net_assets")
        if assets is None or float(assets) < 0:
            continue
        if float(row.get("avg_turnover_20") or 0) < MIN_TURNOVER:
            continue
        if row.get("one_word_limit") or int(row.get("limit_up_streak") or 0) >= 2:
            continue
        if required and any(row.get(key) is None for key in required):
            continue
        kept.append(row)
    return kept


__all__ = ["MIN_LISTED_DAYS", "MIN_TURNOVER", "apply_hard_filters"]
