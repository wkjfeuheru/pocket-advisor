"""``allocation_required`` 中断载荷的契约与 resume 校验。

用户选定方案这个动作复用现有 HITL 机制（LangGraph ``interrupt()`` + ``Command(resume=...)``），
但多了一条与既有两种中断不同的规则：**载荷里必须带服务端生成的候选，而 resume 只能从中选**。
模型和前端都不能凭空提交一个 ``plan_id``，否则就等于绕过了方案校验器。

这里只测契约层（构造、脱敏、校验）。把方案生成接进主图是下一步的事。
"""

from __future__ import annotations

import pytest

from app.agents.portfolio.contracts import (
    ALLOCATION_AGENT_VERSION,
    AllocationInterruptError,
    build_allocation_payload,
    public_allocation_payload,
    validate_allocation_resume,
)


def _plan(plan_id: str, tier: str, turnover: float) -> dict:
    return {
        "plan_id": plan_id,
        "tier": tier,
        "turnover_rate": turnover,
        "weights": {"110011": 0.50, "510300": 0.45, "cash": 0.05},
        "items": [
            {
                "product_code": "110011",
                "product_name": "易方达中小盘混合",
                "action": "reduce",
                "current_weight": 0.80,
                "target_weight": 0.50,
                "delta_weight": -0.30,
                "reason": "降低单一主动基金暴露",
            }
        ],
        "estimated_fee": 2.75,
        "limitations": ["净值截至 2026-01-05"],
        "internal_debug": "不应该出现在公开载荷里",
    }


def _three_plans():
    return [
        _plan("p-light", "light", 0.05),
        _plan("p-moderate", "moderate", 0.25),
        _plan("p-rebuild", "rebuild", 0.65),
    ]


def test_the_payload_carries_the_three_plans_and_a_version_tag():
    payload = build_allocation_payload(_three_plans())

    assert payload["kind"] == "allocation"
    assert payload["agent_version"] == ALLOCATION_AGENT_VERSION
    assert [item["plan_id"] for item in payload["plans"]] == [
        "p-light",
        "p-moderate",
        "p-rebuild",
    ]
    assert [item["tier"] for item in payload["plans"]] == ["light", "moderate", "rebuild"]


def test_the_public_payload_drops_internal_fields_and_keeps_the_comparison_data():
    event, public = public_allocation_payload(build_allocation_payload(_three_plans()))

    assert event == "allocation_required"
    assert public["kind"] == "allocation"
    assert len(public["plans"]) == 3

    first = public["plans"][0]
    # 用户要对比的就是这些：换手、费用、明细、理由、限制
    assert first["turnover_rate"] == pytest.approx(0.05)
    assert first["estimated_fee"] == pytest.approx(2.75)
    assert first["items"][0]["reason"] == "降低单一主动基金暴露"
    assert first["limitations"] == ["净值截至 2026-01-05"]
    # 内部字段必须被剔除
    assert "internal_debug" not in first
    assert "weights" not in first


def test_a_payload_without_plans_is_rejected():
    with pytest.raises(AllocationInterruptError) as excinfo:
        build_allocation_payload([])

    assert excinfo.value.code == "no_plans"


def test_resume_must_pick_one_of_the_offered_plan_ids():
    payload = build_allocation_payload(_three_plans())

    chosen = validate_allocation_resume(payload, {"plan_id": "p-moderate"})
    assert chosen == {"plan_id": "p-moderate"}


def test_resume_with_an_invented_plan_id_is_rejected():
    payload = build_allocation_payload(_three_plans())

    with pytest.raises(AllocationInterruptError) as excinfo:
        validate_allocation_resume(payload, {"plan_id": "p-i-made-this-up"})

    assert excinfo.value.code == "unknown_plan"


def test_resume_with_an_unavailable_tier_is_rejected():
    """超界被标记为不可用的档位不能被采纳——否则校验器白做了。"""
    plans = _three_plans()
    plans[2]["unavailable_reason"] = "turnover_exceeded"
    payload = build_allocation_payload(plans)

    with pytest.raises(AllocationInterruptError) as excinfo:
        validate_allocation_resume(payload, {"plan_id": "p-rebuild"})

    assert excinfo.value.code == "plan_unavailable"


def test_resume_missing_or_extra_fields_are_rejected():
    payload = build_allocation_payload(_three_plans())

    with pytest.raises(AllocationInterruptError) as missing:
        validate_allocation_resume(payload, {})
    assert missing.value.code == "missing_plan"

    with pytest.raises(AllocationInterruptError) as extra:
        validate_allocation_resume(payload, {"plan_id": "p-light", "override_weights": {"x": 1}})
    assert extra.value.code == "unknown_field"
