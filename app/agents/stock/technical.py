"""K 线技术指标。缺数据返回 unavailable，不编造。"""

from __future__ import annotations

from typing import Any


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    chunk = values[-window:]
    return sum(chunk) / window


def _ema(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (window + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _rsi(closes: list[float], window: int = 14) -> float | None:
    if len(closes) <= window:
        return None
    gains = []
    losses = []
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-window:]) / window
    avg_loss = sum(losses[-window:]) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: list[dict], window: int = 14) -> float | None:
    if len(bars) < window + 1:
        return None
    trs = []
    for index in range(1, len(bars)):
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        prev_close = float(bars[index - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-window:]) / window


_REQUIRED_BAR_KEYS = ("close", "high", "low")


def _usable_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留可计算的 K 线：缺字段或非数值的行会让指标失真，直接剔除。"""
    usable = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        try:
            [float(bar[key]) for key in _REQUIRED_BAR_KEYS]
        except (KeyError, TypeError, ValueError):
            continue
        usable.append(bar)
    return usable


def compute_technicals(bars: list[dict[str, Any]]) -> dict[str, Any]:
    bars = _usable_bars(bars or [])
    if len(bars) < 20:
        return {"status": "unavailable", "ma": {}, "macd": None, "rsi": None, "atr": None}
    closes = [float(bar["close"]) for bar in bars]
    volumes = [float(bar.get("volume") or 0) for bar in bars]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12[-1] - ema26[-1]
    dea_series = _ema([a - b for a, b in zip(ema12, ema26)], 9)
    dea = dea_series[-1]
    last = closes[-1]
    peak = max(closes)
    drawdown = (peak - last) / peak if peak else 0.0
    trend = "sideways"
    if ma20 is not None and last > ma20:
        trend = "up"
    elif ma20 is not None and last < ma20:
        trend = "down"
    avg_volume = _sma(volumes, 20)
    return {
        "status": "ok",
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        "macd": {"dif": dif, "dea": dea, "hist": dif - dea},
        "rsi": _rsi(closes),
        "atr": _atr(bars),
        "volume": {"last": volumes[-1], "ma20": avg_volume},
        "trend": trend,
        "drawdown": drawdown,
        "support": min(closes[-20:]),
        "resistance": max(closes[-20:]),
        "summary": {"latest_price": last, "trend": trend},
    }


__all__ = ["compute_technicals"]
