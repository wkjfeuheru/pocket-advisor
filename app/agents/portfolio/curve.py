"""从真实交易流水重建用户的历史净值曲线，并以时间加权收益（TWR）剔除现金流影响。

为什么需要 TWR：用户会不定期入金/出金。直接看「总资产涨幅」会把入金当成收益，
频繁入金的用户永远显示跑赢，出金的用户永远显示亏损。TWR 把每日现金流从当日收益中
剔除，只度量**投资决策本身**的收益——这样它才能和 AI 账户那条线同图比较。

现金流约定：``daily_return[t] = (总资产[t] - 现金流[t]) / 总资产[t-1] - 1``，
即当日入金/出金算作**日终**流入，不参与当日收益。若让入金参与当日收益，
用户在高点前入金就会凭空"赚"一笔，TWR 失真。

纯函数模块：不碰数据库、不调模型、不引入第三方数值库。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from app.agents.portfolio.backtest import (
    BacktestError,
    annualized_volatility,
    max_drawdown,
)

#: 视为外部现金流、需要从收益中剔除的流水类型。买卖与费用属于投资行为，不剔除。
CASH_FLOW_KINDS = frozenset({"deposit", "withdraw"})


@dataclass(frozen=True)
class UserCurve:
    """用户历史净值重建结果。``equity_curve`` 以区间首日为 1.0。"""

    dates: list[date]
    equity_curve: list[float]
    total_assets: list[float]
    market_values: list[float]
    cash_balances: list[float]
    daily_returns: list[float]
    time_weighted_return: float
    cumulative_return: float
    max_drawdown: float
    annualized_volatility: float


def _date_of(row: Mapping[str, object], *keys: str) -> date:
    for key in keys:
        value = row.get(key)
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value:
            return date.fromisoformat(value[:10])
    raise BacktestError(f"流水缺少日期字段，期望其中之一: {keys}")


def _as_float(value: object, *, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)  # type: ignore[arg-type]


def rebuild_user_curve(
    *,
    orders: Sequence[Mapping[str, object]],
    flows: Sequence[Mapping[str, object]],
    nav_series: Mapping[str, Sequence[tuple[date, float]]],
    start: date,
    end: date,
) -> UserCurve:
    """逐日重建持仓与市值，返回归一净值曲线与 TWR。

    ``orders`` 是已成交的买卖流水（``side`` / ``shares`` / ``price`` / 日期），
    ``flows`` 是外部现金流（``kind`` / ``amount`` / 日期），``nav_series`` 是各标的
    ``(交易日, 净值)`` 序列。区间内任一持仓标的缺净值即抛 ``BacktestError``。
    """
    if end < start:
        raise BacktestError("结束日期早于开始日期")

    lookups = {code: {day: float(value) for day, value in series} for code, series in nav_series.items()}

    # 交易日轴：优先取净值序列日期，没有就退回区间端点。
    day_set: set[date] = set()
    for series in nav_series.values():
        day_set.update(day for day, _ in series if start <= day <= end)
    if not day_set:
        day_set = {start, end}
    days = sorted(day_set)

    dated_orders = sorted(
        ((_date_of(row, "trade_date", "created_at"), row) for row in orders),
        key=lambda item: item[0],
    )
    dated_flows = sorted(
        ((_date_of(row, "flow_date", "created_at"), row) for row in flows),
        key=lambda item: item[0],
    )

    shares: dict[str, float] = {}
    cash = 0.0
    open_order = 0
    open_flow = 0

    total_assets: list[float] = []
    market_values: list[float] = []
    cash_balances: list[float] = []

    for day in days:
        while open_flow < len(dated_flows) and dated_flows[open_flow][0] <= day:
            row = dated_flows[open_flow][1]
            if str(row.get("kind")) in CASH_FLOW_KINDS:
                cash += _as_float(row.get("amount"))
            open_flow += 1

        while open_order < len(dated_orders) and dated_orders[open_order][0] <= day:
            row = dated_orders[open_order][1]
            code = str(row.get("product_code") or "")
            count = _as_float(row.get("shares"))
            price = _as_float(row.get("price"))
            if code:
                if str(row.get("side")) == "sell":
                    shares[code] = shares.get(code, 0.0) - count
                    cash += count * price
                else:
                    shares[code] = shares.get(code, 0.0) + count
                    cash -= count * price
            open_order += 1

        value = 0.0
        for code, count in shares.items():
            if count == 0:
                continue
            price = lookups.get(code, {}).get(day)
            if price is None:
                raise BacktestError(f"{code} 在 {day} 缺少净值，无法重建用户曲线")
            value += count * price

        market_values.append(value)
        cash_balances.append(cash)
        total_assets.append(value + cash)

    # 现金流按日汇总，供 TWR 剔除。
    flow_by_day: dict[date, float] = {day: 0.0 for day in days}
    for day, row in dated_flows:
        if day in flow_by_day and str(row.get("kind")) in CASH_FLOW_KINDS:
            flow_by_day[day] += _as_float(row.get("amount"))

    daily_returns: list[float] = []
    for index in range(1, len(days)):
        previous = total_assets[index - 1]
        if previous <= 0:
            daily_returns.append(0.0)
            continue
        adjusted = total_assets[index] - flow_by_day[days[index]]
        daily_returns.append(adjusted / previous - 1.0)

    twr = 1.0
    for value in daily_returns:
        twr *= 1.0 + value
    twr -= 1.0

    first = total_assets[0] if total_assets else 0.0
    equity_curve = [value / first if first > 0 else 1.0 for value in total_assets]

    # 回撤必须走 TWR 曲线，不能走含现金流的权益曲线。现金流应当被剔除，
    # 否则一笔入金会被记成"上涨"、一笔出金被记成"回撤"。这里用日收益率
    # 连乘重建归一曲线，再在其上求回撤。
    twr_curve = [1.0]
    running = 1.0
    for value in daily_returns:
        running *= 1.0 + value
        twr_curve.append(running)

    return UserCurve(
        dates=days,
        equity_curve=equity_curve,
        total_assets=total_assets,
        market_values=market_values,
        cash_balances=cash_balances,
        daily_returns=daily_returns,
        time_weighted_return=twr,
        cumulative_return=(equity_curve[-1] - 1.0) if equity_curve else 0.0,
        max_drawdown=max_drawdown(twr_curve),
        annualized_volatility=annualized_volatility(daily_returns),
    )


__all__ = ["CASH_FLOW_KINDS", "UserCurve", "rebuild_user_curve"]
