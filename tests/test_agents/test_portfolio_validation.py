"""方案归一化、硬约束校验与换手分档。

LLM 只产出候选权重与理由；权重归一、约束执行、换手计算、分档判定全部由这里的
确定性代码负责。任何"模型说这个方案合法"的说法都不成立。

═══════════════════════════════════════════════════════════════════════════
derivation —— 换手率定义与分档边界
═══════════════════════════════════════════════════════════════════════════
turnover_rate = Σ|目标权重 - 当前权重| / 2

    除以 2 的原因：一次换仓里"卖出 20%"与"买入 20%"是同一笔动作的两面，
    不除以 2 会把同一笔交易算两次，且数值范围变成 0–2 而不是 0–1。
    本仓既有口径同样是单边：``app/schemas/portfolio.py`` 的
    ``PositionView.weight`` 是百分数，而方案权重是 0–1 小数，实现里不混用。

    例：当前 A 80% / B 20%，目标 A 60% / B 40%
        |60-80| + |40-20| = 20 + 20 = 40 → turnover = 40 / 2 = 20%

分档（设计文档「换手分档」）：
    light    turnover <  0.15
    moderate 0.15 ≤ turnover ≤ 0.50
    rebuild  turnover >  0.50

═══════════════════════════════════════════════════════════════════════════
derivation —— 现金重分类（关键设计决定）
═══════════════════════════════════════════════════════════════════════════
现金不参与换手率计算，而是作为**残余项**单独满足下限。

    理由：把现金塞进换手率会让"降仓避险"被计成极高换手。
    例：当前 100% 股票、目标 100% 现金。若把现金算进去，
        turnover = 100%/2 = 50%（重构档）；但它其实只是一次卖出，没有买入。

    做法：先按"可用于证券的权重"归一化模型的证券候选，再把差额留给现金；
    若由此得到的现金权重低于 ``cash_floor``，则按比例削减证券持仓以满足下限。

═══════════════════════════════════════════════════════════════════════════
derivation —— 单标的上限的修复而非拒绝
═══════════════════════════════════════════════════════════════════════════
超限时把该标的压到上限，超出的权重按其余标的的现有比例分摊；若没有其余标的，
则全部归入现金，并在 ``repairs`` 里记录。

    例：上限 0.30，候选 {A: 0.90, B: 0.10}
        A 压到 0.30，超出 0.60
        B 只有 0.10，按比例分不完 → 0.30 + 0.10 = 0.40 < 1.0
        余下 0.60 归现金
        结果 {A: 0.30, B: 0.10, cash: 0.60}

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import pytest

from app.agents.portfolio.validation import (
    TurnoverTier,
    ValidationConstraints,
    ValidationError,
    normalize_and_validate,
)

CONSTRAINTS = ValidationConstraints(
    max_single_weight=0.70,
    cash_floor=0.05,
    max_risk_level="R3",
)


def _products():
    return {
        "110011": {"name": "易方达中小盘混合", "type": "fund", "risk_level": "R3", "priceable": True},
        "510300": {"name": "华泰柏瑞沪深300ETF", "type": "etf", "risk_level": "R3", "priceable": True},
        "518880": {"name": "华安黄金ETF", "type": "etf", "risk_level": "R2", "priceable": True},
        "161725": {"name": "招商中证白酒指数A", "type": "fund", "risk_level": "R4", "priceable": True},
        "WM2026001": {"name": "示例现金管理理财", "type": "wealth", "risk_level": "R1", "priceable": False},
    }


def _holdings():
    return {
        "110011": {"shares": 800.0, "market_value": 800.0, "weight": 0.80},
        "510300": {"shares": 200.0, "market_value": 200.0, "weight": 0.20},
    }


def _validate(candidate, **overrides):
    kwargs = {
        "holdings": _holdings(),
        "products": _products(),
        "constraints": CONSTRAINTS,
    }
    kwargs.update(overrides)
    return normalize_and_validate(candidate, **kwargs)


# cash_floor = 0.05，因此证券权重之和只能是 0.95；max_single_weight = 0.70。
# 每个用例的期望值都先按这个预算算一遍，不是照抄实现。

def test_turnover_is_half_the_absolute_weight_change_and_lands_in_the_right_tier():
    # 候选 A 65% : B 30% → Ω = 0.95，证券预算 0.95 → A 0.65 / B 0.30 / cash 0.05
    # 换手：|0.65-0.80| + |0.30-0.20| = 0.15 + 0.10 = 0.25 → 0.25/2 = 0.125
    result = _validate({"weights": {"110011": 0.65, "510300": 0.30}})

    assert result.weights["110011"] == pytest.approx(0.65, abs=1e-9)
    assert result.weights["510300"] == pytest.approx(0.30, abs=1e-9)
    assert result.weights["cash"] == pytest.approx(0.05, abs=1e-9)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert result.turnover_rate == pytest.approx(0.125, abs=1e-9)
    assert result.tier is TurnoverTier.LIGHT


def test_a_moderate_plan_lands_in_the_moderate_tier():
    # 候选 A 65% : B 30% → Ω = 0.95 → A 0.65 / B 0.30 / cash 0.05（证券预算正好用满）
    # 当前 80/20 → 换手：|0.65-0.80| + |0.30-0.20| = 0.15 + 0.10 = 0.25 → 0.125（light）
    # 要进 moderate 需要 turnover > 0.15，即 Σ|Δ| > 0.30：
    #   取 A 0.50 / B 0.45 → |0.50-0.80| + |0.45-0.20| = 0.30 + 0.25 = 0.55 → 0.275
    #   但这要求 Ω = 0.95 且比例 A:B = 50:45 → A 0.50 / B 0.45，均未超 0.70 上限
    result = _validate({"weights": {"110011": 0.50, "510300": 0.45}})

    assert result.weights["110011"] == pytest.approx(0.50, abs=1e-9)
    assert result.weights["510300"] == pytest.approx(0.45, abs=1e-9)
    assert result.turnover_rate == pytest.approx(0.275, abs=1e-9)
    assert result.tier is TurnoverTier.MODERATE


def test_a_large_rotation_is_a_rebuild_plan():
    # 候选 B 95% → B 0.95，超 0.70 上限 → 被压到 0.70，其余进现金
    # 换手：|0-0.80| + |0.70-0.20| = 0.80 + 0.50 = 1.30 → 0.65 → rebuild
    result = _validate({"weights": {"510300": 0.95}})

    assert result.weights["510300"] == pytest.approx(0.70, abs=1e-9)
    assert result.tier is TurnoverTier.REBUILD
    assert any("max_single_weight" in note for note in result.repairs)


def test_single_holding_cap_is_repaired_by_trimming_and_parking_the_rest_in_cash():
    # 候选 A 95% → 归一后 A 0.95，超 0.70 上限，且没有其他标的可分摊
    # → A 0.70 / cash 0.30
    result = _validate({"weights": {"110011": 0.95}})

    assert result.weights["110011"] == pytest.approx(0.70, abs=1e-9)
    assert result.weights["cash"] == pytest.approx(0.30, abs=1e-9)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert any("max_single_weight" in note for note in result.repairs)


def test_a_suffixed_risk_level_above_the_profile_is_rejected():
    products = _products()
    products["161725"] = {**products["161725"], "risk_level": "R4 中高风险"}

    with pytest.raises(ValidationError) as excinfo:
        _validate({"weights": {"110011": 0.50, "161725": 0.50}}, products=products)

    assert excinfo.value.code == "risk_exceeded"


def test_an_unrecognized_risk_level_is_rejected():
    products = _products()
    products["110011"] = {**products["110011"], "risk_level": "未披露"}

    with pytest.raises(ValidationError) as excinfo:
        _validate({"weights": {"110011": 1.0}}, products=products)

    assert excinfo.value.code == "risk_exceeded"


def test_a_product_above_the_profile_risk_level_is_rejected_not_repaired():
    # 161725 是 R4，用户上限 R3：这不是"修一下就行"，必须整体拒绝。
    with pytest.raises(ValidationError) as excinfo:
        _validate({"weights": {"110011": 0.50, "161725": 0.50}})

    assert excinfo.value.code == "risk_exceeded"
    assert "161725" in str(excinfo.value)


def test_an_unpriceable_holding_may_be_kept_but_never_increased():
    # WM2026001 不可定价：持有可以，新增不行。
    holdings = dict(_holdings())
    holdings["WM2026001"] = {"shares": 100.0, "market_value": 100.0, "weight": 0.10}

    with pytest.raises(ValidationError) as excinfo:
        _validate({"weights": {"WM2026001": 0.20, "110011": 0.80}}, holdings=holdings)

    assert excinfo.value.code == "unpriceable_increase"
    assert "WM2026001" in str(excinfo.value)
