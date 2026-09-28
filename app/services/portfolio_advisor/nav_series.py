"""净值序列读取器：把存储里的日频净值变成引擎能直接吃的形状。

回测（``replay_weights``）与用户曲线（``rebuild_user_curve``）都要
``{code: [(date, nav), ...]}``。这是存储与计算之间唯一的接缝，口径一次定清楚：

- **交易日轴**取范围内全部标的日期的并集，而不是各自日期。两只产品缺不同天时，
  对齐到并集才能同图比较。
- **前向填充**：某标的某天没有净值时沿用最近一个有效值，并记进 ``filled``。
  漏导一天在运维里是常态，不填充会让整条曲线直接报错。
- **填充不得凭空造收益**：沿用昨日净值意味着当天涨跌幅为 0。这不是"跳过缺失日"——
  跳过会让相邻两天被当成一天，收益率被压缩。
- **``as_of`` 是数据截止日**，不是查询日。页面必须显示它，否则用户以为看的是实时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence


class NavSeriesError(ValueError):
    """无法构造净值序列。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "invalid_series") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class NavSeries:
    """对齐后的净值序列。

    ``series`` 只含真的拿到数据的标的；查不到数据的标的进 ``missing``，
    **不会被静默丢掉**。
    """

    dates: list[date]
    series: dict[str, list[tuple[date, float]]]
    filled: dict[str, list[date]]
    missing: list[str]
    as_of: date | None
    complete: bool
    limitations: list[str] = field(default_factory=list)

    def weights_input(self) -> dict[str, list[tuple[date, float]]]:
        """直接喂给 ``replay_weights`` / ``rebuild_user_curve`` 的形状。"""
        return self.series


def build_nav_series(
    store: object,
    *,
    product_codes: Sequence[str],
    start: date,
    end: date,
) -> NavSeries:
    """读取并对齐若干标的在 ``[start, end]`` 内的日频净值。

    ``store`` 需实现 ``NavStore`` 协议（``history`` / ``latest``）。
    """
    if end < start:
        raise NavSeriesError("结束日期早于开始日期", code="bad_range")

    codes = [str(code) for code in product_codes if str(code)]
    if not codes:
        raise NavSeriesError("必须指定至少一个标的", code="no_products")

    history_fn = getattr(store, "history", None)
    if history_fn is None:
        raise NavSeriesError("存储未实现 history 方法", code="bad_store")

    raw: dict[str, list[tuple[date, float]]] = {}
    missing: list[str] = []
    for code in codes:
        rows = history_fn(code, start, end)
        if rows:
            raw[code] = [(day, float(nav)) for day, nav in rows]
        else:
            missing.append(code)

    if not raw:
        raise NavSeriesError(
            "区间内没有任何标的的净值数据", code="no_data"
        )

    # 交易日轴 = 全部标的日期并集。
    axis = sorted({day for rows in raw.values() for day, _ in rows})

    series: dict[str, list[tuple[date, float]]] = {}
    filled: dict[str, list[date]] = {}
    limitations: list[str] = []
    last_real: list[date] = []

    for code, rows in raw.items():
        by_day = dict(rows)
        last_real.append(rows[-1][0])
        carried: list[date] = []
        aligned: list[tuple[date, float]] = []
        current: float | None = None

        for day in axis:
            value = by_day.get(day)
            if value is not None:
                current = value
            elif current is not None:
                carried.append(day)
            else:
                # 区间起点之前没有数据，前面这几天无法填充——整段跳过，
                # 不能假装它是某个值。
                continue
            aligned.append((day, current))

        if carried:
            filled[code] = carried
        series[code] = aligned

        first_day = aligned[0][0] if aligned else None
        # 必须与**请求的区间起点**比较，不能与 axis[0] 比较：axis 是所有标的日期的
        # 并集，若只有一个标的，axis[0] 就是它自己的首日，这个判断永远不会触发。
        if first_day is not None and first_day > start:
            limitations.append(
                f"{code} 起点 {first_day} 晚于区间起点 {start}，之前无数据可填充"
            )

    # 整组数据的真实截止日 = 各标的最后一个真实净值的较早者。
    as_of = min(last_real) if last_real else None

    if filled:
        total = sum(len(days) for days in filled.values())
        limitations.append(f"{total} 个标的-交易日无净值，已按前向填充处理")
    if missing:
        limitations.append(f"以下标的在区间内没有净值，已排除: {', '.join(sorted(missing))}")
    if as_of is not None and as_of < end:
        limitations.append(f"净值截至 {as_of}，晚于该日的区间没有数据")

    return NavSeries(
        dates=axis,
        series=series,
        filled=filled,
        missing=sorted(missing),
        as_of=as_of,
        complete=not filled and not missing and as_of is not None and as_of >= end,
        limitations=limitations,
    )


__all__ = ["NavSeries", "NavSeriesError", "build_nav_series"]
