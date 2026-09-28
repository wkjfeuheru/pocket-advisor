"""每日快照写入：把当天的用户线与 AI 线固化下来。

曲线是**写出来的**，不是查询时算出来的。每天一条快照的好处：

- 历史不可篡改：行情回补或净值修正不会改变已经画出来的历史；
- 读取便宜：对比页只做一次范围查询，不需要重算整段历史；
- 两线同源：用户线与 AI 线共用一张表与一套口径，天然可比。

幂等：``(customer_id, owner, snapshot_date)`` 是主键，同一天重跑覆盖，不产生重复点。

═══════════════════════════════════════════════════════════════════════════
derivation —— 当日两条线
═══════════════════════════════════════════════════════════════════════════
用户（市值 1000、成本 950）：
    单位净值 = 市值 / 初始市值（首日基线）= 1.0（首日）
AI（initial_capital 1000、总资产 997.25：
    单位净值 = 997.25 / 1000 = 0.99725
    较上一条快照 1.0 → daily_return = -0.00275

两条线的单位净值都以各自 1.0 为起点，因此可以直接叠在同一张图上比较。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol, Sequence


class SnapshotError(ValueError):
    """快照写入不合法。``code`` 便于上层区分处理。"""

    def __init__(self, message: str, *, code: str = "snapshot_error") -> None:
        super().__init__(message)
        self.code = code


class SnapshotWriter(Protocol):
    """每日快照的写入接口（``owner`` 为 ``user`` 或 ``ai``）。"""

    def latest_snapshot(self, customer_id: str, owner: str) -> dict | None: ...

    def upsert_snapshot(self, customer_id: str, owner: str, record: Mapping[str, object]) -> None: ...


@dataclass
class SnapshotOutcome:
    """一次写入的结果。``skipped`` 表示该线当天没有可写的值。"""

    owner: str
    snapshot_date: date
    unit_nav: float = 0.0
    daily_return: float | None = None
    written: bool = False
    skipped_reason: str = ""


def _daily_return(previous: float | None, current: float) -> float | None:
    if previous is None or previous <= 0:
        return None
    return current / previous - 1.0


def write_snapshot(
    writer: SnapshotWriter,
    *,
    customer_id: str,
    owner: str,
    snapshot_date: date,
    total_assets: float,
    cash_balance: float,
    market_value: float,
    total_units: float,
    nav_as_of: date | None = None,
    limitations: Sequence[str] | None = None,
    max_drawdown: float | None = None,
    annualized_volatility: float | None = None,
) -> SnapshotOutcome:
    """写入某条线当天的快照。份额缺失时跳过（``skipped_reason=no_units``）。

    没有份额就算不出单位净值；与其编一个 1.0，不如不写——读取侧本来就会把
    "没有快照"表达成数据不足。

    ``baseline_units`` 会**从既有快照继承**：它是首日定下的锚，必须跨天稳定，
    否则 ``unit_nav`` 会退化成恒等于 1 的直线。
    """
    if owner not in ("user", "ai"):
        raise SnapshotError(f"未知的曲线归属: {owner}", code="bad_owner")
    if not math.isfinite(float(total_assets)):
        raise SnapshotError("总资产必须是有限数", code="bad_assets")

    if total_units is None or float(total_units) <= 0:
        return SnapshotOutcome(
            owner=owner,
            snapshot_date=snapshot_date,
            written=False,
            skipped_reason="no_units",
        )

    unit_nav = float(total_assets) / float(total_units)
    previous_row = writer.latest_snapshot(customer_id, owner)
    previous_nav = None
    baseline_units = float(total_units)
    if previous_row:
        if previous_row.get("unit_nav") is not None:
            previous_nav = float(previous_row["unit_nav"])
        inherited = previous_row.get("baseline_units")
        if inherited:
            baseline_units = float(inherited)

    writer.upsert_snapshot(
        customer_id,
        owner,
        {
            "snapshot_date": snapshot_date,
            "total_assets": float(total_assets),
            "cash_balance": float(cash_balance),
            "market_value": float(market_value),
            "unit_nav": unit_nav,
            "baseline_units": baseline_units,
            "daily_return": _daily_return(previous_nav, unit_nav),
            "max_drawdown": max_drawdown,
            "annualized_volatility": annualized_volatility,
            "nav_as_of": nav_as_of,
            "limitations": list(limitations or []),
        },
    )
    return SnapshotOutcome(
        owner=owner,
        snapshot_date=snapshot_date,
        unit_nav=unit_nav,
        daily_return=_daily_return(previous_nav, unit_nav),
        written=True,
    )


def write_both_curves(
    writer: SnapshotWriter,
    *,
    customer_id: str,
    snapshot_date: date,
    user: Mapping[str, object] | None,
    ai: Mapping[str, object] | None,
    nav_as_of: date | None = None,
) -> list[SnapshotOutcome]:
    """一次写两条线。任一条缺数据时只跳过它，不影响另一条。"""
    outcomes: list[SnapshotOutcome] = []
    for owner, payload in (("user", user), ("ai", ai)):
        if not payload:
            outcomes.append(
                SnapshotOutcome(
                    owner=owner,
                    snapshot_date=snapshot_date,
                    written=False,
                    skipped_reason="no_data",
                )
            )
            continue
        outcomes.append(
            write_snapshot(
                writer,
                customer_id=customer_id,
                owner=owner,
                snapshot_date=snapshot_date,
                total_assets=float(payload.get("total_assets") or 0.0),
                cash_balance=float(payload.get("cash_balance") or 0.0),
                market_value=float(payload.get("market_value") or 0.0),
                total_units=float(payload.get("total_units") or 0.0),
                nav_as_of=nav_as_of,
                limitations=payload.get("limitations") or [],
                max_drawdown=payload.get("max_drawdown"),
                annualized_volatility=payload.get("annualized_volatility"),
            )
        )
    return outcomes


__all__ = [
    "SnapshotError",
    "SnapshotOutcome",
    "SnapshotWriter",
    "write_both_curves",
    "write_snapshot",
]
