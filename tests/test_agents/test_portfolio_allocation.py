"""方案编排：把一次"给我配置建议"变成落库的方案。

这一步是深模块与外部世界的接缝：输入已经取好的持仓、商品池与画像，输出**已落库**的
三类方案（带 ``plan_id``，可被采纳）。取数（持仓接口、商品池）由调用方负责，
这样本模块保持纯确定性、可单测。

三条职责边界：

- **转换**：``GeneratedPlan.weights``（含 ``cash``）→ 方案明细行。``cash`` 不是商品，
  没有 ``product_code``，因此**不生成明细行**——它只体现在权重之和里。
- **动作判定**：``open``/``add``/``reduce``/``close``/``hold`` 由"当前权重 vs 目标权重"
  决定，不由模型给。
- **不落库不可用档**：``available=False`` 的档位只记日志式信息，不写进
  ``allocation_plans``——否则用户会看到一个点不开的空方案。

═══════════════════════════════════════════════════════════════════════════
derivation —— 动作判定
═══════════════════════════════════════════════════════════════════════════
    当前 0    → 目标 >0     open
    当前 >0   → 目标 >0 且增  add
    当前 >0   → 目标 >0 且减  reduce
    当前 >0   → 目标 =0     close
    当前 =0   → 目标 =0     （不生成明细行）
    当前 >0   → 目标 =0 附近但极小（<1e-9）视为 close

    当前 0.80 → 目标 0.7125  reduce（差 -0.0875）
    当前 0.20 → 目标 0.2375  add   （差 +0.0375）
    当前 0.00 → 目标 0.1500  open  （差 +0.1500）

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.portfolio.candidates import build_fallback_candidates
from app.agents.portfolio.plan import PlanTier
from app.agents.portfolio.validation import ValidationConstraints
from app.services.portfolio_advisor.allocation import (
    AllocationError,
    action_for,
    generate_and_persist,
)

CONSTRAINTS = ValidationConstraints(
    max_single_weight=0.85,
    cash_floor=0.05,
    max_risk_level="R3",
)

PRODUCTS = {
    "110011": {"name": "易方达中小盘混合", "type": "fund", "risk_level": "R3", "priceable": True},
    "510300": {"name": "华泰柏瑞沪深300ETF", "type": "etf", "risk_level": "R3", "priceable": True},
    "518880": {"name": "华安黄金ETF", "type": "etf", "risk_level": "R2", "priceable": True},
}

CURRENT = {"110011": 0.60, "510300": 0.40}

#: 一个能让三档都可行的候选：把大部分仓位挪向从未持有的 518880，
#: 使全长换手率达到 0.65（= 重构档目标），因此三档都能落进各自区间。
CANDIDATE = {"110011": 0.10, "510300": 0.30, "518880": 0.55, "cash": 0.05}

AS_OF = date(2026, 9, 28)


class _FakePlanStore:
    """实现 PlanStore 协议；记录落库的 plan 与 items。"""

    def __init__(self):
        self.plans: list[dict] = []
        self.items: list[dict] = []

    def insert_plan(self, plan, items):
        self.plans.append(dict(plan))
        for item in items:
            self.items.append(dict(item))
        return plan["plan_id"]

    def get_plan(self, plan_id):
        for plan in self.plans:
            if plan["plan_id"] == plan_id:
                return dict(plan)
        return None

    def list_plans(self, customer_id, *, limit=50):
        return [dict(p) for p in self.plans if p["customer_id"] == customer_id][:limit]


@pytest.fixture
def store():
    return _FakePlanStore()


def _run(store, **overrides):
    kwargs = {
        "customer_id": "U1",
        "current_weights": CURRENT,
        "products": PRODUCTS,
        "constraints": CONSTRAINTS,
        "candidates": {tier: dict(CANDIDATE) for tier in PlanTier},
        "initial_capital": 100000.0,
        "nav_as_of": AS_OF,
    }
    kwargs.update(overrides)
    return generate_and_persist(store, **kwargs)


def test_actions_are_derived_from_the_weight_change():
    assert action_for(0.0, 0.15) == "open"
    assert action_for(0.80, 0.7125) == "reduce"
    assert action_for(0.20, 0.2375) == "add"
    assert action_for(0.30, 0.0) == "close"
    assert action_for(0.0, 0.0) == "hold"
    # 极小残留视为清仓，不生成"持有 0.0000001%"这种行
    assert action_for(0.30, 1e-12) == "close"


def test_every_available_tier_is_persisted_with_a_plan_id(store):
    result = _run(store)

    assert len(store.plans) == len(result.plans) == 3
    assert {p["tier"] for p in store.plans} == {"light", "moderate", "rebuild"}
    for plan in store.plans:
        assert plan["plan_id"]
        assert plan["customer_id"] == "U1"
        assert plan["status"] == "offered"
        assert plan["nav_as_of"] == AS_OF


def test_unavailable_tiers_are_reported_but_not_persisted(store):
    """不可用档位不写库——否则用户会看到一个点不开的空方案。"""
    # 当前持仓已越风险等级：R4 标的占大头
    products = dict(PRODUCTS)
    products["161725"] = {
        "name": "招商中证白酒指数A",
        "type": "fund",
        "risk_level": "R4",
        "priceable": True,
    }
    result = _run(store, current_weights={"161725": 0.90, "510300": 0.10}, products=products)

    persisted_tiers = {p["tier"] for p in store.plans}
    unavailable_tiers = {p.tier.value for p in result.unavailable}
    assert persisted_tiers.isdisjoint(unavailable_tiers)
    assert len(result.available) == len(store.plans)


def test_cash_never_becomes_a_plan_item(store):
    """``cash`` 不是商品，没有 product_code，不能生成明细行。"""
    _run(store)

    assert store.items
    assert all(item["product_code"] != "cash" for item in store.items)


def test_plan_items_carry_the_weight_change_and_amounts(store):
    _run(store)

    light = next(p for p in store.plans if p["tier"] == "light")
    item = next(
        i for i in store.items
        if i["plan_id"] == light["plan_id"] and i["product_code"] == "110011"
    )

    # 微调档：110011 从 0.60 降到 0.531004367（见 plan 模块按目标换手率反解的 λ）
    assert item["action"] == "reduce"
    assert item["current_weight"] == pytest.approx(0.60, abs=1e-9)
    assert item["delta_weight"] < 0
    assert item["target_weight"] == pytest.approx(0.531004367, abs=1e-8)
    # 恒等式：目标 = 当前 + 变动（减仓腾出的权重去了从未持有的 518880 与现金）
    assert item["target_weight"] == pytest.approx(
        item["current_weight"] + item["delta_weight"], abs=1e-12
    )
    assert item["current_amount"] == pytest.approx(60000.0, abs=1e-6)
    assert item["target_amount"] == pytest.approx(item["target_weight"] * 100000.0, abs=1e-6)
    assert item["product_name"] == "易方达中小盘混合"


def test_a_never_held_product_gets_an_open_action(store):
    """回退/候选里出现当前没持有的标的时，动作必须是 ``open``。"""
    _run(store)

    light = next(p for p in store.plans if p["tier"] == "light")
    item = next(
        i for i in store.items
        if i["plan_id"] == light["plan_id"] and i["product_code"] == "518880"
    )

    assert item["action"] == "open"
    assert item["current_weight"] == pytest.approx(0.0, abs=1e-12)
    assert item["target_weight"] > 0


# 说明：``close``（目标权重归零）没有编排层用例，只有 ``action_for`` 的单元用例。
# 原因是 λ 由目标换手率反解、上界又被"路径可行"与档位区间同时夹住，实际取到的
# λ 几乎不会到 1，因此"既持标的被清零"在真实参数下极难触发；
# 为它硬造一组约束会变成在测约束求解器而不是测编排。动作判定本身已被单元用例覆盖。


def test_items_sum_of_target_weights_plus_cash_is_one(store):
    _run(store)

    for plan in store.plans:
        items = [i for i in store.items if i["plan_id"] == plan["plan_id"]]
        securities = sum(i["target_weight"] for i in items)
        assert securities <= 1.0 - CONSTRAINTS.cash_floor + 1e-9


def test_plan_metadata_records_turnover_fee_and_limitations(store):
    _run(store)

    light = next(p for p in store.plans if p["tier"] == "light")
    assert light["turnover_rate"] == pytest.approx(0.070633, abs=1e-6)
    assert light["estimated_fee"] == pytest.approx(54.661572, abs=1e-5)
    assert isinstance(light["limitations"], list)
    assert light["rationale"]


def test_the_fallback_candidate_is_used_when_none_is_supplied(store):
    """不传 candidates 时用确定性回退——保证没有模型也能出方案。

    等权回退的全长换手率只有 0.34，达不到重构档下界 0.50，因此这里只应落库
    微调与中度两档，重构档必须如实报告不可用。
    """
    result = _run(store, candidates=None)

    persisted = {p["tier"] for p in store.plans}
    assert persisted == {"light", "moderate"}
    assert [p.tier.value for p in result.unavailable] == ["rebuild"]
    assert result.unavailable[0].unavailable_reason in {
        "turnover_out_of_band",
        "constraint_repaired",
    }


def test_explicit_candidates_override_the_fallback(store):
    custom = {"110011": 0.30, "510300": 0.50, "518880": 0.15, "cash": 0.05}

    _run(store, candidates={tier: dict(custom) for tier in PlanTier})

    light = next(p for p in store.plans if p["tier"] == "light")
    item = next(
        i for i in store.items
        if i["plan_id"] == light["plan_id"] and i["product_code"] == "518880"
    )
    # 回退候选不会碰 518880（当前没持有、等权会给它 0.3167），自定义候选会给它权重
    assert item["target_weight"] > 0


def test_an_empty_current_portfolio_is_rejected(store):
    with pytest.raises(AllocationError) as excinfo:
        _run(store, current_weights={})

    assert excinfo.value.code == "no_current"
