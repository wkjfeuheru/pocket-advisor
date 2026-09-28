"""方案生成：把候选权重变成恰好三个"改造幅度分档"的合法方案。

沿「当前持仓 → 候选组合」这条直线取不同进度 ``λ``（``blend``）：

    w(λ) = 当前权重 + λ × (候选权重 - 当前权重)
    turnover(λ) = Σ|w(λ) - 当前权重| / 2 = λ × turnover(1)

每一项都是 ``|λ × d_i|``，所以 ``Σ|Δ|`` 是 λ 的线性函数，换手率与 λ 严格成正比。
**不需要"让模型再试一次"**——重试既贵又不可控，直接按档位选 λ 更可靠。

λ 不是随便取的：档位区间由 ``validation.TurnoverTier`` 定义，生成后由校验器确认落在区间内。
落不进去（候选与当前差别太小，最大换手率本身就够不到该档下界）就把这一档标为
**不可用**，而不是硬塞一个越界方案——否则"三个方案"里会有假的。

方案的合法性校验交给 ``validation.normalize_and_validate``，本模块不重复实现硬约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.agents.portfolio.validation import (
    CASH,
    TurnoverTier,
    ValidationConstraints,
    ValidationError,
    normalize_and_validate,
)

#: 复用换手分档枚举作为方案档位——两者是同一个概念。
PlanTier = TurnoverTier

#: 每档的**目标换手率**。取区间内的代表值，避开边界（边界属于相邻档，容易因浮点
#: 误差掉档）。真正决定权重的是 λ = 目标换手率 / 该候选的最大换手率。
#:
#: 早期实现写死每档的 λ（0.08 / 0.52 / 1.0），在"当前持仓已经很集中"时失效：
#: 候选与当前差 63%，λ=0.08 也会挪动 5% 以上，微调档直接冲出区间上界。
#: 改为按目标换手率反解 λ，就从"猜一个进度"变成"按目标算进度"。
TARGET_TURNOVER: dict[TurnoverTier, float] = {
    TurnoverTier.LIGHT: 0.05,
    TurnoverTier.MODERATE: 0.30,
    TurnoverTier.REBUILD: 0.65,
}


class PlanError(ValueError):
    """生成方案的前置条件不满足。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "invalid_plan_request") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class GeneratedPlan:
    """一个档位的方案。``available=False`` 时 ``weights`` 为空且必须带原因。"""

    tier: TurnoverTier
    weights: dict[str, float] = field(default_factory=dict)
    turnover_rate: float = 0.0
    available: bool = False
    unavailable_reason: str = ""
    estimated_fee: float = 0.0
    max_single_weight: float = 0.0
    concentration: float = 0.0
    repairs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return {"light": "微调", "moderate": "中度", "rebuild": "重构"}[self.tier.value]


@dataclass
class PlanGenerationResult:
    """三个档位的生成结果，始终按 light → moderate → rebuild 排列。"""

    plans: list[GeneratedPlan]

    @property
    def available(self) -> list[GeneratedPlan]:
        return [plan for plan in self.plans if plan.available]

    @property
    def unavailable(self) -> list[GeneratedPlan]:
        return [plan for plan in self.plans if not plan.available]


def _turnover_of(
    current: Mapping[str, float], target: Mapping[str, float]
) -> float:
    """换手率：只比较证券，现金不计入（与 ``validation`` 口径一致）。"""
    codes = {code for code in current if code != CASH} | {
        code for code in target if code != CASH
    }
    delta = sum(
        abs(float(target.get(code, 0.0)) - float(current.get(code, 0.0))) for code in codes
    )
    return delta / 2.0


def _blend(
    current: Mapping[str, float], candidate: Mapping[str, float], lam: float
) -> dict[str, float]:
    """沿直线混合。权重和自动保持为 1（凸组合）。"""
    codes = set(current) | set(candidate)
    return {
        code: float(current.get(code, 0.0))
        + lam * (float(candidate.get(code, 0.0)) - float(current.get(code, 0.0)))
        for code in codes
    }


def _max_feasible_lambda(
    current: Mapping[str, float],
    candidate: Mapping[str, float],
    max_single_weight: float,
    cash_floor: float,
) -> float:
    """从当前走向候选的路上，λ 最大能取到多少仍不违反单标的上限。

    这点很关键：**两端都合法不代表中间过程合法**。例如当前持有 60% 的某只基金、
    候选把它降到 15.9%，路径上会依次经过 55%、50%……全都超过 40% 上限。
    若按目标换手率硬算 λ，得到的方案会被校验器压顶修复，微调档就变成了另一份方案。

    做法：找出每个标的首次触碰上限的 λ 并取最小值。

        A + λ·d = 上限 → λ = (上限 - A) / d

    ``d > 0`` 且 ``A > 上限`` 时永远越界（返回 0）；``d > 0`` 且 A ≤ 上限时约束在后方
    （λ 可以到 1）；``d < 0`` 时只要 A > 上限就有正的上界。

    现金同理受 ``cash_floor`` 约束：路径上现金比例不得低于该值。
    """
    best = 1.0
    codes = set(current) | set(candidate)
    for code in codes:
        start = float(current.get(code, 0.0))
        delta = float(candidate.get(code, 0.0)) - start
        if code == CASH:
            if delta < 0 and start > cash_floor:
                best = min(best, (cash_floor - start) / delta)
            elif delta <= 0 and start < cash_floor - 1e-12:
                return 0.0
            continue
        if delta > 1e-12:
            if start > max_single_weight + 1e-12:
                return 0.0  # 起点就已越界，任何前进都治不好
            if start <= max_single_weight:
                limit = (max_single_weight - start) / delta
                best = min(best, max(0.0, limit))
        elif delta < -1e-12:
            if start > max_single_weight + 1e-12:
                limit = (start - max_single_weight) / -delta
                best = min(best, max(0.0, limit))
    return max(0.0, min(1.0, best))


