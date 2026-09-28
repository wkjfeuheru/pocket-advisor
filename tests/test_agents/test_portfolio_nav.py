"""日频净值服务的覆盖语义、查询与合成数据。

内存假存储刻意实现与 PostgreSQL 相同的覆盖规则（真数据压过合成、合成不能覆盖真
数据、清除合成不误删真数据），这样单元测试验证的是**服务的语义**；DDL 层面的
``ON CONFLICT DO UPDATE ... WHERE`` 另由集成测试覆盖。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import (
    REAL,
    SYNTHETIC,
    NavError,
    NavRow,
    NavService,
    generate_synthetic_nav,
    trading_days,
)


class _FakeNavStore:
    """实现 NavStore 协议，覆盖语义与 PostgreSQL 实现对齐。"""

    def __init__(self):
        self.rows: dict[tuple[str, date], NavRow] = {}

    def upsert_many(self, rows):
        written = 0
        for row in rows:
            key = (row.product_code, row.trade_date)
            existing = self.rows.get(key)
            # 合成值永远不能覆盖真数据。
            if existing is not None and existing.data_source == REAL and row.data_source == SYNTHETIC:
                continue
            self.rows[key] = row
            written += 1
        return written

    def history(self, product_code, start, end):
        found = [
            (day, row.nav)
            for (code, day), row in self.rows.items()
            if code == product_code and start <= day <= end
        ]
        return sorted(found)

    def latest(self, product_code):
        found = sorted(
            (day, row.nav) for (code, day), row in self.rows.items() if code == product_code
        )
        return found[-1] if found else None

    def delete_synthetic(self, product_code=None):
        doomed = [
            key
            for key, row in self.rows.items()
            if row.data_source == SYNTHETIC
            and (product_code is None or key[0] == product_code)
        ]
        for key in doomed:
            del self.rows[key]
        return len(doomed)


def _service():
    return NavService(_FakeNavStore())


def test_real_nav_overwrites_synthetic_on_the_same_day():
    service = _service()
    day = date(2026, 1, 5)

    service.upsert([NavRow("510300", day, 1.000, SYNTHETIC)])
    service.upsert([NavRow("510300", day, 9.876, REAL)])

    assert service.history("510300", day, day) == [(day, 9.876)]


def test_synthetic_nav_never_overwrites_real_on_the_same_day():
    service = _service()
    day = date(2026, 1, 5)

    service.upsert([NavRow("510300", day, 9.876, REAL)])
    service.upsert([NavRow("510300", day, 1.000, SYNTHETIC)])

    assert service.history("510300", day, day) == [(day, 9.876)]


def test_clearing_synthetic_keeps_real_rows_untouched():
    service = _service()
    real_day = date(2026, 1, 5)
    synth_day = date(2026, 1, 6)

    service.upsert(
        [
            NavRow("510300", real_day, 9.876, REAL),
            NavRow("510300", synth_day, 1.010, SYNTHETIC),
            NavRow("110011", synth_day, 2.000, SYNTHETIC),
        ]
    )

    removed = service.clear_synthetic()

    assert removed == 2
    assert service.history("510300", date(2026, 1, 1), date(2026, 1, 31)) == [(real_day, 9.876)]
    assert service.history("110011", date(2026, 1, 1), date(2026, 1, 31)) == []


def test_clearing_synthetic_can_be_scoped_to_one_product():
    service = _service()
    day = date(2026, 1, 5)
    service.upsert(
        [
            NavRow("510300", day, 1.0, SYNTHETIC),
            NavRow("110011", day, 2.0, SYNTHETIC),
        ]
    )

    assert service.clear_synthetic("510300") == 1
    assert service.latest("510300") is None
    assert service.latest("110011") == (day, 2.0)


def test_latest_returns_the_most_recent_trading_day_and_as_of_matches():
    service = _service()
    service.upsert(
        [
            NavRow("510300", date(2026, 1, 5), 1.00, REAL),
            NavRow("510300", date(2026, 1, 7), 1.03, REAL),
            NavRow("510300", date(2026, 1, 6), 1.01, REAL),
        ]
    )

    assert service.latest("510300") == (date(2026, 1, 7), 1.03)
    assert service.as_of("510300") == date(2026, 1, 7)
    assert service.as_of("999999") is None


def test_a_non_positive_or_non_finite_nav_is_rejected():
    service = _service()

    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(NavError) as excinfo:
            service.upsert([NavRow("510300", date(2026, 1, 5), bad, REAL)])
        assert excinfo.value.code == "bad_nav"


def test_synthetic_generation_is_reproducible_and_skips_weekends():
    service = _service()
    span = dict(start=date(2026, 1, 5), end=date(2026, 1, 9))  # 周一 ~ 周五

    first = generate_synthetic_nav(["510300", "110011"], seed=7, **span)
    second = generate_synthetic_nav(["510300", "110011"], seed=7, **span)

    assert first == second
    assert all(row.data_source == SYNTHETIC for row in first)

    days = {row.trade_date for row in first}
    assert days == {
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 9),
    }
    # 每只产品每天恰好一行。
    assert len(first) == 2 * 5

    # 起点净值固定为 1.0，之后按风险等级演化。
    opening = [row for row in first if row.trade_date == date(2026, 1, 5)]
    assert sorted(row.nav for row in opening) == [1.0, 1.0]


def test_a_riskier_product_gets_a_wider_synthetic_swing():
    rows = generate_synthetic_nav(
        ["LOW", "HIGH"],
        start=date(2026, 1, 5),
        end=date(2026, 12, 31),
        seed=11,
        risk_levels={"LOW": "R1", "HIGH": "R5"},
    )

    def spread(code):
        values = [row.nav for row in rows if row.product_code == code]
        return max(values) / min(values)

    assert spread("HIGH") > spread("LOW")


def test_trading_days_skips_saturday_and_sunday():
    days = trading_days(date(2026, 1, 9), date(2026, 1, 12))  # 周五 ~ 周一

    assert days == [date(2026, 1, 9), date(2026, 1, 12)]
