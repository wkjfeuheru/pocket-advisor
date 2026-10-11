"""``allocation_required`` 中断的载荷契约与 resume 校验。

用户选定方案这个动作复用现有 HITL 机制（LangGraph ``interrupt()`` / ``Command(resume=...)``），
但多了一条与既有两种中断不同的规则：**载荷里必须带服务端生成的候选，而 resume 只能从中选**。
模型和前端都不能凭空提交一个 ``plan_id``，否则等于绕过了方案校验器与硬约束。

载荷同时承担"方案对比表"的职责：用户要看到换手率、预估费用、调仓明细与理由才能选。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

#: 与支付/行情口径无关，仅用于识别"这份载荷是哪个版本的 agent 产出的"。
ALLOCATION_AGENT_VERSION = "portfolio-allocation-v1"


class AllocationOffer(BaseModel):
    """挂起前的三档方案集（结果侧留痕）。

    ``plans`` / ``unavailable`` 沿用服务的开放结构，不在本层收窄——方案明细的字段
    白名单由 ``build_allocation_payload`` 与 ``public_allocation_payload`` 负责。
    """

    model_config = ConfigDict(extra="forbid")

    plans: list[dict[str, Any]] = Field(default_factory=list)
    unavailable: list[dict[str, Any]] = Field(default_factory=list)
    nav_as_of: str = ""
    missing_nav: list[str] = Field(default_factory=list)


class AllocationOutcome(BaseModel):
    """采纳方案后的成交摘要。字段严格对齐 ``evidence._ADOPTED_FIELDS``。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = ""
    tier: str = ""
    tier_label: str = ""
    cash_balance: float | None = None
    total_assets: float | None = None
    unit_nav: float | None = None
    total_fee: float | None = None
    trade_count: int | None = None
    established: bool | None = None
    nav_as_of: str = ""


#: 单份方案对外暴露的字段白名单。``weights`` 刻意不暴露——用户看的是调仓明细与金额，
#: 原始权重向量既冗余又容易被误读为精确指令。
_PUBLIC_PLAN_FIELDS = (
    "plan_id",
    "tier",
    "label",
    "turnover_rate",
    "estimated_fee",
    "summary",
    "items",
    "limitations",
    "unavailable_reason",
)

_TIER_LABELS = {"light": "微调", "moderate": "中度", "rebuild": "重构"}

#: 单份调仓明细对外暴露的字段白名单。
_PUBLIC_ITEM_FIELDS = (
    "product_code",
    "product_name",
    "action",
    "current_weight",
    "target_weight",
    "delta_weight",
    "reason",
)


class AllocationInterruptError(ValueError):
    """中断载荷或 resume 值不合法。``code`` 便于上层映射 SSE 错误码。"""

    def __init__(self, message: str, *, code: str = "invalid_allocation") -> None:
        super().__init__(message)
        self.code = code


def _public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    packed: dict[str, Any] = {}
    for field in _PUBLIC_ITEM_FIELDS:
        if field not in item:
            continue
        value: Any = item[field]
        if field in {"current_weight", "target_weight", "delta_weight"} and value is not None:
            value = float(value)
        packed[field] = value
    return packed


def build_allocation_payload(plans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把校验后的方案列表包成中断载荷。

    至少需要一个方案；``plans`` 为空说明生成阶段全军覆没，此时不应挂起等用户选择，
    而应直接以错误结束本轮。
    """
    cleaned = [dict(plan) for plan in plans if isinstance(plan, Mapping)]
    if not cleaned:
        raise AllocationInterruptError("没有可展示的方案", code="no_plans")
    return {
        "kind": "allocation",
        "agent_version": ALLOCATION_AGENT_VERSION,
        "plans": cleaned,
    }


def public_allocation_payload(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """脱敏成给前端的公开载荷，返回 ``(事件名, 载荷)``。

    只保留白名单字段：调仓明细、换手率、预估费用、理由、限制与不可用原因。
    内部字段（原始权重、调试信息等）一律剔除。
    """
    plans: list[dict[str, Any]] = []
    for raw in payload.get("plans") or []:
        if not isinstance(raw, Mapping):
            continue
        packed: dict[str, Any] = {}
        for field in _PUBLIC_PLAN_FIELDS:
            if field not in raw:
                continue
            if field == "items":
                packed["items"] = [
                    _public_item(item)
                    for item in (raw.get("items") or [])
                    if isinstance(item, Mapping)
                ]
            else:
                packed[field] = raw[field]
        tier = str(raw.get("tier") or "")
        packed.setdefault("label", _TIER_LABELS.get(tier, tier))
        plans.append(packed)

    return "allocation_required", {
        "kind": "allocation",
        "agent_version": str(payload.get("agent_version") or ALLOCATION_AGENT_VERSION),
        "plans": plans,
    }


def validate_allocation_resume(
    payload: Mapping[str, Any], resume_value: Mapping[str, Any] | None
) -> dict[str, str]:
    """校验 resume 值：必须恰好选中载荷里提供的、且可用的一个 ``plan_id``。

    这是"只能从服务端候选中选"这条规则在方案场景的落地——与既有
    ``validate_clarify_answers`` 同一思路，但作用对象是 ``plan_id``。
    """
    if not isinstance(resume_value, Mapping):
        raise AllocationInterruptError("resume 值必须是对象", code="missing_plan")

    unknown = [key for key in resume_value if key != "plan_id"]
    if unknown:
        raise AllocationInterruptError(
            f"只能提交 plan_id，收到额外字段: {sorted(unknown)}", code="unknown_field"
        )

    chosen = str(resume_value.get("plan_id") or "").strip()
    if not chosen:
        raise AllocationInterruptError("请选择一个方案", code="missing_plan")

    offered: dict[str, Mapping[str, Any]] = {}
    for raw in payload.get("plans") or []:
        if isinstance(raw, Mapping) and raw.get("plan_id"):
            offered[str(raw["plan_id"])] = raw

    if chosen not in offered:
        raise AllocationInterruptError(f"方案不在本轮候选中: {chosen}", code="unknown_plan")

    if offered[chosen].get("unavailable_reason"):
        raise AllocationInterruptError(
            f"该方案不可采纳: {offered[chosen]['unavailable_reason']}", code="plan_unavailable"
        )

    return {"plan_id": chosen}


__all__ = [
    "ALLOCATION_AGENT_VERSION",
    "AllocationInterruptError",
    "AllocationOffer",
    "AllocationOutcome",
    "build_allocation_payload",
    "public_allocation_payload",
    "validate_allocation_resume",
]
