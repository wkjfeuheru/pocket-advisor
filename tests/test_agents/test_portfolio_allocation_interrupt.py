"""``allocation_required`` 中断在 AgentService 里的接线。

与既有 ``clarification_required`` 同构：轮次挂起 → 落库 ``interrupt_payload`` →
用户提交选择 → 校验必须来自声明候选 → 原子抢占并 resume。

差别在于**校验对象是 ``plan_id``**：模型和前端都不能凭空指定方案，
否则等于绕过方案校验器与硬约束。
"""

from __future__ import annotations

import pytest

from app.agents.portfolio.contracts import (
    ALLOCATION_AGENT_VERSION,
    build_allocation_payload,
)
from app.services.agent_service import AdvisorServiceError, public_interrupt_payload


def _plan(plan_id: str, tier: str) -> dict:
    return {
        "plan_id": plan_id,
        "tier": tier,
        "turnover_rate": 0.05 if tier == "light" else 0.30,
        "estimated_fee": 12.5,
        "items": [
            {
                "product_code": "110011",
                "product_name": "易方达中小盘混合",
                "action": "reduce",
                "current_weight": 0.60,
                "target_weight": 0.55,
                "delta_weight": -0.05,
                "reason": "降低集中度",
            }
        ],
        "limitations": ["净值截至 2026-09-28"],
        "internal_debug": "不该出现在公开载荷里",
    }


def _payload():
    return build_allocation_payload([_plan("p-light", "light"), _plan("p-moderate", "moderate")])


def test_public_payload_emits_the_allocation_event_and_sanitizes_plans():
    event, public = public_interrupt_payload(_payload())

    assert event == "allocation_required"
    assert public["kind"] == "allocation"
    assert public["agent_version"] == ALLOCATION_AGENT_VERSION
    assert [p["plan_id"] for p in public["plans"]] == ["p-light", "p-moderate"]
    first = public["plans"][0]
    assert first["tier"] == "light"
    assert first["items"][0]["reason"] == "降低集中度"
    # 内部字段必须剔除
    assert "internal_debug" not in first


def test_public_payload_keeps_the_other_interrupt_kinds_unchanged():
    """新分支不能把既有的两种中断吃掉。"""
    event, public = public_interrupt_payload({"kind": "clarification", "reason": "x"})
    assert event == "clarification_required"
    assert public["kind"] == "clarification"

    event, public = public_interrupt_payload({"missing_fields": ["risk_level"]})
    assert event == "profile_required"
    assert public["missing_fields"] == ["risk_level"]


def test_validation_accepts_a_declared_plan_id():
    from app.agents.portfolio.contracts import validate_allocation_resume

    assert validate_allocation_resume(_payload(), {"plan_id": "p-moderate"}) == {
        "plan_id": "p-moderate"
    }


def test_validation_rejects_a_plan_id_that_was_never_offered():
    from app.agents.portfolio.contracts import (
        AllocationInterruptError,
        validate_allocation_resume,
    )

    with pytest.raises(AllocationInterruptError) as excinfo:
        validate_allocation_resume(_payload(), {"plan_id": "p-rebuild"})

    assert excinfo.value.code == "unknown_plan"


def test_validation_rejects_extra_fields_so_weights_cannot_be_overridden():
    from app.agents.portfolio.contracts import (
        AllocationInterruptError,
        validate_allocation_resume,
    )

    with pytest.raises(AllocationInterruptError) as excinfo:
        validate_allocation_resume(
            _payload(), {"plan_id": "p-light", "weights": {"110011": 1.0}}
        )

    assert excinfo.value.code == "unknown_field"


def test_validation_rejects_a_missing_plan_id():
    from app.agents.portfolio.contracts import (
        AllocationInterruptError,
        validate_allocation_resume,
    )

    for resume in (None, {}, {"plan_id": ""}):
        with pytest.raises(AllocationInterruptError) as excinfo:
            validate_allocation_resume(_payload(), resume)
        assert excinfo.value.code == "missing_plan"


def test_an_unavailable_plan_cannot_be_selected():
    from app.agents.portfolio.contracts import (
        AllocationInterruptError,
        validate_allocation_resume,
    )

    plan = _plan("p-rebuild", "rebuild")
    plan["unavailable_reason"] = "turnover_out_of_band"
    payload = build_allocation_payload([plan])

    with pytest.raises(AllocationInterruptError) as excinfo:
        validate_allocation_resume(payload, {"plan_id": "p-rebuild"})

    assert excinfo.value.code == "plan_unavailable"


def test_the_service_exposes_the_adopt_entry_point():
    """服务层必须有一个与 prepare_clarify 同构的采纳入口。"""
    from app.services.agent_service import AgentService

    assert hasattr(AgentService, "prepare_allocation_adopt")


def test_public_payload_of_an_empty_allocation_is_still_shaped():
    """载荷里一个方案都没有时，公开结构也不能崩。"""
    event, public = public_interrupt_payload(
        {"kind": "allocation", "agent_version": ALLOCATION_AGENT_VERSION, "plans": []}
    )

    assert event == "allocation_required"
    assert public["plans"] == []
