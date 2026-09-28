"""净值序列读取器：把存储里的日频净值变成引擎能直接吃的形状。

回测（``replay_weights``）和用户曲线（``rebuild_user_curve``）都要
``{code: [(date, nav), ...]}``。这是存储与计算之间唯一的接缝，因此口径必须在这里
一次定清楚：

- **交易日轴**取范围内全部标的的日期并集，而不是各自的日期。两只产品缺不同天时，
  对齐到并集才能同图比较。
- **前向填充**：某标的某天没有净值时沿用最近一个有效值，并记进 ``filled``。
  不填充会让整条曲线在缺一天时直接报错——而漏导一天在运维里是常态。
- **填充不得凭空造收益**：沿用昨日净值意味着当天涨跌幅为 0，而不是把它当成
  缺失然后跳过（跳过会让相邻两天被当成一天，收益率被压缩）。
- **``as_of`` 是数据截止日**，不是查询日。页面必须显示它，否则用户以为看的是实时。

═══════════════════════════════════════════════════════════════════════════
derivation —— 对齐与填充
═══════════════════════════════════════════════════════════════════════════
A: 01-05=1.00, 01-06=1.02,            01-08=1.05
B: 01-05=2.00,             01-07=2.10, 01-08=2.20

日期并集 = [01-05, 01-06, 01-07, 01-08]

A 填充：01-07 无值 → 沿用 01-06 的 1.02
        A = [1.00, 1.02, 1.02, 1.05]   填充 1 天
B 填充：01-06 无值 → 沿用 01-05 的 2.00
        B = [2.00, 2.00, 2.10, 2.20]   填充 1 天

as_of = 各标的最后一个**真实**净值的较早者 = min(01-08, 01-08) = 01-08
        （若 A 只到 01-06，则 as_of = 01-06，提示数据其实更旧）

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import NavRow, NavService
from app.services.portfolio_advisor.nav_series import (
    NavSeriesError,
    build_nav_series,
)


class _FakeNavStore:
    def __init__(self, rows):
        self.rows = {(row.product_code, row.trade_date): row for row in rows}

    def upsert_many(self, rows):
        for row in rows:
            self.rows[(row.product_code, row.trade_date)] = row
        return len(rows)

    def history(self, product_code, start, end):
        return sorted(
            (day, row.nav)
            for (code, day), row in self.rows.items()
            if code == product_code and start <= day <= end
        )

    def latest(self, product_code):
        found = sorted(
            (day, row.nav) for (code, day), row in self.rows.items() if code == product_code
        )
        return found[-1] if found else None

    def delete_synthetic(self, product_code=None):
        return 0


D = date


def _store():
    return _FakeNavStore(
        [
            NavRow("A", D(2026, 1, 5), 1.00),
            NavRow("A", D(2026, 1, 6), 1.02),
            NavRow("A", D(2026, 1, 8), 1.05),
            NavRow("B", D(2026, 1, 5), 2.00),
            NavRow("B", D(2026, 1, 7), 2.10),
            NavRow("B", D(2026, 1, 8), 2.20),
        ]
    )


def test_series_aligns_to_the_union_of_dates_and_forward_fills_gaps():
    series = build_nav_series(
        _store(), product_codes=["A", "B"], start=D(2026, 1, 5), end=D(2026, 1, 8)
    )

    assert series.dates == [D(2026, 1, 5), D(2026, 1, 6), D(2026, 1, 7), D(2026, 1, 8)]
    assert series.series["A"] == [
        (D(2026, 1, 5), 1.00),
        (D(2026, 1, 6), 1.02),
        (D(2026, 1, 7), 1.02),  # 沿用 01-06
        (D(2026, 1, 8), 1.05),
    ]
    assert series.series["B"] == [
        (D(2026, 1, 5), 2.00),
        (D(2026, 1, 6), 2.00),  # 沿用 01-05
        (D(2026, 1, 7), 2.10),
        (D(2026, 1, 8), 2.20),
    ]


def test_filled_days_are_reported_so_the_page_can_say_so():
    series = build_nav_series(
        _store(), product_codes=["A", "B"], start=D(2026, 1, 5), end=D(2026, 1, 8)
    )

    assert series.filled["A"] == [D(2026, 1, 7)]
    assert series.filled["B"] == [D(2026, 1, 6)]
    assert series.complete is False
    assert any("填充" in note for note in series.limitations)


def test_as_of_is_the_oldest_last_real_observation():
    store = _FakeNavStore(
        [
            NavRow("A", D(2026, 1, 5), 1.0),
            NavRow("A", D(2026, 1, 6), 1.1),
            NavRow("B", D(2026, 1, 5), 2.0),
        ]
    )

    series = build_nav_series(
        store, product_codes=["A", "B"], start=D(2026, 1, 5), end=D(2026, 1, 8)
    )

    # A 到 01-06，B 到 01-05 → 整组数据的真实截止是 01-05
    assert series.as_of == D(2026, 1, 5)
    assert any("截至" in note for note in series.limitations)


def test_a_product_with_no_data_in_range_is_reported_not_silently_dropped():
    store = _FakeNavStore([NavRow("A", D(2026, 1, 5), 1.0)])

    series = build_nav_series(
        store, product_codes=["A", "MISSING"], start=D(2026, 1, 5), end=D(2026, 1, 8)
    )

    assert "MISSING" not in series.series
    assert "MISSING" in series.missing
    assert any("MISSING" in note for note in series.limitations)


def test_a_product_whose_first_day_is_after_start_is_marked():
    """区间起点之前没有数据时，前几天无法填充——必须说明，不能假装是 1.0。

    注意日期轴由**有数据的日期**构成，因此单个标的只到 01-07 时不会有 01-08 这一行
    （没有下一个观测值可以往前填）。尾部缺口由 ``as_of`` 表达。
    """
    store = _FakeNavStore([NavRow("A", D(2026, 1, 7), 1.0)])

    series = build_nav_series(
        store, product_codes=["A"], start=D(2026, 1, 5), end=D(2026, 1, 8)
    )

    assert series.series["A"] == [(D(2026, 1, 7), 1.00)]
    assert series.as_of == D(2026, 1, 7)
    assert any("起点" in note for note in series.limitations)
    assert any("截至" in note for note in series.limitations)


def test_an_empty_range_raises():
    with pytest.raises(NavSeriesError) as excinfo:
        build_nav_series(
            _FakeNavStore([]), product_codes=["A"], start=D(2026, 1, 5), end=D(2026, 1, 8)
        )

    assert excinfo.value.code == "no_data"


def test_an_empty_product_list_is_rejected():
    """调用方必须明确要哪些标的——不去读整张净值表。"""
    with pytest.raises(NavSeriesError) as excinfo:
        build_nav_series(
            _store(), product_codes=[], start=D(2026, 1, 5), end=D(2026, 1, 8)
        )

    assert excinfo.value.code == "no_products"
