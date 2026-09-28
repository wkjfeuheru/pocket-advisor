"""候选生成回退：不依赖模型的"目标形态"。

``generate_plans`` 需要各档的候选组合。有 LLM 时由模型给；**没有模型时必须有确定性回退**，
否则三档全 ``no_candidate``，整条链路跑不出任何方案（验收一就无从谈起）。

回退策略：在"风险等级不越界且可定价"的标的里等权配置，单标的超上限的部分转入现金。
这是一个刻意朴素的目标——它的作用是给出一个**明确不同于当前持仓**的形态，
让 λ 混合能真正落在三个档位里；它不承担"选得好"的责任，那是 LLM 那一层的事。

═══════════════════════════════════════════════════════════════════════════
derivation —— 等权 + 上限 + 现金
═══════════════════════════════════════════════════════════════════════════
池（R3 上限）：A R3、B R2、C R3、D R4(越界)、E 不可定价

    合格标的 = A, B, C（D 越风险等级、E 不可定价，都被剔除）
    证券预算 = 1 - cash_floor = 0.95
    等权 = 0.95 / 3 = 0.3166667

    若 max_single_weight = 0.25，则每个压到 0.25：
        证券合计 = 0.75，现金 = 1 - 0.75 = 0.25
    （不是 0.05——现金是残余项，被压顶后自然变大）

    若 max_single_weight = 0.40，则 0.3166667 不超限：
        A 0.3166667 / B 0.3166667 / C 0.3166667 / cash 0.05

无合格标的时抛错——没有可推荐的标的不是"空方案"，是配置建议本身不成立。

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import pytest

from app.agents.portfolio.candidates import (
    CandidateError,
    build_fallback_candidates,
)
from app.agents.portfolio.validation import CASH, ValidationConstraints

CONSTRAINTS = ValidationConstraints(
    max_single_weight=0.40,
    cash_floor=0.05,
    max_risk_level="R3",
)

POOL = {
    "A": {"name": "债基", "type": "fund", "risk_level": "R3", "priceable": True},
    "B": {"name": "宽基ETF", "type": "etf", "risk_level": "R2", "priceable": True},
    "C": {"name": "混合基金", "type": "fund", "risk_level": "R3", "priceable": True},
    "D": {"name": "白酒指数", "type": "fund", "risk_level": "R4", "priceable": True},
    "E": {"name": "示例理财", "type": "wealth", "risk_level": "R1", "priceable": False},
}

CURRENT = {"A": 0.80, "B": 0.20}


def _fallback(**overrides):
    kwargs = {"products": POOL, "current_weights": CURRENT, "constraints": CONSTRAINTS}
    kwargs.update(overrides)
    return build_fallback_candidates(**kwargs)


def test_equal_weight_over_eligible_products_only():
    candidate = _fallback()

    # D 越风险等级、E 不可定价 → 只剩 A/B/C
    assert set(candidate) == {"A", "B", "C", CASH}
    share = 0.95 / 3
    assert candidate["A"] == pytest.approx(share)
    assert candidate["B"] == pytest.approx(share)
    assert candidate["C"] == pytest.approx(share)
    assert candidate[CASH] == pytest.approx(0.05)
    assert sum(candidate.values()) == pytest.approx(1.0)


def test_suffixed_and_unrecognized_risk_levels_are_excluded():
    pool = dict(POOL)
    pool["D"] = {**pool["D"], "risk_level": "R4 中高风险"}
    pool["F"] = {"name": "未披露", "type": "fund", "risk_level": "未披露", "priceable": True}

    candidate = _fallback(products=pool)

    assert "D" not in candidate
    assert "F" not in candidate
    assert set(candidate) == {"A", "B", "C", CASH}


def test_a_product_above_the_profile_risk_level_is_excluded():
    candidate = _fallback()

    assert "D" not in candidate


def test_an_unpriceable_product_is_excluded():
    candidate = _fallback()

    assert "E" not in candidate


def test_the_single_weight_cap_pushes_the_excess_into_cash():
    tight = ValidationConstraints(
        max_single_weight=0.25, cash_floor=0.05, max_risk_level="R3"
    )

    candidate = _fallback(constraints=tight)

    assert candidate["A"] == pytest.approx(0.25)
    assert candidate["B"] == pytest.approx(0.25)
    assert candidate["C"] == pytest.approx(0.25)
    # 残余项：被压顶后现金自然变大，不是 0.05
    assert candidate[CASH] == pytest.approx(0.25)
    assert sum(candidate.values()) == pytest.approx(1.0)


def test_no_eligible_product_raises_instead_of_returning_a_cash_only_plan():
    only_risky = {"D": POOL["D"], "E": POOL["E"]}

    with pytest.raises(CandidateError) as excinfo:
        _fallback(products=only_risky)

    assert excinfo.value.code == "no_eligible_product"


def test_the_result_differs_from_the_current_portfolio_when_current_is_concentrated():
    """回退候选必须能与当前持仓拉开差距，让重构档真的可用。

    注意：当前持仓单标的 95%、上限 40% 时，微调/中度档**本就不可能同时满足
    "换手率落在本档"与"单标的不超限"**——任何近距离混合都仍持有超限的 A。
    这种情况必须显式判不可用（``constraint_repaired``），而不是靠校验器压顶
    把微调档变成实际换手 52% 的另一份方案。
    """
    from app.agents.portfolio.plan import PlanTier, generate_plans

    current = {"A": 0.95, "B": 0.05}
    candidate = _fallback(current_weights=current)

    result = generate_plans(
        current_weights=current,
        candidates={tier: dict(candidate) for tier in PlanTier},
        products=POOL,
        constraints=CONSTRAINTS,
    )

    by_tier = {plan.tier: plan for plan in result.plans}
    # 重构档可行：全量换到等权组合
    rebuild = by_tier[PlanTier.REBUILD]
    assert rebuild.available is True
    assert rebuild.turnover_rate > 0.50

    # 微调/中度不可行，且原因必须说清楚，不能悄悄换成一个越档方案。
    # 具体是哪条规则拦下的（换手率越界 vs 校验器被迫修复）取决于中间权重，
    # 两者都表示"这一档在当前持仓下不成立"，所以只断言"不可用且带原因"。
    for tier in (PlanTier.LIGHT, PlanTier.MODERATE):
        plan = by_tier[tier]
        assert plan.available is False
        assert plan.unavailable_reason in {"turnover_out_of_band", "constraint_repaired"}
        assert plan.weights == {}


def test_a_current_holding_whose_risk_is_too_high_is_still_reported_not_recommended():
    """用户已持有的越界标的：既不推荐新增，也不该让整个回退失败。"""
    candidate = _fallback(current_weights={"A": 0.50, "D": 0.50})

    assert "D" not in candidate
    assert set(candidate) == {"A", "B", "C", CASH}
