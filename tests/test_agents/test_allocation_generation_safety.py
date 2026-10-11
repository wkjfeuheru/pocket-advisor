from datetime import date
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, RLock
import json

import pytest

from app.agents.portfolio.validation import ValidationConstraints
from app.services.portfolio_advisor.advisor_service import AdvisorDeps, AdvisorService
from tests.test_agents.test_portfolio_allocation import _FakePlanStore


class TransactionalPlanStore(_FakePlanStore):
    """与生产窄接口一致的事务假件，失败恢复整个方案批次和生成快照。"""

    def __init__(self):
        super().__init__()
        self.generations = {}
        self.lock = RLock()
        self.fail_at = None
        self.inserts = 0

    @contextmanager
    def generation_context(self, customer_id, **kwargs):
        with self.lock:
            before = deepcopy((self.plans, self.items, self.generations))
            try:
                yield
            except Exception:
                self.plans, self.items, self.generations = before
                raise

    def get_generation(self, customer_id, *, request_id, session_id, task_id):
        return deepcopy(self.generations.get((customer_id.upper(), request_id, session_id, task_id)))

    def save_generation(self, customer_id, response, *, request_id, session_id, task_id):
        normalized = json.loads(json.dumps(response, default=lambda value: value.isoformat()))
        self.generations[(customer_id.upper(), request_id, session_id, task_id)] = normalized
        return deepcopy(normalized)

    def insert_plan(self, plan, items):
        self.inserts += 1
        if self.inserts == self.fail_at:
            raise RuntimeError("第二档写入失败")
        return super().insert_plan(plan, items)


class Portfolio:
    def list_positions(self, customer_id):
        return [{"product_code": "A", "market_value": 800}, {"product_code": "B", "market_value": 200}]


class Library:
    def list_products_for_nav(self):
        return [{"code": "A", "risk_level": "R3", "priceable": True},
                {"code": "B", "risk_level": "R1", "priceable": True}]

    def query_by_codes(self, codes):
        return []


class Navs:
    def history(self, code, start, end):
        return [(end, 1.0)]


def service(store):
    return AdvisorService(AdvisorDeps(Portfolio(), Library(), Navs(), store, None,
                                     constraints=ValidationConstraints(max_single_weight=0.85)))


def test_generation_replays_same_request_session_task_after_rebuilding_service():
    store = TransactionalPlanStore()
    first = service(store).generate("C", request_id="r", session_id="s", task_id="t", as_of=date(2026, 10, 10))
    assert first["plans"]
    second = service(store).generate("C", request_id="r", session_id="s", task_id="t", as_of=date(2026, 10, 11))
    assert [p["plan_id"] for p in second["plans"]] == [p["plan_id"] for p in first["plans"]]
    assert len(store.plans) == len(first["plans"])
    third = service(store).generate("C", request_id="r", session_id="s", task_id="other", as_of=date(2026, 10, 10))
    assert {p["plan_id"] for p in third["plans"]}.isdisjoint({p["plan_id"] for p in first["plans"]})


def test_generation_failure_on_second_plan_rolls_back_entire_batch_and_snapshot():
    store = TransactionalPlanStore()
    store.fail_at = 2
    with pytest.raises(RuntimeError, match="第二档"):
        service(store).generate("C", request_id="r", session_id="s", task_id="t")
    assert store.plans == []
    assert store.items == []
    assert store.generations == {}
    store.fail_at = None
    retried = service(store).generate("C", request_id="r", session_id="s", task_id="t")
    assert len(retried["plans"]) == len(store.plans) >= 2
    assert len(store.generations) == 1


def test_concurrent_same_generation_key_returns_one_identical_complete_group():
    store = TransactionalPlanStore()
    ready = Barrier(2)

    def generate():
        ready.wait(timeout=5)
        return service(store).generate("C", request_id="r", session_id="s", task_id="t")

    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(generate) for _ in range(2)]
        first, second = [call.result(timeout=10) for call in calls]
    assert first == second
    assert len(store.plans) == len(first["plans"])
    assert len(store.generations) == 1


