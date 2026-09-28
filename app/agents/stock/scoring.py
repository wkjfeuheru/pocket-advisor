"""横截面缩尾、分位和三套策略加权。"""

from __future__ import annotations

from typing import Any

from app.agents.stock.filters import apply_hard_filters

WEIGHTS = {
    "value_quality": {
        "quality": 0.45,
        "valuation_inv": 0.35,
        "stability": 0.20,
    },
    "growth_quality": {
        "growth": 0.45,
        "earnings_quality": 0.30,
        "valuation_fit": 0.10,
        "stability": 0.15,
    },
    "trend_momentum": {
        "momentum": 0.45,
        "trend": 0.25,
        "liquidity": 0.15,
        "volatility_inv": 0.15,
    },
}
LONG_STABILITY_BOOST = 1.25


def _winsorize(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    low = ordered[max(int(n * 0.05) - 1, 0)]
    high = ordered[min(int(n * 0.95), n - 1)]
    return [min(max(value, low), high) for value in values]


def _percentiles(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1:
        return [50.0]
    ranked = sorted(range(len(values)), key=lambda index: (values[index], index))
    out = [0.0] * len(values)
    for rank, index in enumerate(ranked):
        out[index] = 100.0 * rank / (len(values) - 1)
    return out


HORIZON_FIELDS = {
    "short": ("ret_5", "ret_10", "ret_20"),
    "medium": ("ret_20", "ret_60"),
    "long": ("ret_60", "ret_120", "ret_250"),
}


def _horizon_value(row: dict, horizon: str, fallback: str) -> float | None:
    keys = HORIZON_FIELDS.get(horizon, ())
    present = [float(row[key]) for key in keys if row.get(key) is not None]
    if present:
        return sum(present) / len(present)
    value = row.get(fallback)
    return None if value is None else float(value)


def _column(rows: list[dict], key: str, *, invert: bool = False) -> list[float | None]:
    raw: list[float | None] = []
    for row in rows:
        value = row.get(key)
        if value is None:
            raw.append(None)
            continue
        number = float(value)
        raw.append(-number if invert else number)
    present = [value for value in raw if value is not None]
    clipped = _winsorize(present)
    ranked = _percentiles(clipped)
    out: list[float | None] = []
    cursor = 0
    for value in raw:
        if value is None:
            out.append(None)
        else:
            out.append(ranked[cursor])
            cursor += 1
    return out


def _weights(strategy: str, horizon: str) -> dict[str, float]:
    weights = dict(WEIGHTS[strategy])
    if horizon == "long" and "stability" in weights:
        weights["stability"] *= LONG_STABILITY_BOOST
        total = sum(weights.values())
        weights = {key: value / total for key, value in weights.items()}
    return weights


def _blend(left: list[float | None], right: list[float | None]) -> list[float | None]:
    blended = []
    for first, second in zip(left, right):
        present = [value for value in (first, second) if value is not None]
        blended.append(sum(present) / len(present) if present else None)
    return blended


def _components(rows: list[dict], strategy: str) -> list[dict[str, float | None]]:
    quality = None
    if strategy == "value_quality":
        quality = _blend(_column(rows, "earnings_quality"), _column(rows, "cashflow_quality"))
    table = {
        "quality": quality,
        "valuation_inv": _column(rows, "valuation", invert=True) if strategy == "value_quality" else None,
        "stability": _column(rows, "stability"),
        "growth": _column(rows, "growth"),
        "earnings_quality": _column(rows, "earnings_quality"),
        "valuation_fit": _column(rows, "valuation_fit"),
        "momentum": _column(rows, "momentum"),
        "trend": _column(rows, "trend"),
        "liquidity": _column(rows, "liquidity"),
        "volatility_inv": _column(rows, "volatility", invert=True),
    }
    packed = []
    for index, _row in enumerate(rows):
        packed.append({key: values[index] for key, values in table.items() if values is not None})
    return packed


def _prepare(rows: list[dict], strategy: str, horizon: str) -> list[dict]:
    prepared = []
    for row in rows:
        item = dict(row)
        if strategy == "trend_momentum":
            item["momentum"] = _horizon_value(row, horizon, "momentum")
            item["trend"] = _horizon_value(row, horizon, "trend")
        prepared.append(item)
    return prepared


def _score_group(rows: list[dict], *, strategy: str, horizon: str) -> list[dict[str, Any]]:
    prepared = _prepare(rows, strategy, horizon)
    weights = _weights(strategy, horizon)
    components = _components(prepared, strategy)
    scored = []
    for row, parts in zip(prepared, components):
        active = {key: parts[key] for key in weights if parts.get(key) is not None}
        if not active:
            total = 0.0
        else:
            scale = sum(weights[key] for key in active)
            total = sum(weights[key] / scale * active[key] for key in active)
        scored.append({**row, "score": total, "components": active})
    return scored


def score_universe(rows: list[dict[str, Any]], *, strategy: str, horizon: str) -> list[dict[str, Any]]:
    kept = apply_hard_filters(rows, strategy=strategy)
    if not kept:
        return []
    groups: dict[str, list[dict]] = {}
    for row in kept:
        groups.setdefault(str(row.get("industry") or ""), []).append(row)
    scored = []
    for group in groups.values():
        scored.extend(_score_group(group, strategy=strategy, horizon=horizon))
    scored.sort(key=lambda item: (-item["score"], item["thscode"]))
    return scored


__all__ = ["LONG_STABILITY_BOOST", "WEIGHTS", "score_universe"]
