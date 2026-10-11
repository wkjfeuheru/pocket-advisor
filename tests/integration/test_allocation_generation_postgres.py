"""真实 PostgreSQL 的整批提交、完成快照和 advisory 锁并发保证。"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import uuid4

import pytest

if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip("需要 RUN_POSTGRES_INTEGRATION=1 和 PostgreSQL 连接配置", allow_module_level=True)

from sqlalchemy import func, select, text

from app.agents.portfolio.validation import ValidationConstraints
from app.crud.portfolio_advisor_crud import PostgresPlanStore, ensure_test_customer
from app.crud.agent_session_crud import PostgresAdvisorStore
from app.db.models.portfolio_advisor import AllocationGeneration, AllocationPlan, AllocationPlanItem
from app.db.session import session_scope
from app.services.portfolio_advisor.advisor_service import AdvisorDeps, AdvisorService


class Portfolio:
    def list_positions(self, customer_id):
        return [{"product_code": "110011", "market_value": 800}, {"product_code": "510300", "market_value": 200}]


class Library:
    def list_products_for_nav(self):
        return [{"code": "110011", "risk_level": "R3", "priceable": True},
                {"code": "510300", "risk_level": "R1", "priceable": True}]

    def query_by_codes(self, codes):
        return [row for row in self.list_products_for_nav() if row["code"] in codes]


class Navs:
    def history(self, code, start, end):
        return [(end, 1.0)]


def service(store):
    # 存储与锁使用真实 PostgreSQL；输入行情是固定夹具，不涉及外部金融数据源。
    return AdvisorService(AdvisorDeps(Portfolio(), Library(), Navs(), store, None,
                                     constraints=ValidationConstraints(max_single_weight=0.85)))


@pytest.fixture
def identity():
    customer = f"GENTEST{uuid4().hex[:12].upper()}"
    ensure_test_customer(customer)
    conversations = PostgresAdvisorStore()
    with conversations.transaction():
        conversation = conversations.insert_session(customer, "生成事务集成测试")
    try:
        yield customer, {"request_id": str(uuid4()), "session_id": conversation["id"], "task_id": "generation-test"}
    finally:
        with session_scope() as session:
            session.execute(text("DELETE FROM finance.users WHERE customer_id = :customer"), {"customer": customer})


@pytest.mark.parametrize("fault", ["second_insert", "snapshot"])
def test_real_database_rolls_back_every_plan_and_snapshot_on_batch_failure(identity, monkeypatch, fault):
    customer, key = identity
    store = PostgresPlanStore()
    original = store.insert_plan if fault == "second_insert" else store.save_generation
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if fault == "snapshot" or calls == 2:
            raise RuntimeError("injected-generation-failure")
        return original(*args, **kwargs)

    method = "insert_plan" if fault == "second_insert" else "save_generation"
    monkeypatch.setattr(store, method, fail)
    with pytest.raises(RuntimeError, match="injected-generation-failure"):
        service(store).generate(customer, **key)
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AllocationPlan).where(AllocationPlan.customer_id == customer)) == 0
        assert session.scalar(select(func.count()).select_from(AllocationGeneration).where(AllocationGeneration.customer_id == customer)) == 0
        assert session.scalar(select(func.count()).select_from(AllocationPlanItem).join(AllocationPlan, AllocationPlan.plan_id == AllocationPlanItem.plan_id)
                              .where(AllocationPlan.customer_id == customer)) == 0
    monkeypatch.setattr(store, method, original)
    retried = service(store).generate(customer, **key)
    assert len(retried["plans"]) >= 2
    assert retried == service(PostgresPlanStore()).generate(customer, **key)


def test_real_database_serializes_same_key_across_independent_store_instances(identity):
    customer, key = identity
    ready = Barrier(2)

    def generate():
        ready.wait(timeout=10)
        return service(PostgresPlanStore()).generate(customer, **key, as_of=date(2026, 10, 10))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(generate) for _ in range(2)]
        first, second = [future.result(timeout=30) for future in futures]
    assert first == second
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AllocationGeneration).where(AllocationGeneration.customer_id == customer)) == 1
        assert session.scalar(select(func.count()).select_from(AllocationPlan).where(AllocationPlan.customer_id == customer)) == len(first["plans"])
