"""对比曲线：从每日快照读出用户线与 AI 线。

前端对比页要的是两条同口径的归一净值序列。这两条线都存在
``ai_portfolio_snapshots`` 里（用 ``owner`` 区分），所以读取逻辑只有一份。

三条约定：

- **缺快照的日子不补零**：没有记录就是没有记录（对应"数据不足"），编一个 0 或 1
  会让曲线在起点处看起来像暴跌。
- **两条线各自按自己的日期升序**返回，不强行对齐——对齐是图表层的事，且用户线本来
  就可能比 AI 线长（AI 账户是采纳后才建立的）。
- **``as_of`` 取两条线里较早的截止日**，因为整组对比只能信到最旧的那条。

═══════════════════════════════════════════════════════════════════════════
derivation —— 曲线读取
═══════════════════════════════════════════════════════════════════════════
快照（customer=U1）：
    owner=user 2026-09-25 unit_nav=1.0000
    owner=user 2026-09-26 unit_nav=1.0120
    owner=user 2026-09-29 unit_nav=0.9980
    owner=ai   2026-09-26 unit_nav=1.0000
    owner=ai   2026-09-29 unit_nav=0.9972

    用户线 = 3 个点，AI 线 = 2 个点
    as_of = min(2026-09-29, 2026-09-29) = 2026-09-29
    若 AI 线只到 2026-09-26 → as_of = 2026-09-26（提示数据其实更旧）

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.curve_store import (
    CurveError,
    build_comparison_curves,
)

D = date


class _FakeSnapshotStore:
    def __init__(self, rows):
        # rows: list[(owner, snapshot_date, unit_nav)]
        self.rows = list(rows)

    def list_snapshots(self, customer_id, owner, *, start=None, end=None):
        return sorted(
            (day, nav)
            for (row_owner, day, nav) in self.rows
            if row_owner == owner
            and (start is None or day >= start)
            and (end is None or day <= end)
        )


ROWS = [
    ("user", D(2026, 9, 25), 1.0000),
    ("user", D(2026, 9, 26), 1.0120),
    ("user", D(2026, 9, 29), 0.9980),
    ("ai", D(2026, 9, 26), 1.0000),
    ("ai", D(2026, 9, 29), 0.9972),
]


def _build(**overrides):
    kwargs = {"store": _FakeSnapshotStore(ROWS), "customer_id": "U1"}
    kwargs.update(overrides)
    return build_comparison_curves(**kwargs)


def test_both_curves_are_returned_under_their_own_dates():
    result = _build()

    assert result.user_curve == [
        {"date": "2026-09-25", "unit_nav": 1.0000},
        {"date": "2026-09-26", "unit_nav": 1.0120},
        {"date": "2026-09-29", "unit_nav": 0.9980},
    ]
    assert result.ai_curve == [
        {"date": "2026-09-26", "unit_nav": 1.0000},
        {"date": "2026-09-29", "unit_nav": 0.9972},
    ]


def test_as_of_is_the_earlier_of_the_two_curves_ends():
    store = _FakeSnapshotStore(
        [
            ("user", D(2026, 9, 25), 1.0),
            ("user", D(2026, 9, 29), 1.1),
            ("ai", D(2026, 9, 26), 1.0),
        ]
    )

    result = _build(store=store)

    assert result.as_of == D(2026, 9, 26)
    assert any("2026-09-26" in note for note in result.limitations)


def test_a_missing_curve_is_empty_not_fabricated():
    """只有用户线、还没有 AI 线时：AI 线为空，不能补一个 1.0 出来。"""
    store = _FakeSnapshotStore([("user", D(2026, 9, 29), 1.0)])

    result = _build(store=store)

    assert result.ai_curve == []
    assert len(result.user_curve) == 1
    assert result.as_of == D(2026, 9, 29)
    assert any("AI" in note for note in result.limitations)


def test_no_snapshots_at_all_yields_empty_curves_and_no_as_of():
    result = _build(store=_FakeSnapshotStore([]))

    assert result.user_curve == []
    assert result.ai_curve == []
    assert result.as_of is None
    assert result.limitations


def test_the_date_window_is_passed_through_to_the_store():
    store = _FakeSnapshotStore(ROWS)

    result = _build(store=store, start=D(2026, 9, 26), end=D(2026, 9, 26))

    assert result.user_curve == [{"date": "2026-09-26", "unit_nav": 1.0120}]
    assert result.ai_curve == [{"date": "2026-09-26", "unit_nav": 1.0000}]


def test_a_bad_window_is_rejected():
    with pytest.raises(CurveError) as excinfo:
        _build(start=D(2026, 9, 29), end=D(2026, 9, 25))

    assert excinfo.value.code == "bad_range"


def test_a_non_finite_nav_is_skipped_rather_than_breaking_the_chart():
    """坏数据不应让整张图崩掉：跳过该点并记进 limitations。"""
    store = _FakeSnapshotStore(
        [
            ("user", D(2026, 9, 25), 1.0),
            ("user", D(2026, 9, 26), float("nan")),
            ("user", D(2026, 9, 29), 1.02),
        ]
    )

    result = _build(store=store)

    assert [p["date"] for p in result.user_curve] == ["2026-09-25", "2026-09-29"]
    assert any("跳过" in note for note in result.limitations)


def test_the_limit_caps_the_number_of_points_returned():
    """长区间只取最近 N 个点，避免一次拉回几百天。"""
    many = [("user", D(2026, 1, 1) + __import__("datetime").timedelta(days=i), 1.0 + i / 100) for i in range(300)]

    result = _build(store=_FakeSnapshotStore(many), limit=50)

    assert len(result.user_curve) == 50
    # 取的是**最近**的 50 个
    assert result.user_curve[-1]["date"] == "2026-10-27"
