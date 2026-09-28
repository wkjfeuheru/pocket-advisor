"""每日快照写入。

曲线是**写出来的**，不是查询时算出来的。每天一条快照的好处：历史不可篡改
（行情回补不会改写已画出的历史）、读取便宜（对比页只做一次范围查询）、
两线同源（用户线与 AI 线共用一张表与一套口径，天然可比）。

幂等：``(customer_id, owner, snapshot_date)`` 是主键，同一天重跑覆盖。

═══════════════════════════════════════════════════════════════════════════
derivation
═══════════════════════════════════════════════════════════════════════════
    上一条快照 unit_nav = 1.0000，今天总资产 997.25、份额 1000
        今天的 unit_nav = 997.25 / 1000 = 0.99725
        daily_return    = 0.99725 / 1.0000 - 1 = -0.00275

    没有份额（total_units <= 0）→ 跳过，**不写 1.0**
    两条线的单位净值都以各自 1.0 为起点，因此可以直接叠在同一张图上。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.snapshot import (
    SnapshotError,
    write_both_curves,
    write_snapshot,
)

DAY = date(2026, 9, 28)


class _FakeWriter:
    def __init__(self, rows=None):
        # rows: dict[(owner, date)] = record
        self.rows: dict[tuple[str, date], dict] = dict(rows or {})
        self.calls: list[tuple[str, str, dict]] = []

    def latest_snapshot(self, customer_id, owner):
        found = sorted(
            ((day, rec) for (o, day), rec in self.rows.items() if o == owner),
            key=lambda item: item[0],
        )
        return dict(found[-1][1]) if found else None

    def upsert_snapshot(self, customer_id, owner, record):
        self.calls.append((customer_id, owner, dict(record)))
        self.rows[(owner, record["snapshot_date"])] = dict(record)


def test_writing_a_snapshot_computes_unit_nav_and_daily_return():
    writer = _FakeWriter({("ai", date(2026, 9, 25)): {"unit_nav": 1.0}})

    outcome = write_snapshot(
        writer,
        customer_id="U1",
        owner="ai",
        snapshot_date=DAY,
        total_assets=997.25,
        cash_balance=97.25,
        market_value=900.0,
        total_units=1000.0,
    )

    assert outcome.written is True
    assert outcome.unit_nav == pytest.approx(0.99725)
    assert outcome.daily_return == pytest.approx(-0.00275, abs=1e-9)

    record = writer.rows[("ai", DAY)]
    assert record["unit_nav"] == pytest.approx(0.99725)
    assert record["total_assets"] == pytest.approx(997.25)
    assert record["market_value"] == pytest.approx(900.0)


def test_the_first_snapshot_has_no_daily_return():
    writer = _FakeWriter()

    outcome = write_snapshot(
        writer,
        customer_id="U1",
        owner="user",
        snapshot_date=DAY,
        total_assets=1000.0,
        cash_balance=0.0,
        market_value=1000.0,
        total_units=1000.0,
    )

    assert outcome.written is True
    assert outcome.unit_nav == pytest.approx(1.0)
    assert outcome.daily_return is None


def test_missing_units_skips_instead_of_inventing_a_nav():
    """没有份额就算不出单位净值——宁可写不进去，也不要编一个 1.0。"""
    writer = _FakeWriter()

    outcome = write_snapshot(
        writer,
        customer_id="U1",
        owner="ai",
        snapshot_date=DAY,
        total_assets=1000.0,
        cash_balance=0.0,
        market_value=1000.0,
        total_units=0.0,
    )

    assert outcome.written is False
    assert outcome.skipped_reason == "no_units"
    assert writer.calls == []


def test_rewriting_the_same_day_overwrites_rather_than_duplicating():
    writer = _FakeWriter()

    write_snapshot(
        writer, customer_id="U1", owner="ai", snapshot_date=DAY,
        total_assets=1000.0, cash_balance=0.0, market_value=1000.0, total_units=1000.0,
    )
    write_snapshot(
        writer, customer_id="U1", owner="ai", snapshot_date=DAY,
        total_assets=990.0, cash_balance=0.0, market_value=990.0, total_units=1000.0,
    )

    assert len(writer.rows) == 1
    assert writer.rows[("ai", DAY)]["unit_nav"] == pytest.approx(0.99)


def test_an_unknown_owner_is_rejected():
    writer = _FakeWriter()

    with pytest.raises(SnapshotError) as excinfo:
        write_snapshot(
            writer, customer_id="U1", owner="robot", snapshot_date=DAY,
            total_assets=1000.0, cash_balance=0.0, market_value=1000.0, total_units=1000.0,
        )

    assert excinfo.value.code == "bad_owner"


def test_a_non_finite_total_is_rejected():
    writer = _FakeWriter()

    with pytest.raises(SnapshotError) as excinfo:
        write_snapshot(
            writer, customer_id="U1", owner="ai", snapshot_date=DAY,
            total_assets=float("nan"), cash_balance=0.0, market_value=0.0, total_units=1000.0,
        )

    assert excinfo.value.code == "bad_assets"


def test_writing_both_curves_skips_only_the_missing_side():
    writer = _FakeWriter()

    outcomes = write_both_curves(
        writer,
        customer_id="U1",
        snapshot_date=DAY,
        user={"total_assets": 1000.0, "market_value": 1000.0, "total_units": 1000.0},
        # AI 账户还没建立
        ai=None,
    )

    by_owner = {item.owner: item for item in outcomes}
    assert by_owner["user"].written is True
    assert by_owner["ai"].written is False
    assert by_owner["ai"].skipped_reason == "no_data"


def test_writing_both_curves_records_limitations_and_nav_as_of():
    writer = _FakeWriter()

    write_both_curves(
        writer,
        customer_id="U1",
        snapshot_date=DAY,
        user={
            "total_assets": 1000.0,
            "market_value": 900.0,
            "cash_balance": 100.0,
            "total_units": 1000.0,
            "limitations": ["WM2026001 无日频净值，按成本计价"],
        },
        ai={
            "total_assets": 998.0,
            "market_value": 900.0,
            "cash_balance": 98.0,
            "total_units": 1000.0,
        },
        nav_as_of=date(2026, 9, 26),
    )

    assert writer.rows[("user", DAY)]["limitations"] == ["WM2026001 无日频净值，按成本计价"]
    assert writer.rows[("user", DAY)]["nav_as_of"] == date(2026, 9, 26)
    assert writer.rows[("ai", DAY)]["limitations"] == []
