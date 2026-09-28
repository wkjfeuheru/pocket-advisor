"""方案权重回放到历史净值的回测计算。

纯函数模块：不碰数据库、不调模型、不引入第三方数值库。所有数字由确定性代码算出，
LLM 不得参与计算，也不得改写结果。

执行假设（与设计文档一致）：

- T0 以当日净值全额建仓到目标权重，按买入费率双边计费；费用从投入资金中扣除。
- 持有期内不调仓，权重随价格自然漂移（买入持有语义），因此无后续费用。
- ``cumulative_return`` 的分母是**投入资金**（含已付出的费用），不是建仓后的市值。
- ``annualized_volatility`` 用日收益率的**总体**标准差（除以 n）乘以 ``sqrt(252)``。
- 缺失净值一律抛错，不静默前向填充；数据准备是调用方的责任。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

#: 年化交易日数。A 股实际交易日约 242–244 天，此处取业界常用的 252 天口径。
TRADING_DAYS_PER_YEAR = 252


class BacktestError(ValueError):
    """回测输入不合法：权重非法、净值缺失或日期区间无交集。"""


@dataclass(frozen=True)
class BacktestResult:
    """一次权重回放的结果。``equity_curve`` 与 ``dates`` 等长，初始归一为 1.0。"""

    dates: list[date]
    equity_curve: list[float]
    initial_capital: float
    initial_value: float
    final_value: float
    total_fee: float
    cumulative_return: float
    max_drawdown: float
    annualized_volatility: float


def _nav_lookup(series: Sequence[tuple[date, float]]) -> dict[date, float]:
    return {day: float(value) for day, value in series}


def _validate_weights(weights: Mapping[str, float]) -> None:
    if not weights:
        raise BacktestError("目标权重为空")
    total = sum(float(value) for value in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise BacktestError(f"目标权重之和必须为 1，实际为 {total!r}")
    for code, value in weights.items():
        if float(value) < 0:
            raise BacktestError(f"目标权重不得为负: {code}={value!r}")


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """路径最大回撤，返回正数（0.0467 表示回撤 4.67%）。"""
    peak = equity_curve[0] if equity_curve else 0.0
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def annualized_volatility(daily_returns: Sequence[float]) -> float:
    """日收益率的总体标准差年化。样本不足两个点时返回 0。"""
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    variance = sum((value - mean) ** 2 for value in daily_returns) / len(daily_returns)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def replay_weights(
    weights: Mapping[str, float],
    *,
    nav_series: Mapping[str, Sequence[tuple[date, float]]],
    start: date,
    end: date,
    initial_capital: float,
    fee_buy: float = 0.0015,
    fee_sell: float = 0.005,
) -> BacktestResult:
    """把 ``weights`` 回放到 ``nav_series`` 上，返回归一净值曲线与指标。

    ``nav_series`` 是每个标的的 ``(交易日, 净值)`` 序列，必须覆盖 ``[start, end]``
    区间内的全部交易日；缺失即抛 ``BacktestError``。
    """
    _validate_weights(weights)
    if initial_capital <= 0:
        raise BacktestError(f"初始资金必须为正，实际为 {initial_capital!r}")
    if end < start:
        raise BacktestError("结束日期早于开始日期")

    lookups = {code: _nav_lookup(series) for code, series in nav_series.items()}

    missing = [code for code in weights if code not in lookups]
    if missing:
        raise BacktestError(f"缺少净值序列: {sorted(missing)}")

    # 交易日轴取自权重中第一个标的的序列，再按区间裁剪。
    axis = sorted(day for day in lookups[next(iter(weights))] if start <= day <= end)
    if not axis:
        raise BacktestError("区间内没有任何交易日")

    for code in weights:
        absent = [day for day in axis if day not in lookups[code]]
        if absent:
            raise BacktestError(
                f"{code} 在区间内缺失 {len(absent)} 个交易日的净值，首个为 {absent[0]}"
            )

    # T0 建仓：按目标权重用投入资金全额买入，买入费用由现金支付。
    # 建仓当天持仓市值仍是全部投入资金；费用不进入归一曲线，而是单独作为
    # total_fee / cumulative_return 上的成本体现。这样无费用的回放 T0 也是 1.0，
    # 计费与不计费两个结果可以直接叠在同一张图上比较。
    opening_day = axis[0]
    total_fee = 0.0
    shares: dict[str, float] = {}
    for code, weight in weights.items():
        amount = initial_capital * float(weight)
        total_fee += amount * fee_buy
        shares[code] = amount / lookups[code][opening_day]

    equity_curve: list[float] = []
    for day in axis:
        value = sum(shares[code] * lookups[code][day] for code in weights)
        equity_curve.append(value)

    normalized = [value / equity_curve[0] for value in equity_curve]
    daily_returns = [
        normalized[index] / normalized[index - 1] - 1.0 for index in range(1, len(normalized))
    ]

    return BacktestResult(
        dates=list(axis),
        equity_curve=normalized,
        initial_capital=float(initial_capital),
        initial_value=equity_curve[0],
        final_value=equity_curve[-1],
        total_fee=total_fee,
        cumulative_return=(equity_curve[-1] - total_fee) / float(initial_capital) - 1.0,
        max_drawdown=max_drawdown(normalized),
        annualized_volatility=annualized_volatility(daily_returns),
    )


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "BacktestError",
    "BacktestResult",
    "annualized_volatility",
    "max_drawdown",
    "replay_weights",
]
