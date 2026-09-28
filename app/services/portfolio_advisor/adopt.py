"""采纳流程：把"用户在中断里选了一个方案"落到 AI 账本。

把几个接缝串起来：中断载荷的 resume 校验 → 读取方案 → （首次时）建立 AI 账户 →
按方案权重做差额调仓 → 返回采纳结果。

三条不变量：

- **只能用载荷里提供的 plan_id**（``validate_allocation_resume``）——模型和前端都不能
  凭空指定方案，否则等于绕过校验器。
- **用户持仓一行都不动**：本流程只经 ``LedgerService``，而它只写 ``ai_*`` 表。
- **采纳不可逆**：同一 plan_id 第二次采纳返回 ``already_adopted``；换方案再采是一次新的
  调仓，**不重建账户**——重建会让历史净值曲线断掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Protocol

from app.agents.portfolio.contracts import (
    AllocationInterruptError,
    validate_allocation_resume,
)
from app.agents.portfolio.plan import PlanTier
from app.services.portfolio_advisor.ledger import LedgerError, LedgerService


class AdoptError(ValueError):
    """采纳流程的前置条件不满足。``code`` 便于上层映射错误响应。"""

    def __init__(self, message: str, *, code: str = "adopt_failed") -> None:
        super().__init__(message)
        self.code = code


class PlanReader(Protocol):
    """按 plan_id 读取已落库方案。"""

    def get_plan(self, plan_id: str) -> dict | None: ...


@dataclass
class AdoptionOutcome:
    """一次采纳的结果。"""

    plan_id: str
    tier: PlanTier
    cash_balance: float
    total_assets: float
    unit_nav: float
    total_fee: float
    trade_count: int
    established: bool


def _plan_weights(plan: Mapping[str, Any]) -> dict[str, float]:
    """取方案的目标权重。

    方案既可能带着生成时的 ``weights``（含 ``cash``），也可能只存了明细行
    （``allocation_plan_items``）。两者都要能采纳：前者直接用，后者由
    ``target_weight`` 拼出来，现金是残余项。
    """
    raw = plan.get("weights")
    if isinstance(raw, Mapping) and raw:
        return {str(code): float(value) for code, value in raw.items()}

    weights: dict[str, float] = {}
    for item in plan.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("product_code") or "")
        if code:
            weights[code] = float(item.get("target_weight") or 0.0)
    if not weights:
        raise AdoptError("方案里没有任何持仓权重", code="empty_plan")

    residue = 1.0 - sum(weights.values())
    if residue > 1e-9:
        weights["cash"] = residue
    return weights


def adopt_selected_plan(
    *,
    customer_id: str,
    payload: Mapping[str, Any],
    resume_value: Mapping[str, Any] | None,
    plans: PlanReader,
    ledger: LedgerService,
    current_holdings: Mapping[str, Mapping[str, Any]],
    navs: Mapping[str, float],
    nav_as_of: date,
    initial_capital: float,
    fee_buy: float = 0.0015,
    fee_sell: float = 0.005,
) -> AdoptionOutcome:
    """采纳用户在中断里选定的方案。

    ``current_holdings`` 是用户当前持仓（``market_value`` / ``shares`` / ``priceable``），
    用于两件事：首次采纳时镜像建仓，以及计算差额成交。
    """
    # 1) resume 只能是载荷里声明的、且可用的 plan_id。
    chosen = validate_allocation_resume(payload, resume_value)
    plan_id = chosen["plan_id"]

    # 2) 读方案并确认它确实存在、还没被采纳过。
    plan = plans.get_plan(plan_id)
    if plan is None:
        raise AdoptError(f"方案不存在: {plan_id}", code="plan_not_found")
    if plan.get("status") == "adopted" and plan.get("adopted_at"):
        raise AdoptError(f"方案已被采纳: {plan_id}", code="already_adopted")

    weights = _plan_weights(plan)
    tier = PlanTier(str(plan.get("tier") or "moderate"))

    # 3) 首次采纳：建立 AI 账户并镜像用户持仓。第二次采纳不重建账户。
    established = False
    if not ledger.has_account(customer_id):
        ledger.establish(
            customer_id=customer_id,
            initial_capital=initial_capital,
            holdings_summary={
                code: float(row.get("market_value") or 0.0)
                for code, row in current_holdings.items()
            },
            nav_as_of=nav_as_of,
            navs=navs,
            opening_plan_id=plan_id,
        )
        established = True

    try:
        adoption = ledger.adopt_plan(
            customer_id=customer_id,
            plan_id=plan_id,
            weights=weights,
            holdings=current_holdings,
            navs=navs,
            nav_as_of=nav_as_of,
            fee_buy=fee_buy,
            fee_sell=fee_sell,
        )
    except LedgerError as exc:
        raise AdoptError(str(exc), code=exc.code) from exc

    return AdoptionOutcome(
        plan_id=plan_id,
        tier=tier,
        cash_balance=adoption.cash_balance,
        total_assets=adoption.total_assets,
        unit_nav=adoption.unit_nav,
        total_fee=adoption.total_fee,
        trade_count=len(adoption.trades),
        established=established,
    )


__all__ = [
    "AdoptError",
    "AdoptionOutcome",
    "PlanReader",
    "adopt_selected_plan",
]
