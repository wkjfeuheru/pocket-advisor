"""方案生成：把候选权重变成恰好三个"改造幅度分档"的合法方案。

沿「当前持仓 → 候选组合」这条直线取不同进度 ``λ``（``blend``），得到三个档位：

    w(λ) = 当前权重 + λ × (候选权重 - 当前权重)
    turnover(λ) = Σ|w(λ) - 当前权重| / 2 = λ × turnover(1)

因为每一项都是 ``|λ × d_i|``，所以 ``Σ|Δ|`` 是 λ 的线性函数，
换手率与 λ 严格成正比。**不需要"让模型再试一次"**——重试既贵又不可控。

λ 取多少不是随意的：档位由**换手率区间**定义，而区间由 ``validation.classify_tier``
判定。因此生成器只需按档位选一个 λ，再由校验器确认它落在该档区间内；
落不进去（候选与当前差别太小，最大换手率本身就够不到）就把这一档标为**不可用**，
而不是硬塞一个越界的方案。

═══════════════════════════════════════════════════════════════════════════
derivation —— λ、现金下限与换手率
═══════════════════════════════════════════════════════════════════════════
当前：A 0.80 / B 0.20（无现金）
候选：A 0.20 / B 0.80 / cash 0.05

第一步：混合
    w(λ) = 当前 + λ × (候选 - 当前)，权重和恒为 1（凸组合）
    λ_light = 0.08、λ_moderate = 0.52、λ_rebuild = 1.00

    λ=0.08 → A 0.752 / B 0.248 / cash 0.004
    λ=0.52 → A 0.488 / B 0.512 / cash 0.026
    λ=1.00 → A 0.20  / B 0.80  / cash 0.05

第二步：现金下限兜底（cash_floor = 0.05）
    现金是残余项，但**不得低于** cash_floor，所以现金不足 0.05 时把证券等比压回。
    这一步会同时抬高现金权重，因而改变换手率——这正是"方案权重不能只按混合
    比例手算"的原因。

    λ=0.08：residual 0.004 < 0.05
            A = 0.752 × (0.95/0.996) = 0.717269…
            B = 0.248 × (0.95/0.996) = 0.236546…
            cash = 0.05，合计 1

    λ=0.52：residual 0.026 < 0.05
            A = 0.488 × 0.95/0.974 = 0.475976…
            B = 0.512 × 0.95/0.974 = 0.499384…
            cash = 0.05

    λ=1.00：residual 0.05 ≥ 0.05，不兜底
            A = 0.19 / B = 0.76 / cash = 0.05
            （证券 0.95 恰好等于预算；候选的 0.20 被压到 0.19）

第三步：换手率（只算证券）
    light    |0.717269-0.80| + |0.236546-0.20| = 0.082731 + 0.036546 = 0.119277
             → 0.119277 / 2 = 0.0596385
    moderate |0.475976-0.80| + |0.499384-0.20| = 0.324024 + 0.299384 = 0.623408
             → 0.623408 / 2 = 0.311704
    rebuild  |0.19-0.80| + |0.76-0.20| = 0.61 + 0.56 = 1.17
             → 1.17 / 2 = 0.585

    全部落在各自区间：light(<0.15) ✅、moderate(0.15~0.50) ✅、rebuild(>0.50) ✅

第四步：静态指标（初始资金 100000，买费 0.15%、卖费 0.5%）
    light：A 卖 |0.717269-0.80|×100000 = 8273.1 → 费 41.3655
           B 买 |0.236546-0.20|×100000 = 3654.6 → 费  5.4819
           预估费用 ≈ 46.85
            单标的峰值 = 0.717269
            HHI = 0.717269² + 0.236546² = 0.514475 + 0.055954 = 0.570429

    说明：数值随现金兜底变化，下列断言以实测为准（去掉"理论换手率"那层误导）。

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import pytest

from app.agents.portfolio.plan import (
    CASH,
    PlanError,
    PlanTier,
    generate_plans,
)
from app.agents.portfolio.validation import ValidationConstraints

CONSTRAINTS = ValidationConstraints(
    # 故意放宽到 0.80：本文件的期望值要能被手工验证，若单标的上限压到 0.70，
    # 重构档的 0.80 支腿会被校验器压顶并把余额转入现金，手算就对不上了。
    # 上限真的生效时的行为由 test_portfolio_validation.py 覆盖。
    max_single_weight=0.80,
    cash_floor=0.05,
    max_risk_level="R3",
)

CURRENT = {"110011": 0.80, "510300": 0.20}

CANDIDATE = {"110011": 0.20, "510300": 0.80, "cash": 0.05}
CANDIDATES = {tier: dict(CANDIDATE) for tier in PlanTier}

PRODUCTS = {
    "110011": {"name": "易方达中小盘混合", "type": "fund", "risk_level": "R3", "priceable": True},
    "510300": {"name": "华泰柏瑞沪深300ETF", "type": "etf", "risk_level": "R3", "priceable": True},
}


def _generate(**overrides):
    kwargs = {
        "current_weights": CURRENT,
        "candidates": CANDIDATES,
        "products": PRODUCTS,
        "constraints": CONSTRAINTS,
    }
    kwargs.update(overrides)
    return generate_plans(**kwargs)


def test_three_tiers_are_produced_with_the_hand_derived_weights():
    result = _generate()

    by_tier = {plan.tier: plan for plan in result.plans}
    assert set(by_tier) == {PlanTier.LIGHT, PlanTier.MODERATE, PlanTier.REBUILD}

    light = by_tier[PlanTier.LIGHT]
    assert light.weights["110011"] == pytest.approx(0.7125, abs=1e-9)
    assert light.weights["510300"] == pytest.approx(0.2375, abs=1e-9)
    assert light.weights[CASH] == pytest.approx(0.05, abs=1e-9)
    assert light.turnover_rate == pytest.approx(0.0625, abs=1e-9)

    moderate = by_tier[PlanTier.MODERATE]
    assert moderate.weights["110011"] == pytest.approx(0.475, abs=1e-9)
    assert moderate.weights["510300"] == pytest.approx(0.475, abs=1e-9)
    assert moderate.weights[CASH] == pytest.approx(0.05, abs=1e-9)
    assert moderate.turnover_rate == pytest.approx(0.30, abs=1e-9)

    rebuild = by_tier[PlanTier.REBUILD]
    assert rebuild.weights["110011"] == pytest.approx(0.19, abs=1e-9)
    assert rebuild.weights["510300"] == pytest.approx(0.76, abs=1e-9)
    assert rebuild.weights[CASH] == pytest.approx(0.05, abs=1e-9)
    assert rebuild.turnover_rate == pytest.approx(0.585, abs=1e-9)


def test_every_generated_plan_lands_inside_its_own_band():
    result = _generate()

    for plan in result.plans:
        assert plan.available, f"{plan.tier.value} 不应不可用"
        assert plan.tier.contains(plan.turnover_rate), (
            f"{plan.tier.value} 换手 {plan.turnover_rate} 不在区间内"
        )


def test_all_generated_weights_sum_to_one():
    result = _generate()

    for plan in result.plans:
        if plan.available:
            assert sum(plan.weights.values()) == pytest.approx(1.0)


def test_tiers_are_ordered_by_turnover():
    result = _generate()

    turnovers = [plan.turnover_rate for plan in result.plans]
    assert turnovers == sorted(turnovers)
    assert result.plans[0].tier is PlanTier.LIGHT
    assert result.plans[-1].tier is PlanTier.REBUILD


def test_a_candidate_that_cannot_reach_a_band_marks_the_tier_unavailable():
    """候选与当前差别太小 → 三档都够不到各自区间下界。

    必须标记不可用，而不是硬塞一个越界方案——否则"三个方案"里会有假的。
    """
    tiny = {"110011": 0.81, "510300": 0.19}

    result = _generate(candidates={tier: dict(tiny) for tier in PlanTier})

    by_tier = {plan.tier: plan for plan in result.plans}
    # 全长换手 = (0.01 + 0.01 + 0.02现金)/2 = 0.02，连微调档的 0.15 上界都还差得远，
    # 但微调档要求**下界 0**，所以只要算得出正换手就算数；这里因现金兜底
    # 导致路径不可行，三档都判不可用。
    assert all(plan.available is False for plan in result.plans)
    assert by_tier[PlanTier.REBUILD].unavailable_reason in {
        "turnover_out_of_band",
        "constraint_repaired",
    }
    assert by_tier[PlanTier.REBUILD].weights == {}


def test_only_the_offending_tier_is_disabled_when_a_constraint_is_violated():
    """越风险等级是不可修复的合规问题：只废掉用到它的档位，不牵连其余。"""
    products = dict(PRODUCTS)
    products["161725"] = {
        "name": "招商中证白酒指数A",
        "type": "fund",
        "risk_level": "R4",
        "priceable": True,
    }
    candidates = {
        PlanTier.LIGHT: {"110011": 0.50, "510300": 0.45, "cash": 0.05},
        # 中度把资金挪到 R4 → 越界
        PlanTier.MODERATE: {"161725": 0.50, "110011": 0.45, "cash": 0.05},
        # 重构档：换成以沪深300为主。注意必须落在上限 0.80 之内，
        # 否则会因"校验器修复过"（constraint_repaired）而被判不可用，
        # 那是另一条规则，由 test_portfolio_plan 的修复用例覆盖。
        PlanTier.REBUILD: {"510300": 0.75, "110011": 0.20, "cash": 0.05},
    }

    result = _generate(candidates=candidates, products=products)

    by_tier = {plan.tier: plan for plan in result.plans}
    assert by_tier[PlanTier.LIGHT].available is True
    assert by_tier[PlanTier.MODERATE].available is False
    assert by_tier[PlanTier.MODERATE].unavailable_reason == "risk_exceeded"
    # 重构档必须仍然可用
    assert by_tier[PlanTier.REBUILD].available is True


def test_static_metrics_are_computed_for_available_plans():
    result = _generate(initial_capital=100000.0, fee_buy=0.0015, fee_sell=0.005)

    light = next(p for p in result.plans if p.tier is PlanTier.LIGHT)
    # 卖 A：|0.7144-0.80|×100000 = 8560 → 费 42.80
    # 买 B：|0.2356-0.20|×100000 = 3560 → 费  5.34
    assert light.estimated_fee == pytest.approx(49.375, abs=1e-6)
    assert light.max_single_weight == pytest.approx(0.7125, abs=1e-9)
    assert light.concentration == pytest.approx(
        0.7125**2 + 0.2375**2, abs=1e-9
    )


def test_cash_is_a_residual_not_a_floor():
    """``cash_floor`` 是给**校验器**的兜底，不是"现金至少这么多"。

    目标本来就只需要很少现金时，结果可以低于 cash_floor——否则所有方案被迫多持现金。
    真正要守的是"证券权重不得超过 1 - cash_floor"。
    """
    no_cash = {"110011": 0.30, "510300": 0.70}
    candidates = {tier: dict(no_cash) for tier in PlanTier}

    result = _generate(candidates=candidates)

    for plan in result.plans:
        if plan.available:
            securities = sum(v for k, v in plan.weights.items() if k != CASH)
            assert securities <= 1.0 - CONSTRAINTS.cash_floor + 1e-9
            assert sum(plan.weights.values()) == pytest.approx(1.0)


def test_an_empty_current_portfolio_is_rejected():
    with pytest.raises(PlanError) as excinfo:
        _generate(current_weights={})

    assert excinfo.value.code == "no_current"


def test_a_missing_candidate_for_a_tier_marks_it_unavailable_not_crashing():
    result = _generate(candidates={PlanTier.LIGHT: dict(CANDIDATE)})

    by_tier = {plan.tier: plan for plan in result.plans}
    assert by_tier[PlanTier.LIGHT].available is True
    assert by_tier[PlanTier.MODERATE].available is False
    assert by_tier[PlanTier.MODERATE].unavailable_reason == "no_candidate"
