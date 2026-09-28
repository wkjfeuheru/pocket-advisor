"""候选生成回退：不依赖模型的"目标形态"。

``generate_plans`` 需要各档的候选组合。有 LLM 时由模型给；**没有模型时必须有确定性回退**，
否则三档全 ``no_candidate``，整条链路跑不出任何方案。

策略：在"风险等级不越界且可定价"的标的里等权配置，单标的超上限的部分留给现金。
这是刻意朴素的目标——它的职责是提供一个**明确不同于当前持仓**的形态，让 λ 混合能真正
落进三个档位；"选得好"是 LLM 那一层的事，不是这里的责任。

注意：被剔除的标的**不报错**（用户可能已经持有一只风险等级超标的基金，那是既成事实）；
只有当**一个合格标的都没有**时才抛错——那时不是"空方案"，而是配置建议本身不成立。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.agents.portfolio.validation import (
    CASH,
    ValidationConstraints,
    risk_index,
)


class CandidateError(ValueError):
    """无法构造候选组合。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "no_candidate_pool") -> None:
        super().__init__(message)
        self.code = code


def eligible_products(
    products: Mapping[str, Mapping[str, Any]],
    constraints: ValidationConstraints,
) -> list[str]:
    """风险等级不越界且可定价的标的代码，按代码排序（保证可复现）。"""
    limit = risk_index(constraints.max_risk_level)
    chosen: list[str] = []
    for code in sorted(products):
        product = products[code] or {}
        if not product.get("priceable", True):
            continue
        product_risk = risk_index(product.get("risk_level"))
        if limit is None or product_risk is None or product_risk > limit:
            continue
        chosen.append(str(code))
    return chosen


def build_fallback_candidates(
    *,
    products: Mapping[str, Mapping[str, Any]],
    current_weights: Mapping[str, float],
    constraints: ValidationConstraints | None = None,
) -> dict[str, float]:
    """等权候选：``{code: weight, ..., cash: weight}``，权重和恒为 1。

    ``current_weights`` 目前不参与计算，保留在签名里是为了让调用点统一——
    将来若要"只在现有持仓里调整"这类更保守的目标形态，可以从这里接。
    """
    rules = constraints or ValidationConstraints()
    codes = eligible_products(products, rules)
    if not codes:
        raise CandidateError(
            "没有符合风险等级且可定价的标的，无法给出配置建议",
            code="no_eligible_product",
        )

    security_budget = max(0.0, 1.0 - rules.cash_floor)
    share = security_budget / len(codes)
    capped = min(share, rules.max_single_weight)

    weights = {code: capped for code in codes}
    # 现金是残余项：单标的被压顶后，未被证券占用的部分自然归现金。
    weights[CASH] = 1.0 - sum(weights.values())
    return weights


__all__ = [
    "CandidateError",
    "build_fallback_candidates",
    "eligible_products",
]
