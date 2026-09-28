"""持仓估值：把账户 + 持仓 + 当日净值算成一份可直接展示的快照。

这是"实时展示用户持仓和 AI 账户的收益对比"的取数入口。用户线与 AI 线共用本函数——
两条线口径完全一致，才谈得上可比。纯函数，不碰数据库。

三条容易写错的约定：

1. ``unit_nav`` 的分母是**份额**，不是总成本、也不是市值。拿总成本当分母会算出一个
   假的收益率。
2. 份额缺失（0 或未建立）时返回 ``unit_nav = None`` 并记入 ``limitations``，
   **不要**用别的东西凑一个数——页面宁可显示"暂无"，也不能显示一个错的净值。
3. 不可定价持仓按成本计价、浮盈记 0，并强制写进 ``limitations``。否则用户会以为
   自己那部分资产凭空消失了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence


class ValuationError(ValueError):
    """估值输入不合法。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "invalid_valuation") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PositionValuation:
    """单个持仓的估值结果。``weight`` 是按总资产算的市值占比（含现金口径）。"""

    product_code: str
    shares: float
    cost_amount: float
    market_value: float
    unrealized_pnl: float
    weight: float
    priced: bool


@dataclass
class PortfolioValuation:
    """整户估值快照。"""

    positions: list[PositionValuation]
    cash_balance: float
    market_value: float
    total_assets: float
    total_cost: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    unit_nav: float | None
    cash_weight: float
    nav_as_of: date | None
    pricing_complete: bool
    limitations: list[str] = field(default_factory=list)


def value_portfolio(
    *,
    positions: Sequence[Mapping[str, Any]],
    cash_balance: float,
    navs: Mapping[str, float],
    total_units: float,
    nav_as_of: date | None = None,
    realized_pnl: float = 0.0,
) -> PortfolioValuation:
    """给一份持仓估值。

    ``positions`` 每项需要 ``product_code`` / ``shares`` / ``cost_amount``；
    ``priceable=False`` 表示该标的没有日频净值，按成本计价。
    """
    limitations: list[str] = []
    valued: list[PositionValuation] = []
    pricing_complete = True

    market_value = 0.0
    total_cost = 0.0

    for row in positions:
        product_code = str(row.get("product_code") or "")
        if not product_code:
            raise ValuationError("持仓缺少 product_code", code="missing_product")

        shares = float(row.get("shares") or 0.0)
        cost = float(row.get("cost_amount") or 0.0)
        priceable = bool(row.get("priceable", True))
        total_cost += cost

        if not priceable:
            # 没有净值就不估值：按成本计，浮盈记 0，并声明出来。
            pricing_complete = False
            limitations.append(f"{product_code} 无日频净值，按成本计价，未计入收益")
            valued.append(
                PositionValuation(
                    product_code=product_code,
                    shares=shares,
                    cost_amount=cost,
                    market_value=cost,
                    unrealized_pnl=0.0,
                    weight=0.0,
                    priced=False,
                )
            )
            market_value += cost
            continue

        price = navs.get(product_code)
        if price is None:
            raise ValuationError(
                f"{product_code} 缺少当日净值，无法估值", code="missing_nav"
            )

        value = shares * float(price)
        market_value += value
        valued.append(
            PositionValuation(
                product_code=product_code,
                shares=shares,
                cost_amount=cost,
                market_value=value,
                unrealized_pnl=value - cost,
                weight=0.0,
                priced=True,
            )
        )

    total_assets = market_value + float(cash_balance)
    unrealized = market_value - total_cost

    # 权重按总资产（含现金）算，合计才是 1。
    if total_assets > 0:
        for item in valued:
            item.weight = item.market_value / total_assets
    cash_weight = (float(cash_balance) / total_assets) if total_assets > 0 else 0.0

    if total_units and total_units > 0:
        unit_nav: float | None = total_assets / float(total_units)
    else:
        unit_nav = None
        limitations.append("缺少份额，无法计算单位净值")

    if not pricing_complete:
        limitations.append("部分持仓无净值，收益对比未包含该部分")

    return PortfolioValuation(
        positions=valued,
        cash_balance=float(cash_balance),
        market_value=market_value,
        total_assets=total_assets,
        total_cost=total_cost,
        unrealized_pnl=unrealized,
        realized_pnl=float(realized_pnl),
        total_pnl=unrealized + float(realized_pnl),
        unit_nav=unit_nav,
        cash_weight=cash_weight,
        nav_as_of=nav_as_of,
        pricing_complete=pricing_complete,
        limitations=limitations,
    )


__all__ = [
    "PortfolioValuation",
    "PositionValuation",
    "ValuationError",
    "value_portfolio",
]
