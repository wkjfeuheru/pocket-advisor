"""方案编排：把一次"给我配置建议"变成落库的方案。

深模块与外部世界的接缝：输入**已经取好**的持仓、商品池与约束，输出已落库的三档方案
（带 ``plan_id``，可被采纳）。取数（持仓接口、商品池）由调用方负责，本模块保持纯确定性。

三条职责边界：

- **转换**：``GeneratedPlan.weights``（含 ``cash``）→ 方案明细行。``cash`` 不是商品、
  没有 ``product_code``，因此**不生成明细行**——它只体现在权重之和里。
- **动作判定**：``open``/``add``/``reduce``/``close``/``hold`` 由"当前 vs 目标权重"决定，
  不由模型给。
- **不落库不可用档**：``available=False`` 的档位只回传给调用方，不写进
  ``allocation_plans``——否则用户会看到一个点不开的空方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from app.agents.portfolio.candidates import CandidateError, build_fallback_candidates
from app.agents.portfolio.plan import (
    GeneratedPlan,
    PlanError,
    PlanGenerationResult,
    PlanTier,
    generate_plans,
)
from app.agents.portfolio.validation import CASH, ValidationConstraints

#: 权重差小于此值视为"不动"，既不生成明细行也不产生费用。
_WEIGHT_EPSILON = 1e-9


class AllocationError(ValueError):
    """方案编排的前置条件不满足。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "invalid_allocation") -> None:
        super().__init__(message)
        self.code = code


class PlanStore(Protocol):
    """方案持久化的窄接口。"""

    def insert_plan(self, plan: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> str: ...

    def get_plan(self, plan_id: str) -> dict | None: ...

    def list_plans(self, customer_id: str, *, limit: int = 50) -> list[dict]: ...


@dataclass
class PersistedPlan:
    """落库后的方案：生成结果 + ``plan_id``。"""

    plan_id: str
    generated: GeneratedPlan

    @property
    def tier(self) -> PlanTier:
        return self.generated.tier


@dataclass
class AllocationResult:
    """一次编排的结果。``persisted`` 只含已落库的可用档位。"""

    persisted: list[PersistedPlan] = field(default_factory=list)
    unavailable: list[GeneratedPlan] = field(default_factory=list)

    @property
    def plans(self) -> list[PersistedPlan]:
        return list(self.persisted)

    @property
    def available(self) -> list[PersistedPlan]:
        return list(self.persisted)


def action_for(current_weight: float, target_weight: float) -> str:
    """按权重变化判定调仓动作。

    残留极小（< 1e-9）视为 0：不生成"持有 0.0000001%"这种没有意义的明细行。
    """
    current = 0.0 if abs(current_weight) < _WEIGHT_EPSILON else float(current_weight)
    target = 0.0 if abs(target_weight) < _WEIGHT_EPSILON else float(target_weight)

    if current == 0.0 and target == 0.0:
        return "hold"
    if current == 0.0:
        return "open"
    if target == 0.0:
        return "close"
    return "add" if target > current else "reduce"


def _build_items(
    *,
    plan_id: str,
    weights: Mapping[str, float],
    current_weights: Mapping[str, float],
    products: Mapping[str, Mapping[str, Any]],
    initial_capital: float,
) -> list[dict[str, Any]]:
    """把权重映射成明细行。现金被跳过（它不是商品）。"""
    items: list[dict[str, Any]] = []
    codes = {code for code in weights if code != CASH} | {
        code for code in current_weights if code != CASH
    }
    for code in sorted(codes):
        current = float(current_weights.get(code, 0.0))
        target = float(weights.get(code, 0.0))
        if abs(target - current) < _WEIGHT_EPSILON and target == 0.0:
            # 本来就没有、目标也没有：不是一条明细
            continue
        product = products.get(code) or {}
        items.append(
            {
                "plan_id": plan_id,
                "product_code": code,
                "product_name": str(product.get("name") or ""),
                "asset_class": str(product.get("type") or ""),
                "action": action_for(current, target),
                "current_weight": current,
                "target_weight": target,
                "delta_weight": target - current,
                "current_amount": current * initial_capital,
                "target_amount": target * initial_capital,
                "reason": "",
            }
        )
    return items


def generate_and_persist(
    store: PlanStore,
    *,
    customer_id: str,
    current_weights: Mapping[str, float],
    products: Mapping[str, Mapping[str, Any]],
    constraints: ValidationConstraints | None = None,
    candidates: Mapping[PlanTier, Mapping[str, float]] | None = None,
    initial_capital: float = 0.0,
    nav_as_of: date | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    fee_buy: float = 0.0015,
    fee_sell: float = 0.005,
    rationale: Mapping[PlanTier, str] | None = None,
) -> AllocationResult:
    """生成三档方案并把可用的落库。

    ``candidates`` 缺省时使用确定性回退（``build_fallback_candidates``），
    因此**没有模型也能产出方案**；有 LLM 时由调用方传入模型给的目标形态。
    """
    current = {
        str(code): float(weight)
        for code, weight in current_weights.items()
        if float(weight) != 0.0
    }
    if not current:
        raise AllocationError("当前持仓为空，无法生成调仓方案", code="no_current")

    rules = constraints or ValidationConstraints()

    if candidates is None:
        try:
            fallback = build_fallback_candidates(
                products=products, current_weights=current, constraints=rules
            )
        except CandidateError as exc:
            raise AllocationError(str(exc), code=exc.code) from exc
        candidates = {tier: dict(fallback) for tier in PlanTier}

    try:
        generated: PlanGenerationResult = generate_plans(
            current_weights=current,
            candidates=candidates,
            products=products,
            constraints=rules,
            initial_capital=initial_capital,
            fee_buy=fee_buy,
            fee_sell=fee_sell,
        )
    except PlanError as exc:
        raise AllocationError(str(exc), code=exc.code) from exc

    result = AllocationResult()
    for plan in generated.plans:
        if not plan.available:
            result.unavailable.append(plan)
            continue

        plan_id = str(uuid4())
        items = _build_items(
            plan_id=plan_id,
            weights=plan.weights,
            current_weights=current,
            products=products,
            initial_capital=initial_capital,
        )
        store.insert_plan(
            {
                "plan_id": plan_id,
                "customer_id": customer_id,
                "request_id": request_id,
                "session_id": session_id,
                "tier": plan.tier.value,
                "status": "offered",
                "turnover_rate": plan.turnover_rate,
                "estimated_fee": plan.estimated_fee,
                "rationale": (rationale or {}).get(plan.tier, f"{plan.label}方案"),
                "repairs": list(plan.repairs),
                "limitations": list(plan.limitations),
                "nav_as_of": nav_as_of,
            },
            items,
        )
        result.persisted.append(PersistedPlan(plan_id=plan_id, generated=plan))

    return result


__all__ = [
    "AllocationError",
    "AllocationResult",
    "PersistedPlan",
    "PlanStore",
    "action_for",
    "generate_and_persist",
]