def _lambda_for_tier(
    current: Mapping[str, float],
    candidate: Mapping[str, float],
    tier: TurnoverTier,
    *,
    max_lambda: float = 1.0,
) -> float | None:
    """反解 λ，使换手率命中该档目标；够不到档位下界时返回 ``None``。

    ``turnover(λ)`` 对 λ 严格线性，因此 ``λ = 目标 / 全长换手率``，再夹到可行上界
    ``max_lambda`` 之内。夹完之后若换手率掉出档位区间，说明这一档在当前持仓与候选之间
    **不存在既满足换手率、又不违反硬约束的方案**——返回 ``None``，由调用方标记不可用。
    """
    full = _turnover_of(current, _blend(current, candidate, 1.0))
    if full <= 1e-12:
        return None
    target = TARGET_TURNOVER[tier]
    lam = min(max_lambda, target / full)
    if lam <= 0.0:
        return None
    achieved = lam * full
    return lam if tier.contains(achieved) else None


def _static_metrics(
    weights: Mapping[str, float],
    *,
    current: Mapping[str, float],
    initial_capital: float,
    fee_buy: float,
    fee_sell: float,
) -> tuple[float, float, float]:
    """预估费用、单标的峰值、集中度（HHI）。现金不计费也不计入集中度。"""
    fee = 0.0
    for code, target in weights.items():
        if code == CASH:
            continue
        delta = float(target) - float(current.get(code, 0.0))
        amount = abs(delta) * initial_capital
        fee += amount * (fee_buy if delta > 0 else fee_sell)

    securities = [float(v) for k, v in weights.items() if k != CASH]
    peak = max(securities) if securities else 0.0
    concentration = sum(value * value for value in securities)
    return fee, peak, concentration


def _unavailable(tier: TurnoverTier, reason: str) -> GeneratedPlan:
    return GeneratedPlan(tier=tier, available=False, unavailable_reason=reason)


def generate_plans(
    *,
    current_weights: Mapping[str, float],
    candidates: Mapping[TurnoverTier, Mapping[str, float]],
    products: Mapping[str, Mapping[str, Any]],
    constraints: ValidationConstraints | None = None,
    initial_capital: float = 0.0,
    fee_buy: float = 0.0015,
    fee_sell: float = 0.005,
) -> PlanGenerationResult:
    """为三个档位各生成一个方案。

    ``candidates`` 是各档的候选目标形态（来自 LLM 或规则）；缺哪一档就把哪一档标为
    ``no_candidate``。``current_weights`` 必须非空——没有当前持仓就没有"调仓"可言。
    """
    current = {
        str(code): float(weight)
        for code, weight in current_weights.items()
        if float(weight) != 0.0
    }
    if not current:
        raise PlanError("当前持仓为空，无法生成调仓方案", code="no_current")

    plans: list[GeneratedPlan] = []
    for tier in (TurnoverTier.LIGHT, TurnoverTier.MODERATE, TurnoverTier.REBUILD):
        candidate = candidates.get(tier)
        if not candidate:
            plans.append(_unavailable(tier, "no_candidate"))
            continue

        rules = constraints or ValidationConstraints()
        max_lambda = _max_feasible_lambda(
            current, candidate, rules.max_single_weight, rules.cash_floor
        )
        lam = _lambda_for_tier(current, candidate, tier, max_lambda=max_lambda)
        if lam is None:
            # 在"当前 → 候选"这条路上，这一档不存在既满足换手率、又不违反硬约束的方案。
            plans.append(_unavailable(tier, "turnover_out_of_band"))
            continue

        blended = _blend(current, candidate, lam)

        try:
            validated = normalize_and_validate(
                {"weights": blended},
                holdings={code: {"weight": weight} for code, weight in current.items()},
                products=products,
                constraints=constraints,
            )
        except ValidationError as exc:
            # 合规问题不可修复：只废掉这一档。
            plans.append(_unavailable(tier, exc.code))
            continue

        if not tier.contains(validated.turnover_rate):
            # 候选与当前差别太小，这一档够不到区间——不硬塞。
            plans.append(_unavailable(tier, "turnover_out_of_band"))
            continue

        if validated.repairs:
            # 校验器动了权重（例如单标的超上限被压顶、余额转现金），
            # 说明这个方案**不满足硬约束**，修复后已经不是同一份方案了。
            # 实测过最坏的情况：微调档被修复后换手率从 5% 变成 52.5%，
            # 却仍然打着"微调"的标签——比直接判不可用危险得多。
            plans.append(_unavailable(tier, "constraint_repaired"))
            continue

        fee, peak, concentration = _static_metrics(
            validated.weights,
            current=current,
            initial_capital=initial_capital,
            fee_buy=fee_buy,
            fee_sell=fee_sell,
        )
        plans.append(
            GeneratedPlan(
                tier=tier,
                weights=dict(validated.weights),
                turnover_rate=validated.turnover_rate,
                available=True,
                estimated_fee=fee,
                max_single_weight=peak,
                concentration=concentration,
                repairs=list(validated.repairs),
                limitations=list(validated.limitations),
            )
        )

    return PlanGenerationResult(plans=plans)


__all__ = [
    "CASH",
    "TARGET_TURNOVER",
    "GeneratedPlan",
    "PlanError",
    "PlanGenerationResult",
    "PlanTier",
    "generate_plans",
]
