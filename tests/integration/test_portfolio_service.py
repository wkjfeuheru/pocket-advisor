"""模拟交易服务层测试：费率、成交链路、幂等与账户口径。

用内存假存储替代 PostgreSQL（与仓库既有"手写 fake + monkeypatch"惯例一致），
因此不需要真实数据库即可验证资金与持仓计算。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from app.schemas.portfolio import AccountSnapshot
from app.services.portfolio_service import (
    PortfolioDeps,
    PortfolioError,
    PortfolioService,
    round_shares,
    subscription_by_amount,
)
from app.services.product_service import NavQuote, ProductDeps, ProductError, ProductService, to_rate


# ── 内存假存储 ────────────────────────────────────────────────────

class _FakeTransaction:
    """假事务：异常时回滚内存快照，与 PostgreSQL 事务语义对齐。"""

    def __init__(self, store: "_FakeStore"):
        self._store = store
        self._snapshot: dict | None = None

    def __enter__(self):
        self._snapshot = {
            "accounts": copy.deepcopy(self._store.accounts),
            "positions": copy.deepcopy(self._store.positions),
            "orders": copy.deepcopy(self._store.orders),
            "transactions": copy.deepcopy(self._store.transactions),
        }
        return self._store

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and self._snapshot is not None:
            self._store.accounts = self._snapshot["accounts"]
            self._store.positions = self._snapshot["positions"]
            self._store.orders = self._snapshot["orders"]
            self._store.transactions = self._snapshot["transactions"]
        return False


class _FakeStore:
    """实现 PostgresPortfolioStore 的公开接口，全部状态放内存。"""

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.positions: dict[tuple[str, str], dict] = {}
        self.orders: list[dict] = []
        self.transactions: list[dict] = []

    def ensure_account(self, customer_id):
        cid = customer_id.upper()
        if cid not in self.accounts:
            self.accounts[cid] = {
                "customer_id": cid, "cash_balance": 0.0, "frozen_balance": 0.0,
                "total_deposit": 0.0, "created_at": datetime.now(),
                "updated_at": datetime.now(),
            }
        return dict(self.accounts[cid])

    def lock_account(self, session, customer_id):
        return self.accounts.get(customer_id.upper())

    def transaction(self):
        return _FakeTransaction(self)

    def find_transaction_by_key(self, customer_id, idempotency_key):
        if not idempotency_key:
            return None
        for row in self.transactions:
            if row["customer_id"] == customer_id.upper() and row.get("idempotency_key") == idempotency_key:
                return dict(row)
        return None

    def find_order_by_key(self, customer_id, idempotency_key):
        if not idempotency_key:
            return None
        for row in self.orders:
            if row["customer_id"] == customer_id.upper() and row.get("idempotency_key") == idempotency_key:
                return dict(row)
        return None

    def insert_transaction(self, session, record):
        cid = str(record["customer_id"]).upper()
        key = record.get("idempotency_key") or ""
        if key:
            for row in self.transactions:
                if row["customer_id"] == cid and row.get("idempotency_key") == key:
                    from app.core.security import UniqueConstraintError
                    raise UniqueConstraintError()
        row = dict(record)
        row["customer_id"] = cid
        row["created_at"] = datetime.now()
        self.transactions.append(row)
        return dict(row)

    def insert_order(self, session, record):
        cid = str(record["customer_id"]).upper()
        key = record.get("idempotency_key") or ""
        if key:
            for row in self.orders:
                if row["customer_id"] == cid and row.get("idempotency_key") == key:
                    from app.core.security import UniqueConstraintError
                    raise UniqueConstraintError()
        row = dict(record)
        row["customer_id"] = cid
        row["created_at"] = datetime.now()
        row["fee_limitations"] = json.dumps(row.get("fee_limitations", []))
        self.orders.append(row)
        return dict(row)

    def update_cash_balance(self, session, customer_id, cash_balance):
        self.accounts[customer_id.upper()]["cash_balance"] = cash_balance
        self.accounts[customer_id.upper()]["updated_at"] = datetime.now()

    def add_deposit(self, session, customer_id, amount):
        acc = self.accounts[customer_id.upper()]
        acc["total_deposit"] = float(acc["total_deposit"] or 0) + amount

    def get_position_row(self, session, customer_id, product_code):
        row = self.positions.get((customer_id.upper(), product_code))
        return dict(row) if row else None

    def upsert_position(self, session, customer_id, product_code, shares, cost_amount):
        key = (customer_id.upper(), product_code)
        if shares <= 0:
            self.positions.pop(key, None)
            return
        avg = cost_amount / shares if shares else 0.0
        existing = self.positions.get(key)
        self.positions[key] = {
            "customer_id": customer_id.upper(), "product_code": product_code,
            "shares": shares, "cost_amount": cost_amount, "avg_cost": avg,
            "opened_at": (existing or {}).get("opened_at") or datetime.now(),
            "updated_at": datetime.now(),
        }

    def list_positions(self, customer_id):
        return [
            dict(row) for (cid, _code), row in self.positions.items()
            if cid == customer_id.upper()
        ]

    def list_orders(self, customer_id, limit=100):
        rows = [dict(r) for r in self.orders if r["customer_id"] == customer_id.upper()]
        return rows[-limit:][::-1]

    def list_transactions(self, customer_id, limit=100):
        rows = [dict(r) for r in self.transactions if r["customer_id"] == customer_id.upper()]
        return rows[-limit:][::-1]

    def realized_pnl_total(self, customer_id):
        return sum(
            float(r.get("realized_pnl") or 0)
            for r in self.orders
            if r["customer_id"] == customer_id.upper() and r["side"] == "sell"
        )


class _FakeLibrary:
    """实现 query_by_code / query_by_codes / list_products。"""

    def __init__(self, products):
        self._products = products

    def _shape(self, code):
        raw = self._products.get(code)
        if raw is None:
            return None
        return {
            "basic_info": {
                "code": code, "name": raw["name"], "type": raw.get("type", "fund"),
                "risk_level": raw.get("risk_level", ""), "company": raw.get("company", ""),
                "manager": raw.get("manager", ""), "scale": raw.get("scale"),
                "subscription_fee": raw.get("subscription_fee"),
                "redemption_fee": raw.get("redemption_fee", ""),
                "recommended_holding_period": raw.get("recommended_holding_period", ""),
                "investment_target": raw.get("investment_target", ""),
                "is_active": raw.get("is_active", True),
            },
            "performance": {"nav": raw.get("nav"), "as_of": raw.get("as_of", ""),
                            "source": "postgresql", "return_1y": raw.get("return_1y"),
                            "max_drawdown": raw.get("max_drawdown"),
                            "volatility": raw.get("volatility"),
                            "sharpe_ratio": raw.get("sharpe_ratio")},
            "fee": {
                "subscription_fee": raw.get("subscription_fee"),
                "redemption_fee": raw.get("redemption_fee", ""),
            },
        }

    def query_by_code(self, code):
        return self._shape(str(code).strip())

    def query_by_codes(self, codes):
        out = []
        for code in codes:
            item = self._shape(str(code).strip())
            if item is not None:
                out.append(item)
        return out

    def list_products(self, product_type="fund", include_inactive=False):
        kind = (product_type or "fund").strip()
        return [
            {"code": code}
            for code, data in self._products.items()
            if (include_inactive or data.get("is_active", True))
            and (kind.lower() == "all" or data.get("type", "fund") == kind)
        ]


class _FakeNavSource:
    def __init__(self, navs):
        self._navs = navs

    def latest_nav(self, product_code):
        nav = self._navs.get(str(product_code).strip())
        if nav is None:
            return None
        return NavQuote(product_code=str(product_code), nav=nav, as_of="2026-09-08")


PRODUCTS = {
    "110011": {"name": "易方达中小盘混合", "nav": 3.85, "subscription_fee": 1.5,
               "redemption_fee": "0.5%", "risk_level": "R3 中风险",
               "volatility": 0.162, "return_1y": 0.126, "max_drawdown": 0.187},
    "003003": {"name": "华夏现金增利货币A", "nav": 1.0, "subscription_fee": 0.0,
               "redemption_fee": "0", "risk_level": "R1 低风险",
               "volatility": 0.002, "return_1y": 0.0195, "max_drawdown": 0.0},
    # 费率未披露且没有净值：不可成交，用于验证显式失败。
    "999999": {"name": "数据缺失示例基金", "nav": None, "subscription_fee": None,
               "redemption_fee": "", "risk_level": "未披露"},
}


@pytest.fixture()
def service():
    store = _FakeStore()
    deps = PortfolioDeps(
        store=store,
        library=_FakeLibrary(PRODUCTS),
        nav_source=_FakeNavSource({
            "110011": 3.85, "003003": 1.0,
        }),
    )
    return PortfolioService(deps)


@pytest.fixture()
def products():
    return ProductService(ProductDeps(
        library=_FakeLibrary(PRODUCTS),
        nav_source=_FakeNavSource({"110011": 3.85, "003003": 1.0}),
    ))


CUSTOMER = "CUST000001"


# ── 费率 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (1.5, 0.015),
    (0, 0.0),
    ("0.5%", 0.005),
    ("0", 0.0),
    ("0.8", 0.008),
    (None, None),
    ("", None),
    ("未披露", None),
])
def test_to_rate_normalizes_both_encodings(raw, expected):
    """费率两种编码（数字/百分比字符串）都要归一到小数。"""
    assert to_rate(raw) == expected


def test_subscription_by_amount_uses_outer_deduction():
    """外扣法：费用 = 金额 - 金额/(1+费率)，扣款不超过申请金额。"""
    result = subscription_by_amount(10000.0, 3.85, 0.015)
    assert result.fee == pytest.approx(147.78, abs=0.01)
    assert result.shares == pytest.approx(2559.01, abs=0.01)
    assert result.cash_out <= 10000.0


def test_round_shares_floors_never_rounds_up():
    """份额向下取整：向上取整会导致扣款超过申请金额。"""
    assert round_shares(2559.019) == 2559.01
    assert round_shares(0.0) == 0.0
    assert round_shares(-5) == 0.0


def test_undisclosed_fee_is_reported_not_assumed_zero():
    """费率未披露时照常成交，但必须登记 fee_unavailable 限制项。"""
    result = subscription_by_amount(10000.0, 3.85, None)
    assert "fee_unavailable:subscription_fee" in result.limitations
    assert result.fee == 0.0


# ── 商品 ──────────────────────────────────────────────────────────

def test_list_products_marks_untradable_without_nav(products):
    """无净值商品照样上架，但 tradable=False 且带限制项。"""
    shelf = {item.code: item for item in products.list_products()}
    assert shelf["110011"].tradable is True
    assert shelf["999999"].tradable is False
    assert "pricing_unavailable" in shelf["999999"].limitations
    assert shelf["110011"].nav.value == 3.85


def test_list_products_filters_fund_etf_and_wealth():
    """基金列表不含 ETF 和理财；etf、wealth、all 各自只返回对应集合。"""
    catalog = {
        "110011": {"name": "混合基金", "type": "fund", "nav": 1.2},
        "510300": {"name": "沪深300ETF", "type": "etf", "nav": 4.1},
        "WM001": {"name": "现金理财", "type": "wealth", "nav": 1.01},
    }
    service = ProductService(ProductDeps(
        library=_FakeLibrary(catalog),
        nav_source=_FakeNavSource({"110011": 1.2, "510300": 4.1, "WM001": 1.01}),
    ))

    def codes(product_type):
        return {item.code for item in service.list_products(product_type)}

    assert codes("fund") == {"110011"}
    assert codes("etf") == {"510300"}
    assert codes("wealth") == {"WM001"}
    assert codes("all") == {"110011", "510300", "WM001"}


def test_get_product_rejects_unknown_code(products):
    with pytest.raises(ProductError) as exc:
        products.get_product("000000")
    assert exc.value.code == "product_not_found"


def test_product_view_reads_fees_from_fee_block(products):
    """产品库把费率列从 basic_info 摘到 fee 子字典；只读 basic_info 会一律显示未披露。"""
    view = products.get_product("110011")
    # 1.5% 以百分比数值返回（前端直接加 %）。
    assert view.subscription_fee == pytest.approx(1.5)
    assert view.redemption_fee == "0.5%"
    assert "fee_unavailable:subscription_fee" not in view.limitations


def test_product_view_reports_undisclosed_fee(products):
    view = products.get_product("999999")
    assert view.subscription_fee is None
    assert "fee_unavailable:subscription_fee" in view.limitations


def test_product_view_passes_through_decimal_ratios(products, monkeypatch):
    """收益/回撤以小数存储（0.126 表示 12.6%），服务层原样透传，由前端负责换算。"""
    monkeypatch.setitem(PRODUCTS, "110011", {
        "name": "易方达中小盘混合", "nav": 3.85, "subscription_fee": 1.5,
        "redemption_fee": "0.5%", "return_1y": 0.126, "max_drawdown": 0.187,
    })
    view = products.get_product("110011")
    assert view.return_1y == pytest.approx(0.126)
    assert view.max_drawdown == pytest.approx(0.187)


# ── 充值 ──────────────────────────────────────────────────────────

def test_deposit_increases_cash_and_total_deposit(service):
    txn, account, replay = service.deposit(CUSTOMER, 100000.0)
    assert replay is False
    assert txn.kind == "deposit"
    assert account.cash_balance == 100000.0
    assert account.total_deposit == 100000.0
    assert account.total_assets == 100000.0


def test_deposit_rejects_non_positive_amount(service):
    with pytest.raises(PortfolioError) as exc:
        service.deposit(CUSTOMER, 0)
    assert exc.value.code == "invalid_amount"
    with pytest.raises(PortfolioError) as exc:
        service.deposit(CUSTOMER, -100)
    assert exc.value.code == "invalid_amount"


def test_deposit_rejects_amount_over_cap(service):
    with pytest.raises(PortfolioError) as exc:
        service.deposit(CUSTOMER, 99_999_999.0)
    assert exc.value.code == "invalid_amount"


def test_deposit_with_same_idempotency_key_does_not_double_credit(service):
    """同一幂等键重复充值只入账一次（网络重试保护）。"""
    service.deposit(CUSTOMER, 50000.0, idempotency_key="dep-1")
    _txn, account, replay = service.deposit(CUSTOMER, 50000.0, idempotency_key="dep-1")
    assert replay is True
    assert account.cash_balance == 50000.0
    assert account.total_deposit == 50000.0


# ── 买入 ──────────────────────────────────────────────────────────

def test_buy_creates_position_and_deducts_cash(service):
    service.deposit(CUSTOMER, 100000.0)
    result = service.buy(CUSTOMER, "110011", amount=38500.0)

    assert result.order.side == "buy"
    assert result.order.shares == pytest.approx(9852.22, abs=0.05)
    assert result.position is not None
    assert result.position.shares == result.order.shares
    # 成本含费：实际现金支出全部计入成本。
    assert result.position.cost_amount == pytest.approx(38500.0, abs=0.05)
    assert result.account.cash_balance == pytest.approx(61500.0, abs=0.05)
    assert result.account.position_count == 1


def test_buy_rejects_insufficient_funds_without_mutating_account(service):
    service.deposit(CUSTOMER, 1000.0)
    with pytest.raises(PortfolioError) as exc:
        service.buy(CUSTOMER, "110011", amount=50000.0)
    assert exc.value.code == "insufficient_funds"
    # 失败不得留下任何痕迹。
    assert service.get_account(CUSTOMER).cash_balance == 1000.0
    assert service.list_positions(CUSTOMER) == []


def test_buy_rejects_product_without_nav(service):
    """无净值商品必须显式失败，不能用 1.0 顶替成交。"""
    service.deposit(CUSTOMER, 100000.0)
    with pytest.raises(PortfolioError) as exc:
        service.buy(CUSTOMER, "999999", amount=10000.0)
    assert exc.value.code == "pricing_unavailable"
    assert service.get_account(CUSTOMER).cash_balance == 100000.0


def test_buy_requires_exactly_one_of_amount_or_shares(service):
    service.deposit(CUSTOMER, 100000.0)
    with pytest.raises(PortfolioError) as exc:
        service.buy(CUSTOMER, "110011")
    assert exc.value.code == "invalid_amount"
    with pytest.raises(PortfolioError) as exc:
        service.buy(CUSTOMER, "110011", amount=1000.0, shares=100)
    assert exc.value.code == "invalid_amount"


def test_buy_below_minimum_amount_is_rejected(service):
    service.deposit(CUSTOMER, 100000.0)
    with pytest.raises(PortfolioError) as exc:
        service.buy(CUSTOMER, "110011", amount=50.0)
    assert exc.value.code == "invalid_amount"


def test_buy_accumulates_weighted_cost_across_two_purchases(service):
    """两次买入按移动加权累加成本与份额。"""
    service.deposit(CUSTOMER, 100000.0)
    first = service.buy(CUSTOMER, "110011", amount=10000.0)
    second = service.buy(CUSTOMER, "110011", amount=20000.0)
    position = second.position
    assert position.shares == pytest.approx(first.order.shares + second.order.shares, abs=0.01)
    assert position.cost_amount == pytest.approx(30000.0, abs=0.05)
    assert position.avg_cost == pytest.approx(30000.0 / position.shares, abs=0.01)


def test_buy_with_same_idempotency_key_does_not_double_deduct(service):
    service.deposit(CUSTOMER, 100000.0)
    first = service.buy(CUSTOMER, "110011", amount=10000.0, idempotency_key="buy-1")
    cash_after_first = first.account.cash_balance
    replay = service.buy(CUSTOMER, "110011", amount=10000.0, idempotency_key="buy-1")

    assert replay.idempotent_replay is True
    assert replay.order.order_id == first.order.order_id
    assert replay.account.cash_balance == cash_after_first
    assert len(service.list_orders(CUSTOMER)) == 1


def test_buy_unique_violation_after_missed_lookup_replays(service):
    """并发窗口：先查未命中、INSERT 撞唯一约束时必须回放，不得二次扣款。"""
    service.deposit(CUSTOMER, 100000.0)
    first = service.buy(CUSTOMER, "110011", amount=10000.0, idempotency_key="race-buy")
    cash_after_first = first.account.cash_balance
    original_find = service.store.find_order_by_key
    calls = {"n": 0}

    def find_miss_once(customer_id, idempotency_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_find(customer_id, idempotency_key)

    service.store.find_order_by_key = find_miss_once
    replay = service.buy(CUSTOMER, "110011", amount=10000.0, idempotency_key="race-buy")

    assert replay.idempotent_replay is True
    assert replay.order.order_id == first.order.order_id
    assert replay.account.cash_balance == cash_after_first
    assert len(service.list_orders(CUSTOMER)) == 1


def test_deposit_unique_violation_after_missed_lookup_replays(service):
    """并发充值撞唯一约束时返回首次流水，不得二次入账。"""
    first_txn, account, _ = service.deposit(CUSTOMER, 50000.0, idempotency_key="race-dep")
    original_find = service.store.find_transaction_by_key
    calls = {"n": 0}

    def find_miss_once(customer_id, idempotency_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_find(customer_id, idempotency_key)

    service.store.find_transaction_by_key = find_miss_once
    txn, replayed_account, replay = service.deposit(
        CUSTOMER, 50000.0, idempotency_key="race-dep",
    )

    assert replay is True
    assert txn.txn_id == first_txn.txn_id
    assert replayed_account.cash_balance == account.cash_balance
    assert replayed_account.total_deposit == 50000.0


def test_buy_reports_undisclosed_fee_as_limitation(service, monkeypatch):
    """费率未披露（含净值）时，订单里必须能看到限制项。"""
    monkeypatch.setitem(PRODUCTS, "110011", {
        "name": "易方达中小盘混合", "nav": 3.85, "subscription_fee": None,
        "redemption_fee": "", "risk_level": "R3 中风险",
    })
    service.deposit(CUSTOMER, 100000.0)
    result = service.buy(CUSTOMER, "110011", amount=10000.0)
    assert "fee_unavailable:subscription_fee" in result.order.fee_limitations
    assert result.order.fee == 0.0


# ── 卖出 ──────────────────────────────────────────────────────────

def test_sell_partial_keeps_remaining_cost_and_records_realized_pnl(service):
    """部分赎回按份额比例结转成本，剩余成本不产生残余。"""
    service.deposit(CUSTOMER, 100000.0)
    bought = service.buy(CUSTOMER, "110011", amount=40000.0)
    total_shares = bought.order.shares
    original_cost = bought.position.cost_amount
    cost_out = original_cost / 2

    sold = service.sell(CUSTOMER, "110011", shares=total_shares / 2)
    assert sold.position is not None
    assert sold.position.shares == pytest.approx(total_shares / 2, abs=0.01)
    # 结转一半成本，剩余成本刚好是另一半（无残余）。
    assert sold.position.cost_amount == pytest.approx(cost_out, abs=0.05)
    assert sold.position.cost_amount + cost_out == pytest.approx(original_cost, abs=0.02)
    # 已实现盈亏 = 净到账 - 结转成本；净值未变，所以为负（申购费已在成本里 + 赎回费）。
    assert sold.order.realized_pnl is not None
    assert sold.order.realized_pnl == pytest.approx(sold.order.net_amount - cost_out, abs=0.05)
    assert sold.order.realized_pnl < 0


def test_sell_all_removes_position(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=10000.0)
    result = service.sell(CUSTOMER, "110011", all_shares=True)
    assert result.position is None
    assert service.list_positions(CUSTOMER) == []


def test_sell_rejects_shares_over_holding(service):
    service.deposit(CUSTOMER, 100000.0)
    bought = service.buy(CUSTOMER, "110011", amount=10000.0)
    with pytest.raises(PortfolioError) as exc:
        service.sell(CUSTOMER, "110011", shares=bought.order.shares + 100)
    assert exc.value.code == "insufficient_shares"


def test_sell_without_position_is_explicit_failure(service):
    service.deposit(CUSTOMER, 100000.0)
    with pytest.raises(PortfolioError) as exc:
        service.sell(CUSTOMER, "110011", all_shares=True)
    assert exc.value.code == "no_position"


def test_sell_credits_cash_net_of_fee(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "003003", amount=10000.0)  # 免费赎回的货币基金
    before = service.get_account(CUSTOMER).cash_balance
    result = service.sell(CUSTOMER, "003003", all_shares=True)
    assert result.order.fee == 0.0
    # 现金增加额等于净到账金额。
    assert result.account.cash_balance == pytest.approx(before + result.order.net_amount, abs=0.01)


# ── 清仓 ──────────────────────────────────────────────────────────

def test_liquidate_all_clears_every_position(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=10000.0)
    service.buy(CUSTOMER, "003003", amount=10000.0)
    assert len(service.list_positions(CUSTOMER)) == 2

    result = service.liquidate_all(CUSTOMER)
    assert sorted(result.cleared_codes) == ["003003", "110011"]
    assert len(result.orders) == 2
    assert result.failed == {}
    assert service.list_positions(CUSTOMER) == []
    assert result.account.position_count == 0
    # 全部回到现金。
    assert result.account.market_value == 0.0


def test_liquidate_all_isolates_unpriced_position(service):
    """一个商品取不到净值只记入 failed，不阻断其余清仓、也不伪造成交。"""
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=10000.0)
    # 模拟"曾经买过、如今净值不可用"的持仓（service 不会自己造出这种态）。
    service.store.positions[(CUSTOMER, "999999")] = {
        "customer_id": CUSTOMER, "product_code": "999999", "shares": 100.0,
        "cost_amount": 100.0, "avg_cost": 1.0,
        "opened_at": datetime.now(), "updated_at": datetime.now(),
    }

    result = service.liquidate_all(CUSTOMER)
    assert result.cleared_codes == ["110011"]
    assert "999999" in result.failed
    assert result.failed["999999"] == "pricing_unavailable"


# ── 账户口径 ──────────────────────────────────────────────────────

def test_account_totals_reconcile_with_cash_and_market_value(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=38500.0)
    account = service.get_account(CUSTOMER)

    assert account.total_assets == pytest.approx(
        account.cash_balance + account.market_value, abs=0.02
    )
    assert account.total_deposit == 100000.0
    # 刚买入时相对本金为负（申购费与管理费等摩擦），不得为正。
    assert account.total_pnl < 0
    assert account.position_pnl < 0


def test_account_marks_incomplete_when_position_unpriced(service):
    """存在无法定价的持仓时，市值口径必须标记为不完整并列出代码。"""
    service.deposit(CUSTOMER, 100000.0)
    service.store.positions[(CUSTOMER, "999999")] = {
        "customer_id": CUSTOMER, "product_code": "999999", "shares": 100.0,
        "cost_amount": 100.0, "avg_cost": 1.0,
        "opened_at": datetime.now(), "updated_at": datetime.now(),
    }
    account = service.get_account(CUSTOMER)
    assert account.market_value_complete is False
    assert "999999:unavailable" in account.pricing_issues


def test_account_is_created_empty_on_first_access(service):
    """首次访问自动开户，且余额为 0（不注入默认资金）。"""
    account = service.get_account(CUSTOMER)
    assert isinstance(account, AccountSnapshot)
    assert account.cash_balance == 0.0
    assert account.total_deposit == 0.0
    assert account.simulated is True


def test_accounts_are_isolated_between_customers(service):
    service.deposit("CUST000001", 100000.0)
    service.deposit("CUST000002", 5000.0)
    service.buy("CUST000001", "110011", amount=10000.0)

    assert service.get_account("CUST000002").cash_balance == 5000.0
    assert service.list_positions("CUST000002") == []
    assert len(service.list_positions("CUST000001")) == 1


def test_position_weight_is_share_of_market_value(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=10000.0)
    service.buy(CUSTOMER, "003003", amount=30000.0)
    positions = {p.product_code: p for p in service.list_positions(CUSTOMER)}
    assert positions["110011"].weight is not None
    assert positions["003003"].weight is not None
    assert positions["110011"].weight + positions["003003"].weight == pytest.approx(100.0, abs=0.01)
    assert positions["003003"].weight > positions["110011"].weight


# ── 记录查询 ──────────────────────────────────────────────────────

def test_orders_and_transactions_are_recorded(service):
    service.deposit(CUSTOMER, 100000.0)
    service.buy(CUSTOMER, "110011", amount=10000.0)
    service.sell(CUSTOMER, "110011", all_shares=True)

    orders = service.list_orders(CUSTOMER)
    assert [o.side for o in orders] == ["sell", "buy"]
    kinds = [t.kind for t in service.list_transactions(CUSTOMER)]
    assert "deposit" in kinds and "buy" in kinds and "sell" in kinds


def test_order_view_carries_product_name(service):
    service.deposit(CUSTOMER, 100000.0)
    result = service.buy(CUSTOMER, "110011", amount=10000.0)
    assert result.order.product_name == "易方达中小盘混合"
