"""持仓配置分支：生成 → 挂起 → 校验选择 → 采纳。

用可注入的 ``interrupt`` 函数实现，因此完整分支逻辑可以在**不搭真图 + 真 checkpointer**
的前提下测完。真图那一层另有集成测试。

关键顺序：**方案先落库，再 interrupt**。这样用户 resume 时引用的 ``plan_id``
必然存在——反过来（先挂起再落库）会在 resume 时找不到方案。
"""

from __future__ import annotations

import pytest

from app.agents.graphs.results import PortfolioWorkerResult
from app.agents.portfolio.branch import run_allocation_task


class _FakeService:
    """记录调用；``generate`` 返回已落库的方案视图。"""

    def __init__(self, *, plans=None, unavailable=None, adopt_result=None, fail_generate=None, fail_adopt=None):
        self.plans = plans if plans is not None else [_plan("p-1", "light", "微调")]
        self.unavailable = unavailable or []
        self.adopt_result = adopt_result or {
            "plan_id": "p-1",
            "tier": "light",
            "tier_label": "微调",
            "trade_count": 2,
            "unit_nav": 0.9975,
            "total_assets": 997.5,
            "cash_balance": 47.25,
            "total_fee": 2.5,
            "established": True,
        }
        self.fail_generate = fail_generate
        self.fail_adopt = fail_adopt
        self.adopted: list[str] = []
        self.generated = 0

    def generate(self, customer_id, *, request_id=None, session_id=None):
        self.generated += 1
        if self.fail_generate:
            raise self.fail_generate
        return {
            "customer_id": customer_id,
            "total_assets": 1000.0,
            "nav_as_of": "2026-09-28",
            "missing_nav": [],
            "plans": self.plans,
            "unavailable": self.unavailable,
        }

    def adopt(self, customer_id, plan_id, *, request_id=None):
        if self.fail_adopt:
            raise self.fail_adopt
        self.adopted.append(plan_id)
        return dict(self.adopt_result, plan_id=plan_id)


def _plan(plan_id: str, tier: str, label: str) -> dict:
    return {
        "plan_id": plan_id,
        "tier": tier,
        "tier_label": label,
        "turnover_rate": 0.05,
        "estimated_fee": 12.5,
        "weights": {"110011": 0.55, "cash": 0.05},
        "limitations": [],
        "unavailable_reason": "",
    }


def _run(service, resume=None, **overrides):
    """``interrupt`` 替身：记录被挂起时收到的载荷，并返回 ``resume``。"""
    seen: dict = {}

    def _interrupt(payload):
        seen["payload"] = payload
        return resume if resume is not None else {"plan_id": service.plans[0]["plan_id"]}

    kwargs = {"customer_id": "U1", "service": service, "interrupt": _interrupt}
    kwargs.update(overrides)
    result = run_allocation_task(**kwargs)
    return result, seen


def test_the_branch_interrupts_with_the_persisted_plans_then_adopts_the_choice():
    service = _FakeService(plans=[_plan("p-1", "light", "微调"), _plan("p-2", "moderate", "中度")])

    result, seen = _run(service, resume={"plan_id": "p-2"})

    # 挂起载荷里带上了两个候选
    assert seen["payload"]["kind"] == "allocation"
    assert [p["plan_id"] for p in seen["payload"]["plans"]] == ["p-1", "p-2"]

    # 采纳的是用户选的那个
    assert service.adopted == ["p-2"]
    assert result.failed is False
    assert "已采纳" in result.summary
    assert result.artifacts[0]["scenario"] == "allocation_adopted"


def test_the_result_keeps_the_specialist_shape_expected_by_the_graph():
    """compliance_node / suitability_guard 不该需要知道这个分支存在。"""
    service = _FakeService()

    result, _ = _run(service)

    assert isinstance(result, PortfolioWorkerResult)
    assert result.agent == "portfolio_agent"
    assert result.status == "ok"
    assert result.recommended_product_codes == []


def test_the_generated_summary_mentions_the_nav_cutoff():
    service = _FakeService()

    _, seen = _run(service)

    assert seen["payload"]["kind"] == "allocation"


def test_an_invalid_resume_choice_is_reported_without_adopting():
    service = _FakeService()

    result, _ = _run(service, resume={"plan_id": "invented"})

    assert service.adopted == []
    assert result.failed is True
    assert any("方案选择无效" in note for note in result.warnings)


def test_a_resume_value_carrying_extra_fields_is_rejected():
    """前端/模型都不能借 resume 夹带权重去覆盖方案。"""
    service = _FakeService()

    result, _ = _run(service, resume={"plan_id": "p-1", "weights": {"110011": 1.0}})

    assert service.adopted == []
    assert result.failed is True


def test_no_available_plan_does_not_interrupt_at_all():
    """一个可用档位都没有时挂起毫无意义——没有可选项给用户。"""
    service = _FakeService(plans=[], unavailable=[{"tier": "light", "tier_label": "微调", "unavailable_reason": "turnover_out_of_band"}])
    interrupted = {"called": False}

    def _interrupt(payload):
        interrupted["called"] = True
        return None

    result = run_allocation_task(customer_id="U1", service=service, interrupt=_interrupt)

    assert interrupted["called"] is False
    assert result.failed is False
    assert "没有可行" in result.summary
    assert "微调" in result.summary


def test_a_generation_failure_degrades_to_a_readable_result():
    service = _FakeService(fail_generate=RuntimeError("商品库读不到"))

    result = run_allocation_task(
        customer_id="U1", service=service, interrupt=lambda payload: None
    )

    assert result.failed is True
    assert "无法生成配置建议" in result.summary
    assert result.warnings


def test_an_adoption_failure_is_explained_and_does_not_crash_the_turn():
    service = _FakeService(fail_adopt=RuntimeError("账本不可用"))

    result, _ = _run(service)

    assert result.failed is True
    assert "采纳方案失败" in result.summary


def test_unavailable_tiers_are_surfaced_even_when_some_plans_exist():
    service = _FakeService(
        plans=[_plan("p-1", "light", "微调")],
        unavailable=[{"tier": "rebuild", "tier_label": "重构", "unavailable_reason": "turnover_out_of_band"}],
    )

    result, seen = _run(service)

    # 载荷里只放可用方案（不可用的进去会被 resume 校验拒掉），
    # 但文案里要说明哪些档位不可行。
    assert [p["plan_id"] for p in seen["payload"]["plans"]] == ["p-1"]
    assert "refund" not in result.summary
