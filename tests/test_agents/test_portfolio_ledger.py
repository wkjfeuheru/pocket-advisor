"""AI 账本：以用户持仓建仓、差额成交、单位净值、现金流同步与幂等。

这个模块承载设计文档里最关键的不变量：

**AI 账本永不写用户数据。** 采纳方案只在 ``ai_*`` 表里产生成交与持仓，
``finance.positions`` / ``finance.orders`` / ``finance.cash_transactions`` 一行都不动。
用户持仓是事实，AI 账本是反事实。

═══════════════════════════════════════════════════════════════════════════
derivation —— 建仓成交额与现金流份额
═══════════════════════════════════════════════════════════════════════════
账户建立：initial_capital = 用户当时总资产；total_units = initial_capital；
          unit_nav = 1.0（定义上如此）。账户建立**不产生成交**——AI 账户的起点就是
          用户当时的真实持仓，这正是"存量调仓"口径：对比问的是"听了 AI 会不会更好"，
          而不是"从零重来会怎样"。

采纳方案：目标金额 = 采纳前总资产 × 目标权重（按**采纳前**总资产计，不含费用），
          成交额 = 目标金额 - 当前市值。
          因此成交后实际权重会略低于目标——因为买卖费用吃掉了现金。
          这不是 bug，是真实摩擦；收益对比里必须体现。

现金流：入金 → 按当日 unit_nav 申购，total_units += 入金 / unit_nav，
        不改变已有仓位比例；出金 → 按比例赎回。
        TWR/单位净值口径下，入金本身不产生收益。

    例：总资产 1000、total_units 1000、unit_nav 1.0
        入金 500 → 申购 500/1.0 = 500 份 → total_units 1500，unit_nav 仍为 1.0
        （现金多了 500，分母也多了 500 份，净值不变——入金不是收益）

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.ledger import (
    LedgerError,
    LedgerService,
)

CLASSES = ("fund", "etf", "wealth")


class _FakeLedgerStore:
    """实现 LedgerStore 协议；幂等键唯一约束与 PostgreSQL 对齐。"""

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.positions: dict[tuple[str, str], dict] = {}
        self.trades: list[dict] = []
        self.plans: dict[str, dict] = {}

    def establish_account(self, customer_id, *, initial_capital, opening_plan_id, nav_as_of):
        self.accounts[customer_id] = {
            "customer_id": customer_id,
            "initial_capital": initial_capital,
            "opening_plan_id": opening_plan_id,
            "cash_balance": 0.0,
            "unit_nav": 1.0,
            "total_units": initial_capital,
            "cumulative_inflow": 0.0,
            "cumulative_outflow": 0.0,
            "nav_as_of": nav_as_of,
        }
        return dict(self.accounts[customer_id])

    def get_account(self, customer_id):
        row = self.accounts.get(customer_id)
        return dict(row) if row else None

    def update_account(self, customer_id, **fields):
        self.accounts[customer_id].update(fields)
        return dict(self.accounts[customer_id])

    def get_position(self, customer_id, product_code):
        row = self.positions.get((customer_id, product_code))
        return dict(row) if row else None

    def list_positions(self, customer_id):
        return [
            dict(row) for (cid, _), row in self.positions.items() if cid == customer_id
        ]

    def upsert_position(self, customer_id, product_code, *, shares, cost_amount):
        key = (customer_id, product_code)
        if shares <= 0:
            self.positions.pop(key, None)
            return
        self.positions[key] = {
            "customer_id": customer_id,
            "product_code": product_code,
            "shares": shares,
            "cost_amount": cost_amount,
            "avg_cost": cost_amount / shares if shares else 0.0,
        }

    def insert_trade(self, record):
        # UNIQUE (customer_id, trade_key)：单笔成交级幂等。
        trade_key = record.get("trade_key") or ""
        if trade_key and any(
            row["customer_id"] == record["customer_id"] and row.get("trade_key") == trade_key
            for row in self.trades
        ):
            from app.core.security import UniqueConstraintError

            raise UniqueConstraintError()
        self.trades.append(dict(record))
        return dict(record)

    def find_trade_by_key(self, customer_id, adoption_key):
        """按**采纳级**键查找。一次采纳的成交共用 adoption_key。"""
        if not adoption_key:
            return None
        for row in self.trades:
            if row["customer_id"] == customer_id and row.get("adoption_key") == adoption_key:
                return dict(row)
        return None

    def mark_plan_adopted(self, plan_id, *, adopted_at):
        self.plans[plan_id] = {"plan_id": plan_id, "status": "adopted", "adopted_at": adopted_at}

    def get_plan(self, plan_id):
        row = self.plans.get(plan_id)
        return dict(row) if row else None


@pytest.fixture
def store():
    return _FakeLedgerStore()


@pytest.fixture
def service(store):
    return LedgerService(store)


DAYS = {  # 采纳日净值
    "110011": 1.00,
    "510300": 2.00,
}
NAV_AS_OF = date(2026, 1, 5)

# 用户当前持仓：总资产 1000 = A 800 + B 200
CURRENT_HOLDINGS = {
    "110011": {"market_value": 800.0, "shares": 800.0, "priceable": True},
    "510300": {"market_value": 200.0, "shares": 100.0, "priceable": True},
}


def _establish(service):
    return service.establish(
        customer_id="U1",
        initial_capital=1000.0,
        holdings_summary={"110011": 800.0, "510300": 200.0},
        nav_as_of=NAV_AS_OF,
        navs=DAYS,
    )


def test_establishing_the_account_mirrors_the_user_holdings_without_any_trade(service, store):
    # AI 账户的起点就是用户当时的真实持仓，因此不应产生成交。
    _establish(service)

    assert store.trades == []
    positions = {row["product_code"]: row for row in store.list_positions("U1")}
    assert positions["110011"]["shares"] == pytest.approx(800.0)
    assert positions["510300"]["shares"] == pytest.approx(100.0)

    account = store.get_account("U1")
    assert account["unit_nav"] == pytest.approx(1.0)
    assert account["total_units"] == pytest.approx(1000.0)


def test_adopting_a_plan_records_only_the_delta_trades(service, store):
    _establish(service)

    # 目标：A 50% / B 45% / cash 5%，总资产 1000
    #   A 目标 500，当前 800 → 卖出 300 元（净值 1.00 → 300 份）
    #   B 目标 450，当前 200 → 买入 250 元（净值 2.00 → 125 份）
    result = service.adopt_plan(
        customer_id="U1",
        plan_id="plan-1",
        weights={"110011": 0.50, "510300": 0.45, "cash": 0.05},
        holdings=CURRENT_HOLDINGS,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
        fee_buy=0.005,
        fee_sell=0.005,
    )

    trades = {(t["product_code"], t["side"]): t for t in store.trades}
    assert set(trades) == {("110011", "sell"), ("510300", "buy")}
    assert trades[("110011", "sell")]["gross_amount"] == pytest.approx(300.0)
    assert trades[("110011", "sell")]["shares"] == pytest.approx(300.0)
    assert trades[("510300", "buy")]["gross_amount"] == pytest.approx(250.0)
    assert trades[("510300", "buy")]["shares"] == pytest.approx(125.0)

    # 卖出费率 0.5%、买入费率 0.5%（本用例显式传参，与默认值不同）
    #   卖出 300 → 费 1.50，现金 +298.50
    #   买入 250 → 费 1.25，现金 -251.25
    #   现金 = 298.50 - 251.25 = 47.25；总费用 = 2.75
    assert result.cash_balance == pytest.approx(47.25)
    assert result.total_fee == pytest.approx(2.75)

    # 持仓按方案调整完毕：A 800-300=500 份，B 100+125=225 份
    positions = {row["product_code"]: row["shares"] for row in store.list_positions("U1")}
    assert positions["110011"] == pytest.approx(500.0)
    assert positions["510300"] == pytest.approx(225.0)

    # 单位净值只能因费用下降，不能因为"调仓"本身凭空上涨。
    # 采纳后总资产 = 500×1.00 + 225×2.00 + 47.25 = 997.25
    # 份额是初始本金的固定换算，全程 1000 份不变
    # unit_nav = 997.25 / 1000 = 0.99725，跌幅正好等于费用率 2.75/1000
    assert result.total_assets == pytest.approx(997.25)
    assert result.total_units == pytest.approx(1000.0)
    assert result.unit_nav == pytest.approx(0.99725, abs=1e-9)
    assert result.unit_nav < 1.0


def test_adopting_a_plan_does_not_create_a_phantom_gain(service, store):
    """调仓前后市值口径必须连续：卖 300 买 250 不该让账户凭空涨 9%。

    这是 AI 账户最容易出的错——若 total_units 沿用采纳前资产值，而持仓按成交后的
    股数计价，单位净值会跳升。真实市场没涨，钱却多了，那整条对比曲线就废了。
    """
    _establish(service)

    before = store.get_account("U1")
    result = service.adopt_plan(
        customer_id="U1",
        plan_id="plan-1",
        weights={"110011": 0.50, "510300": 0.45, "cash": 0.05},
        holdings=CURRENT_HOLDINGS,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
    )

    # 净值变化只能来自费用，量级是千分之几，绝不是百分之几。
    assert abs(result.unit_nav - before["unit_nav"]) < 0.01


def test_adopting_the_same_plan_twice_with_the_same_request_id_is_idempotent(service, store):
    """同一个 request_id 重试（前端超时重发）必须幂等：不产生第二笔成交。

    "重放"与"重复"是两回事：带同一个显式幂等键 = 同一请求重试 → 重放；
    不带键或换键再采同一方案 = 重复/并发采纳 → 拒绝（见下一个用例）。
    """
    _establish(service)
    kwargs = dict(
        customer_id="U1",
        plan_id="plan-1",
        weights={"110011": 0.50, "510300": 0.45, "cash": 0.05},
        holdings=CURRENT_HOLDINGS,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
        idempotency_key="req-abc",
    )

    first = service.adopt_plan(**kwargs)
    trades_after_first = len(store.trades)
    second = service.adopt_plan(**kwargs)

    assert first.replayed is False
    assert len(store.trades) == trades_after_first
    assert second.replayed is True
    assert second.cash_balance == pytest.approx(first.cash_balance)


def test_adopting_a_plan_twice_under_a_different_key_is_rejected(service, store):
    _establish(service)
    kwargs = dict(
        customer_id="U1",
        plan_id="plan-1",
        weights={"110011": 0.50, "510300": 0.45, "cash": 0.05},
        holdings=CURRENT_HOLDINGS,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
    )
    service.adopt_plan(**kwargs)

    # 同一个 plan_id 换个幂等键再来一次：必须是并发/重复采纳，直接拒绝。
    with pytest.raises(LedgerError) as excinfo:
        service.adopt_plan(**{**kwargs, "idempotency_key": "another-key"})

    assert excinfo.value.code == "already_adopted"


def test_missing_nav_aborts_the_whole_adoption_instead_of_partially_applying(service, store):
    """缺净值必须**在动任何一笔之前**就报错。

    早期实现在循环里边算边成交：110011 先卖出成功，轮到缺净值的标的才抛错，
    结果留下一个"部分采纳"的组合——调用方只看到异常，以为什么都没发生，
    但账本已经被改了一半。这是最难排查的一类状态损坏。
    """
    _establish(service)
    holdings = dict(CURRENT_HOLDINGS)
    holdings["999999"] = {"market_value": 100.0, "shares": 100.0, "priceable": True}

    with pytest.raises(LedgerError) as excinfo:
        service.adopt_plan(
            customer_id="U1",
            plan_id="plan-1",
            weights={"110011": 0.20, "999999": 0.70, "cash": 0.10},
            holdings=holdings,
            # 故意不给 999999 的净值
            navs=DAYS,
            nav_as_of=NAV_AS_OF,
        )

    assert excinfo.value.code == "missing_nav"
    # 关键：一笔成交都不能留下
    assert store.trades == []
    positions = {row["product_code"]: row["shares"] for row in store.list_positions("U1")}
    assert positions == {"110011": 800.0, "510300": 100.0}
    assert store.get_account("U1")["cash_balance"] == pytest.approx(0.0)


def test_a_deposit_subscribes_units_without_creating_a_return(service, store):
    _establish(service)

    before = store.get_account("U1")
    result = service.apply_cash_flow(
        customer_id="U1", kind="deposit", amount=500.0, nav_as_of=NAV_AS_OF
    )

    # 入金 500，unit_nav 仍为 1.0 → 申购 500 份
    assert result.total_units == pytest.approx(before["total_units"] + 500.0)
    assert result.unit_nav == pytest.approx(1.0)
    assert result.cash_balance == pytest.approx(500.0)
    # 入金不是收益：单位净值不变
    assert result.unit_nav == pytest.approx(before["unit_nav"])


def test_an_unpriceable_holding_is_carried_over_but_never_traded(service, store):
    holdings = dict(CURRENT_HOLDINGS)
    holdings["WM2026001"] = {"market_value": 100.0, "shares": 100.0, "priceable": False}
    service.establish(
        customer_id="U1",
        initial_capital=1100.0,
        holdings_summary={"110011": 800.0, "510300": 200.0, "WM2026001": 100.0},
        nav_as_of=NAV_AS_OF,
        navs=DAYS,
    )

    service.adopt_plan(
        customer_id="U1",
        plan_id="plan-1",
        # 不可定价标的保持原值，不新增
        weights={"110011": 0.40, "510300": 0.40, "WM2026001": 0.09090909, "cash": 0.05},
        holdings=holdings,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
    )

    traded = {t["product_code"] for t in store.trades}
    assert "WM2026001" not in traded
    # 没有净值就不估值：按成本保留
    position = store.get_position("U1", "WM2026001")
    assert position["shares"] == pytest.approx(100.0)


def test_cash_flow_on_an_unknown_account_is_rejected(service):
    with pytest.raises(LedgerError) as excinfo:
        service.apply_cash_flow(
            customer_id="NOPE", kind="deposit", amount=100.0, nav_as_of=NAV_AS_OF
        )

    assert excinfo.value.code == "no_account"


def test_the_ledger_never_writes_user_tables(service, store):
    """最关键的不变量：采纳方案只动 ai_* 表。"""
    _establish(service)
    service.adopt_plan(
        customer_id="U1",
        plan_id="plan-1",
        weights={"110011": 0.30, "510300": 0.30, "cash": 0.40},
        holdings=CURRENT_HOLDINGS,
        navs=DAYS,
        nav_as_of=NAV_AS_OF,
    )

    # 假存储里根本没有 user positions/orders 的入口——这正是接口层的保证。
    assert not hasattr(store, "user_positions")
    assert set(type(store).__dict__) & {"insert_order", "upsert_user_position"} == set()