def test_generation_replays_full_snapshot_including_unavailable_and_missing_nav():
    store = TransactionalPlanStore()

    class MissingNavs(Navs):
        def history(self, code, start, end):
            return [] if code == "B" else super().history(code, start, end)

    original = service(store)
    original._deps.navs = MissingNavs()
    first = original.generate("C", request_id="r", session_id="s", task_id="t", as_of=date(2026, 10, 10))
    second = service(store).generate("C", request_id="r", session_id="s", task_id="t", as_of=date(2026, 10, 11))
    assert first["missing_nav"] == ["B"]
    assert first["unavailable"]
    assert first == second


def test_generate_uses_per_call_profile_risk_without_changing_shared_constraints(monkeypatch):
    from app.services.portfolio_advisor import advisor_service as module
    from app.services.portfolio_advisor.allocation import AllocationResult

    configured = service(_FakePlanStore())
    seen = []

    def generate(store, **kwargs):
        seen.append(kwargs["constraints"].max_risk_level)
        return AllocationResult()

    monkeypatch.setattr(module, "generate_and_persist", generate)
    configured.generate("C", max_risk="R1")
    configured.generate("C", max_risk="R5")
    assert seen == ["R1", "R5"]
    assert configured._deps.constraints.max_risk_level == "R3"


def test_adoption_rechecks_latest_profile_risk_before_touching_ledger():
    from app.services.portfolio_advisor.advisor_service import AdvisorServiceError

    class Plans(_FakePlanStore):
        def get_plan(self, plan_id):
            return {"customer_id": "C", "plan_id": plan_id, "tier": "light", "status": "offered",
                    "items": [{"product_code": "A", "target_weight": 0.5, "action": "hold"}]}

    class ActualLibrary(Library):
        def query_by_codes(self, codes):
            return [item for item in self.list_products_for_nav() if item["code"] in codes]

    configured = AdvisorService(AdvisorDeps(Portfolio(), ActualLibrary(), Navs(), Plans(), None))
    with pytest.raises(AdvisorServiceError) as caught:
        configured.adopt("C", "same-persisted-plan", max_risk="R1")
    assert caught.value.code == "risk_exceeded"


@pytest.mark.parametrize("stage", ["generate", "adopt"])
def test_allocation_errors_do_not_expose_exception_payload(stage):
    from app.agents.portfolio.branch import run_allocation_task
    from tests.test_agents.test_portfolio_branch import _FakeService

    secret = "postgresql://user:private-password@db-host"
    configured = _FakeService(**{f"fail_{stage}": RuntimeError(secret)})
    result = run_allocation_task(customer_id="C", service=configured,
                                 interrupt=lambda payload: {"plan_id": "p-1"})
    assert secret not in result.summary
    assert secret not in str(result.warnings)


@pytest.mark.parametrize("nav_as_of, expected", [(date(2026, 10, 10), "2026-10-10"), (None, ""),
                                                ("2026-10-10", "2026-10-10")])
def test_allocation_branch_normalizes_actual_adoption_date_without_losing_outcome(nav_as_of, expected):
    from app.agents.portfolio.branch import run_allocation_task
    from tests.test_agents.test_portfolio_branch import _FakeService

    class DateService(_FakeService):
        def generate(self, customer_id, **kwargs):
            return {**super().generate(customer_id, **kwargs), "nav_as_of": date(2026, 10, 10)}

    configured = DateService()
    configured.adopt_result["nav_as_of"] = nav_as_of
    original_numbers = {key: configured.adopt_result[key] for key in ("unit_nav", "total_assets", "total_fee", "trade_count")}
    result = run_allocation_task(customer_id="C", service=configured,
                                 interrupt=lambda payload: {"plan_id": "p-1"})
    assert configured.adopted == ["p-1"]
    assert result.status == "ok"
    assert result.offer.nav_as_of == "2026-10-10"
    assert result.adopted.nav_as_of == expected
    assert {key: getattr(result.adopted, key) for key in original_numbers} == original_numbers
