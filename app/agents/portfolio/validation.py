"""方案归一化、硬约束校验与换手分档。

LLM 只产出候选权重与理由。权重归一、约束执行、换手计算、分档判定全部由这里的
确定性代码负责——模型无法通过"声称合法"让一个坏方案通过。

三条关键约定（推导见 ``tests/test_agents/test_portfolio_validation.py`` 文件头）：

1. ``turnover_rate = Σ|目标权重 - 当前权重| / 2``。除以 2 是因为卖出一笔与买入一笔
   是同一动作的两面，不除会把同一笔交易算两次。
2. 现金不参与换手率，而是作为残余项单独满足下限；否则"降仓避险"会被计成极高换手。
3. 单标的上限**修复**（压到上限、余额转现金并记录），风险等级越界与不可定价标的新增
   **拒绝**——前者是数值问题，后者是合规问题，不能靠"改一改"绕过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

#: 风险等级由低到高。用于比较用户风险上限与产品风险等级。
RISK_ORDER: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")

_RISK_LEVEL = re.compile(r"R([1-5])(?!\d)", re.IGNORECASE)

CASH = "cash"


class ValidationError(ValueError):
    """方案违反硬约束且不可修复。``code`` 便于上层区分处理。"""

    def __init__(self, message: str, *, code: str = "invalid_plan") -> None:
        super().__init__(message)
        self.code = code


class TurnoverTier(str, Enum):
    """改造幅度档位。

    区间就是档位的**定义**，不是展示用的标签——生成器按它判定方案是否合法，
    因此放在枚举本身上，避免"分档"与"校验"两处各写一套边界。
    """

    LIGHT = "light"
    MODERATE = "moderate"
    REBUILD = "rebuild"

    @property
    def lower(self) -> float:
        """区间下界（含）。``light`` 无下界。"""
        return {"light": 0.0, "moderate": LIGHT_BELOW, "rebuild": MODERATE_UP_TO}[self.value]

    @property
    def upper(self) -> float | None:
        """区间上界（含）。``rebuild`` 无上界（实际受候选最大换手率限制）。"""
        return {"light": LIGHT_BELOW, "moderate": MODERATE_UP_TO, "rebuild": None}[self.value]

    def contains(self, turnover_rate: float) -> bool:
        """换手率是否落在本档区间内。"""
        if turnover_rate < self.lower - 1e-12:
            return False
        upper = self.upper
        return True if upper is None else turnover_rate <= upper + 1e-12


#: 分档边界：``light`` 上界（不含）与 ``moderate`` 上界（含）。
LIGHT_BELOW = 0.15
MODERATE_UP_TO = 0.50


@dataclass(frozen=True)
class ValidationConstraints:
    """一次校验用到的硬约束。"""

    max_single_weight: float = 0.30
    cash_floor: float = 0.05
    max_risk_level: str = "R3"
    #: 允许的最小成交权重增量；低于此值的变化视为噪声，不计入换手。
    min_trade_weight: float = 0.0


@dataclass
class ValidatedPlan:
    """校验并归一后的方案。``weights`` 含 ``cash`` 且总和为 1。"""

    weights: dict[str, float]
    current_weights: dict[str, float]
    turnover_rate: float
    tier: TurnoverTier
    repairs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def tier_label(self) -> str:
        return {"light": "微调", "moderate": "中度", "rebuild": "重构"}[self.tier.value]


def risk_index(level: object) -> int | None:
    """取出 ``R1``–``R5`` 的下标。``R3 中风险`` 这类带说明的串也能识别。

    无法识别返回 ``None``。调用方必须把它当成不合格，不能当成最低风险放行。
    """
    match = _RISK_LEVEL.search(str(level or ""))
    if match is None:
        return None
    return int(match.group(1)) - 1


def classify_tier(turnover_rate: float) -> TurnoverTier:
    """按换手率分档。边界归属：0.15 与 0.50 都算 ``moderate``。"""
    for tier in (TurnoverTier.LIGHT, TurnoverTier.MODERATE):
        if tier.contains(turnover_rate):
            return tier
    return TurnoverTier.REBUILD


def _weight_of(holdings: Mapping[str, Mapping[str, Any]], code: str) -> float:
    row = holdings.get(code) or {}
    value = row.get("weight")
    return float(value) if value is not None else 0.0


def normalize_and_validate(
    candidate: Mapping[str, Any],
    *,
    holdings: Mapping[str, Mapping[str, Any]],
    products: Mapping[str, Mapping[str, Any]],
    constraints: ValidationConstraints | None = None,
) -> ValidatedPlan:
    """归一化候选权重、执行硬约束、计算换手率与档位。

    ``candidate`` 形状为 ``{"weights": {code: weight, ...}}``；权重可以是任意正数
    （由本函数归一到可用证券权重之和为 1），现金由校验器补足。
    """
    rules = constraints or ValidationConstraints()
    raw = candidate.get("weights") if isinstance(candidate, Mapping) else None
    if not isinstance(raw, Mapping) or not raw:
        raise ValidationError("方案缺少 weights", code="missing_weights")

    limitations: list[str] = []
    candidate_weights: dict[str, float] = {}
    for code, value in raw.items():
        code = str(code)
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{code} 的权重不是数字: {value!r}", code="bad_weight") from exc
        if weight < 0:
            raise ValidationError(f"{code} 的权重为负: {weight!r}", code="negative_weight")
        if weight == 0:
            continue
        candidate_weights[code] = weight

    if not candidate_weights:
        raise ValidationError("方案的 weights 全为 0", code="missing_weights")

    # ── 硬约束：标的必须存在、风险等级不得越界 ──────────────────────────
    # 现金是残余项，不是可交易商品：不查商品池、不查风险等级、不可定价规则也不适用于它。
    profile_limit = risk_index(rules.max_risk_level)
    for code in candidate_weights:
        if code == CASH:
            continue
        product = products.get(code)
        if product is None:
            raise ValidationError(f"标的不在可售商品池中: {code}", code="unknown_product")
        if not product.get("priceable", True):
            # 不可定价的标的分两种：已在持仓里的只能持有或减少（下面的专门检查），
            # 全新的则一律不能买——没有净值就无法成交，也无法估值。
            if code not in holdings:
                raise ValidationError(
                    f"标的无日频净值，不可新增: {code}", code="unpriceable"
                )
        product_risk = risk_index(product.get("risk_level"))
        if profile_limit is None or product_risk is None or product_risk > profile_limit:
            raise ValidationError(
                f"{code} 风险等级 {product.get('risk_level')} 超出用户上限 {rules.max_risk_level}",
                code="risk_exceeded",
            )

    # ── 不可定价的存量持仓：可持有、可减少，不得增加 ────────────────────
    for code, row in holdings.items():
        product = products.get(code) or {}
        if product.get("priceable", True):
            continue
        target = candidate_weights.get(code, 0.0)
        current = _weight_of(holdings, code)
        if target > current + 1e-9:
            raise ValidationError(
                f"{code} 无日频净值，只能持有或减少，不能新增（当前 {current:.4f}，目标 {target:.4f}）",
                code="unpriceable_increase",
            )

    # ── 归一化：证券权重和为 1 - cash_floor ─────────────────────────────
    total = sum(candidate_weights.values())
    security_budget = max(0.0, 1.0 - rules.cash_floor)
    # ── 归一化：证券权重落在 (1 - cash_floor) 的预算内 ──────────────────
    # 候选**已经归一**（含现金的和为 1）时原样保留：再规范化会把证券按
    # ``1 - cash`` 二次缩放，把 0.95 的持仓压成 0.3167，进而触发上限修复，
    # 微调档的换手率会从 5% 暴涨到 50%（实测）。
    explicit_cash = CASH in candidate_weights
    security_budget = max(0.0, 1.0 - rules.cash_floor)
    securities = {code: v for code, v in candidate_weights.items() if code != CASH}
    total = sum(securities.values())
    declared_total = total + (candidate_weights[CASH] if explicit_cash else 0.0)

    if total <= 0:
        weights = {}
    elif explicit_cash and abs(declared_total - 1.0) <= 1e-6:
        # 已归一：保留声明值（但仍会在下面按 security_budget 压回预算）。
        weights = dict(securities)
    elif explicit_cash:
        weights = {code: value / declared_total for code, value in securities.items()}
    else:
        weights = {code: value / total * security_budget for code, value in securities.items()}

    # 证券不得超过预算；超出部分转入现金（cash_floor 是给证券的上限兜底）。
    overflow = sum(weights.values()) - security_budget
    if overflow > 1e-12:
        weights = {
            code: value * (security_budget / sum(weights.values())) for code, value in weights.items()
        }

    # ── 单标的上限：压到上限，余额按比例分摊，分不完则进现金 ─────────────
    repairs: list[str] = []
    capped = {
        code: min(value, rules.max_single_weight) for code, value in weights.items()
    }
    if capped != weights:
        repairs.append("max_single_weight: 单标的权重超限，已压到上限")
        weights = capped

    shortfall = security_budget - sum(weights.values())
    if shortfall > 1e-9:
        others = [code for code, value in weights.items() if value > 0]
        headroom = sum(
            rules.max_single_weight - weights[code] for code in others
        )
        if others and headroom > 1e-9:
            scale = min(1.0, shortfall / headroom)
            for code in others:
                weights[code] += (rules.max_single_weight - weights[code]) * scale
            shortfall = security_budget - sum(weights.values())
        if shortfall > 1e-9:
            repairs.append("cash_sweep: 超出上限的权重已转入现金")

    # 现金是**残余项**：证券权重之和已经预留了预算，差额就是应由现金占的部分。
    # 只有当差额**低于** cash_floor 时才兜底到 cash_floor（证券多占了就压回预算）。
    # 不能写成 max(cash_floor, residual)：那会把"目标只需要很少现金"的方案
    # （如微调档只把 0.4% 挪成现金）强行垫到 5%，凭空多持现金，也让换手率不再与
    # 生成器的混合比例成正比。
    residual = 1.0 - sum(weights.values())
    if residual < rules.cash_floor - 1e-12:
        # 证券占了 1 - residual，需要让出 cash_floor 给现金。
        weights = {
            code: value * ((1.0 - rules.cash_floor) / sum(weights.values()))
            for code, value in weights.items()
        }
        residual = rules.cash_floor
    weights[CASH] = residual

    # ── 换手率：现金不计入，只比较证券权重 ──────────────────────────────
    # 设计文档明确：把现金算进换手率会让"降仓避险"被计成极高换手。
    codes = {code for code in weights if code != CASH} | {
        code for code in holdings if code != CASH
    }
    delta = 0.0
    for code in codes:
        target = weights.get(code, 0.0)
        current = _weight_of(holdings, code)
        change = abs(target - current)
        if change < rules.min_trade_weight:
            continue
        delta += change
    turnover = delta / 2.0

    current_weights = {code: _weight_of(holdings, code) for code in codes}
    for code, row in holdings.items():
        product = products.get(code) or {}
        if not product.get("priceable", True):
            limitations.append(f"{code} 无日频净值，估值与对比中已排除")

    return ValidatedPlan(
        weights=weights,
        current_weights=current_weights,
        turnover_rate=turnover,
        tier=classify_tier(turnover),
        repairs=repairs,
        limitations=limitations,
    )


__all__ = [
    "CASH",
    "LIGHT_BELOW",
    "MODERATE_UP_TO",
    "RISK_ORDER",
    "TurnoverTier",
    "risk_index",
    "ValidatedPlan",
    "ValidationConstraints",
    "ValidationError",
    "classify_tier",
    "normalize_and_validate",
]
