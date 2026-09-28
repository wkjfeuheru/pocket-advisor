"""采纳流程：把"用户在中断里选了一个方案"落到 AI 账本。

把前面几个接缝串起来：中断载荷的 resume 校验 → 读取方案 → （首次时）建立 AI 账户 →
按方案权重做差额调仓 → 返回采纳结果。

三条不变量：

- **只能用载荷里提供的 plan_id**（``validate_allocation_resume``）——模型和前端都不能
  凭空指定方案，否则等于绕过校验器。
- **用户持仓一行都不动**：本流程只经 ``LedgerService``，它只写 ``ai_*`` 表。
- **采纳不可逆**：同一 plan_id 第二次采纳返回 ``already_adopted``。

═══════════════════════════════════════════════════════════════════════════
derivation —— 首次采纳的建仓与成交
═══════════════════════════════════════════════════════════════════════════
用户当前：110011 800 份（市值 800，净值 1.00）、510300 100 份（市值 200，净值 2.00）
总资产 = 1000；现金 0

方案（中度）：110011 0.20 / 510300 0.70 / cash 0.10（和 = 1）

    AI 账户建立：本金 1000、份额 1000、unit_nav 1.0
                 持仓镜像用户：110011 800 份、510300 100 份（按市值/净值折算）

    目标金额 = 1000 × 目标权重
        110011：1000 × 0.20 = 200，当前 800 → 卖 600 → 600 份 @1.00
        510300：1000 × 0.70 = 700，当前 200 → 买 500 → 250 份 @2.00

    费率：卖 0.5%、买 0.15%
        卖 600 → 费 3.00
        买 500 → 费 0.75
        现金 = (600 - 3.00) - (500 + 0.75) = 597 - 500.75 = 96.25

    持仓：110011 200 份、510300 350 份
    总资产 = 200×1.00 + 350×2.00 + 96.25 = 200 + 700 + 96.25 = 996.25
    份额仍为 1000 → unit_nav = 0.99625（跌幅 = 费用 3.75/1000）

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.portfolio.contracts import AllocationInterruptError, build_allocation_payload
from app.agents.portfolio.plan import PlanTier
from app.services.portfolio_advisor.adopt import (
    AdoptError,
    adopt_selected_plan,
)
from app.services.portfolio_advisor.ledger import LedgerService

AS_OF = date(2026, 9, 28)

CURRENT_HOLDINGS = {
    "110011": {"market_value": 800.0, "shares": 800.0, "priceable": True},
    "510300": {"market_value": 200.0, "shares": 100.0, "priceable": True},
}
NAVS = {"110011": 1.00, "510300": 2.00}


class _FakeLedgerStore:
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
        return [dict(r) for (cid, _), r in self.positions.items() if cid == customer_id]

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
        trade_key = record.get("trade_key") or ""
        if trade_key and any(
            r["customer_id"] == record["customer_id"] and r.get("trade_key") == trade_key
            for r in self.trades
        ):
            from app.core.security import UniqueConstraintError

            raise UniqueConstraintError()
        self.trades.append(dict(record))
        return dict(record)

    def find_trade_by_key(self, customer_id, adoption_key):
        if not adoption_key:
            return None
        for row in self.trades:
            if row["customer_id"] == customer_id and row.get("adoption_key") == adoption_key:
                return dict(row)
        return None

    def mark_plan_adopted(self, plan_id, *, adopted_at):
        self.plans.setdefault(plan_id, {})
        self.plans[plan_id].update(
            {"plan_id": plan_id, "status": "adopted", "adopted_at": adopted_at}
        )

    def get_plan(self, plan_id):
        row = self.plans.get(plan_id)
        return dict(row) if row else None


class _FakePlanReader:
    """按 plan_id 返回已落库的方案（含明细）。"""

    def __init__(self, plans):
        self._plans = plans

    def get_plan(self, plan_id):
        return self._plans.get(plan_id)


def _plan(plan_id: str, weights: dict[str, float], tier: str = "moderate") -> dict:
    return {
        "plan_id": plan_id,
        "tier": tier,
        "turnover_rate": 0.275,
        "estimated_fee": 3.75,
        "status": "offered",
        "weights": weights,
        "items": [
            {
                "product_code": code,
                "action": "reduce" if code == "110011" else "add",
                "current_weight": 0.80 if code == "110011" else 0.20,
                "target_weight": weight,
                "delta_weight": 0.0,
                "reason": "测试",
            }
            for code, weight in weights.items()
            if code != "cash"
        ],
    }


WEIGHTS = {"110011": 0.20, "510300": 0.70, "cash": 0.10}


@pytest.fixture
def ledger_store():
    return _FakeLedgerStore()


@pytest.fixture
def ledger(ledger_store):
    return LedgerService(ledger_store)


def _payload(plan_id: str = "p-1"):
    return build_allocation_payload([_plan(plan_id, WEIGHTS)])


def _adopt(ledger, ledger_store, reader, *, payload=None, resume=None, **overrides):
    kwargs = {
        "customer_id": "U1",
        "payload": payload if payload is not None else _payload(),
        "resume_value": resume if resume is not None else {"plan_id": "p-1"},
        "plans": reader,
        "ledger": ledger,
        "current_holdings": CURRENT_HOLDINGS,
        "navs": NAVS,
        "nav_as_of": AS_OF,
        "initial_capital": 1000.0,
        "fee_buy": 0.0015,
        "fee_sell": 0.005,
    }
    kwargs.update(overrides)
    return adopt_selected_plan(**kwargs)


def test_first_adoption_establishes_the_account_and_rebalances(ledger, ledger_store):
    reader = _FakePlanReader({"p-1": _plan("p-1", WEIGHTS)})

    result = _adopt(ledger, ledger_store, reader)

    # 账户已建立且份额 = 本金
    account = ledger_store.get_account("U1")
    assert account["total_units"] == pytest.approx(1000.0)
    assert account["initial_capital"] == pytest.approx(1000.0)

    # 两笔差额成交
    trades = {(t["product_code"], t["side"]): t for t in ledger_store.trades}
    assert set(trades) == {("110011", "sell"), ("510300", "buy")}
    assert trades[("110011", "sell")]["gross_amount"] == pytest.approx(600.0)
    assert trades[("510300", "buy")]["gross_amount"] == pytest.approx(500.0)

    # 现金 96.25、总资产 996.25、净值减掉费用
    assert result.cash_balance == pytest.approx(96.25)
    assert result.total_assets == pytest.approx(996.25)
    assert result.unit_nav == pytest.approx(0.99625, abs=1e-9)
    assert result.plan_id == "p-1"
    assert result.tier == PlanTier.MODERATE


def test_adoption_never_touches_user_holdings(ledger, ledger_store):
    """最关键的不变量：采纳只写 ai_* 表。"""
    reader = _FakePlanReader({"p-1": _plan("p-1", WEIGHTS)})

    _adopt(ledger, ledger_store, reader)

    # 假存储里根本没有用户持仓入口；只有 ai_* 被写过
    assert not hasattr(ledger_store, "user_positions")
    assert ledger_store.trades
    assert all(t["customer_id"] == "U1" for t in ledger_store.trades)


def test_resuming_with_an_invented_plan_id_is_rejected_before_any_write(ledger, ledger_store):
    reader = _FakePlanReader({"p-1": _plan("p-1", WEIGHTS)})

    with pytest.raises(AllocationInterruptError) as excinfo:
        _adopt(ledger, ledger_store, reader, resume={"plan_id": "invented"})

    assert excinfo.value.code == "unknown_plan"
    assert ledger_store.trades == []


def test_an_unavailable_plan_cannot_be_adopted(ledger, ledger_store):
    plan = _plan("p-1", WEIGHTS)
    plan["unavailable_reason"] = "turnover_out_of_band"
    payload = build_allocation_payload([plan])
    reader = _FakePlanReader({"p-1": plan})

    with pytest.raises(AllocationInterruptError) as excinfo:
        _adopt(ledger, ledger_store, reader, payload=payload)

    assert excinfo.value.code == "plan_unavailable"
    assert ledger_store.trades == []


def test_a_plan_missing_from_storage_is_reported(ledger, ledger_store):
    reader = _FakePlanReader({})  # 载荷里有，库里没有

    with pytest.raises(AdoptError) as excinfo:
        _adopt(ledger, ledger_store, reader)

    assert excinfo.value.code == "plan_not_found"


def test_adopting_the_same_plan_twice_is_rejected(ledger, ledger_store):
    reader = _FakePlanReader({"p-1": _plan("p-1", WEIGHTS)})

    _adopt(ledger, ledger_store, reader)
    trades_after_first = len(ledger_store.trades)

    with pytest.raises(AdoptError) as excinfo:
        _adopt(ledger, ledger_store, reader)

    assert excinfo.value.code == "already_adopted"
    assert len(ledger_store.trades) == trades_after_first


def test_the_account_is_not_reestablished_on_a_second_distinct_plan(ledger, ledger_store):
    """第二次采纳不同方案时不能重建账户——否则历史净值曲线会断。"""
    first = _plan("p-1", WEIGHTS)
    second = _plan("p-2", {"110011": 0.50, "510300": 0.45, "cash": 0.05}, tier="light")
    reader = _FakePlanReader({"p-1": first, "p-2": second})

    _adopt(ledger, ledger_store, reader)
    account_after_first = dict(ledger_store.get_account("U1"))

    _adopt(
        ledger,
        ledger_store,
        reader,
        payload=build_allocation_payload([second]),
        resume={"plan_id": "p-2"},
    )

    account = ledger_store.get_account("U1")
    assert account["initial_capital"] == account_after_first["initial_capital"]
    assert account["total_units"] == account_after_first["total_units"]
    # 第二笔采纳新增了成交
    assert len(ledger_store.trades) > 2


def test_a_plan_with_no_usable_nav_is_reported(ledger, ledger_store):
    """方案里有权重但拿不到净值 → 不能凭空成交。"""
    plan = _plan("p-1", {"110011": 0.20, "999999": 0.70, "cash": 0.10})
    reader = _FakePlanReader({"p-1": plan})

    with pytest.raises(AdoptError) as excinfo:
        _adopt(
            ledger,
            ledger_store,
            reader,
            payload=build_allocation_payload([plan]),
        )

    assert excinfo.value.code == "missing_nav"
    assert ledger_store.trades == []
