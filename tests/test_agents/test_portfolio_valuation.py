"""持仓估值：把账户 + 持仓 + 当日净值算成一份可直接展示的快照。

这是"实时展示用户持仓和 AI 账户的收益对比"的取数入口，用户线与 AI 线共用同一个函数，
两条线口径完全一致才谈得上可比。纯函数，不碰数据库。

═══════════════════════════════════════════════════════════════════════════
derivation —— 两个标的 + 现金 + 一个不可定价持仓
═══════════════════════════════════════════════════════════════════════════
持仓：
    A  200 份 × 净值 1.50 = 300.00   成本 250.00  → 浮盈 +50.00
    B  100 份 × 净值 2.00 = 200.00   成本 250.00  → 浮亏 -50.00
    WM 不可定价，成本 100.00 → 按成本计 100.00（浮盈 0）

    市值 = 300 + 200 + 100 = 600.00
    总资产 = 市值 600.00 + 现金 400.00 = 1000.00
    总成本 = 250 + 250 + 100 = 600.00
    总浮盈 = 600.00 - 600.00 = 0.00
    已实现盈亏 = 120.00（由调用方传入，来自历史成交）

单位净值：份额 1000 份 → unit_nav = 总资产 / 份额 = 1000.00 / 1000 = 1.000000
    注意分母是**份额**，不是总成本，也不是市值。

权重（用于展示集中度）：按市值占比，合计含现金为 1。
    A 300/1000 = 0.30   B 200/1000 = 0.20   WM 100/1000 = 0.10   现金 400/1000 = 0.40

不可定价持仓必须进 ``limitations``——页面要显示"这部分没算进收益"，
否则用户会以为自己的理财产品凭空消失了。

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.valuation import (
    PositionValuation,
    ValuationError,
    value_portfolio,
)

AS_OF = date(2026, 1, 5)

NAVS = {"110011": 1.50, "510300": 2.00}

POSITIONS = [
    {"product_code": "110011", "shares": 200.0, "cost_amount": 250.0, "priceable": True},
    {"product_code": "510300", "shares": 100.0, "cost_amount": 250.0, "priceable": True},
    {"product_code": "WM2026001", "shares": 100.0, "cost_amount": 100.0, "priceable": False},
]


def test_valuation_reproduces_the_hand_derived_totals():
    result = value_portfolio(
        positions=POSITIONS,
        cash_balance=400.0,
        navs=NAVS,
        total_units=1000.0,
        realized_pnl=120.0,
        nav_as_of=AS_OF,
    )

    by_code = {item.product_code: item for item in result.positions}

    assert by_code["110011"].market_value == pytest.approx(300.0)
    assert by_code["110011"].unrealized_pnl == pytest.approx(50.0)
    assert by_code["510300"].market_value == pytest.approx(200.0)
    assert by_code["510300"].unrealized_pnl == pytest.approx(-50.0)

    assert result.market_value == pytest.approx(600.0)
    assert result.total_assets == pytest.approx(1000.0)
    assert result.total_cost == pytest.approx(600.0)
    assert result.unrealized_pnl == pytest.approx(0.0)
    assert result.realized_pnl == pytest.approx(120.0)
    assert result.total_pnl == pytest.approx(120.0)
    assert result.unit_nav == pytest.approx(1.0)
    assert result.nav_as_of == AS_OF


def test_weights_are_market_value_shares_including_cash():
    result = value_portfolio(
        positions=POSITIONS,
        cash_balance=400.0,
        navs=NAVS,
        total_units=1000.0,
        nav_as_of=AS_OF,
    )

    weights = {item.product_code: item.weight for item in result.positions}
    assert weights["110011"] == pytest.approx(0.30)
    assert weights["510300"] == pytest.approx(0.20)
    assert weights["WM2026001"] == pytest.approx(0.10)
    assert result.cash_weight == pytest.approx(0.40)
    assert sum(weights.values()) + result.cash_weight == pytest.approx(1.0)


def test_an_unpriceable_holding_is_carried_at_cost_and_flagged():
    result = value_portfolio(
        positions=POSITIONS,
        cash_balance=400.0,
        navs=NAVS,
        total_units=1000.0,
        nav_as_of=AS_OF,
    )

    wm = next(item for item in result.positions if item.product_code == "WM2026001")
    assert wm.priced is False
    assert wm.market_value == pytest.approx(100.0)
    # 没有净值就不该报浮盈
    assert wm.unrealized_pnl == pytest.approx(0.0)
    assert any("WM2026001" in note for note in result.limitations)
    assert result.pricing_complete is False


def test_a_version_conflict_reports_no_units_rather_than_a_wrong_nav():
    """份额缺失时不能拿"总成本"凑一个 unit_nav——那会显示成一个假的收益率。"""
    result = value_portfolio(
        positions=POSITIONS,
        cash_balance=400.0,
        navs=NAVS,
        total_units=0.0,
        nav_as_of=AS_OF,
    )

    assert result.unit_nav is None
    assert any("份额" in note for note in result.limitations)


def test_a_missing_nav_for_a_priceable_holding_raises():
    with pytest.raises(ValuationError) as excinfo:
        value_portfolio(
            positions=[
                {"product_code": "110011", "shares": 200.0, "cost_amount": 250.0, "priceable": True}
            ],
            cash_balance=0.0,
            navs={},
            total_units=1000.0,
            nav_as_of=AS_OF,
        )

    assert excinfo.value.code == "missing_nav"
    assert "110011" in str(excinfo.value)
