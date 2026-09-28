"""技术指标：缺失 K 线不编造。"""

from __future__ import annotations

from app.agents.stock.technical import compute_technicals


def test_moving_average_and_indicators_on_known_series():
    bars = [{"close": float(index), "high": float(index) + 1, "low": float(index) - 1, "volume": 1000.0} for index in range(1, 61)]
    result = compute_technicals(bars)
    assert result["status"] == "ok"
    assert result["ma"]["ma20"] == 50.5
    assert result["macd"]["dif"] is not None
    assert result["rsi"] is not None
    assert result["atr"] is not None
    assert result["trend"] in {"up", "down", "sideways"}
    assert result["drawdown"] >= 0
    assert result["support"] is not None
    assert result["resistance"] is not None
    assert "kline" not in result


def test_missing_kline_is_unavailable():
    result = compute_technicals([])
    assert result["status"] == "unavailable"
    assert result["ma"] == {}
    assert result.get("macd") is None
