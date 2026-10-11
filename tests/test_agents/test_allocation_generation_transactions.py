"""使用真实 SQLAlchemy 提交/回滚验证生产 PlanStore 的事务复用。

SQLite 仅作本地事务后端；PostgreSQL advisory lock 的并发保证另由真库集成测试覆盖。
"""

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import BigInteger, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles, deregister
from sqlalchemy.orm import Session, sessionmaker

from app.crud import portfolio_advisor_crud as crud
from app.db.models.portfolio_advisor import AllocationGeneration, AllocationPlan, AllocationPlanItem
from tests.test_agents.test_allocation_generation_safety import service


REQUEST = "11111111-1111-1111-1111-111111111111"
SESSION = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def actual_plan_store(monkeypatch):
    @compiles(JSONB, "sqlite")
    def jsonb_as_json(type_, compiler, **kwargs):
        return "JSON"

    @compiles(BigInteger, "sqlite")
    def bigint_identity_as_integer(type_, compiler, **kwargs):
        return "INTEGER"

    @compiles(UUID, "sqlite")
    def uuid_as_character(type_, compiler, **kwargs):
        return "CHAR(32)"

    class LocalTransactionSession(Session):
        def execute(self, statement, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(statement):
                # 单线程回滚测试不模拟锁；并发锁语义仅在真实 PostgreSQL 上断言。
                return super().execute(text("SELECT 1"))
            return super().execute(statement, *args, **kwargs)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS finance")
    for model in (AllocationPlan, AllocationPlanItem, AllocationGeneration):
        model.__table__.create(engine)
    factory = sessionmaker(engine, class_=LocalTransactionSession, expire_on_commit=False, autoflush=False)

    @contextmanager
    def scope():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(crud, "ensure_schema", lambda: None)
    monkeypatch.setattr(crud, "session_scope", scope)
    try:
        yield crud.PostgresPlanStore(), factory
    finally:
        engine.dispose()
        deregister(JSONB)
        deregister(BigInteger)
        deregister(UUID)


def test_actual_plan_store_rolls_back_flushed_first_tier_when_second_insert_fails(actual_plan_store, monkeypatch):
    store, sessions = actual_plan_store
    insert = store.insert_plan
    calls = 0

    def fail_second(plan, items):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("failed-second-tier")
        return insert(plan, items)

    monkeypatch.setattr(store, "insert_plan", fail_second)
    with pytest.raises(RuntimeError, match="second-tier"):
        service(store).generate("C", request_id=REQUEST, session_id=SESSION, task_id="t")
    with sessions() as session:
        for model in (AllocationPlan, AllocationPlanItem, AllocationGeneration):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    monkeypatch.setattr(store, "insert_plan", insert)
    response = service(store).generate("C", request_id=REQUEST, session_id=SESSION, task_id="t")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AllocationGeneration)) == 1
        assert session.scalar(select(func.count()).select_from(AllocationPlan)) == len(response["plans"])


def test_actual_plan_store_snapshot_replay_survives_a_new_store_instance(actual_plan_store):
    store, sessions = actual_plan_store
    first = service(store).generate("C", request_id=REQUEST, session_id=SESSION, task_id="t", as_of=date(2026, 10, 10))
    second = service(crud.PostgresPlanStore()).generate("C", request_id=REQUEST, session_id=SESSION, task_id="t", as_of=date(2026, 10, 11))
    assert first == second
    assert first["nav_as_of"] == "2026-10-10"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AllocationGeneration)) == 1
        assert session.scalar(select(func.count()).select_from(AllocationPlan)) == len(first["plans"])


def test_actual_plan_store_rolls_back_all_tiers_when_snapshot_cannot_be_saved(actual_plan_store, monkeypatch):
    store, sessions = actual_plan_store

    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("failed-generation-snapshot")

    monkeypatch.setattr(store, "save_generation", fail_snapshot)
    with pytest.raises(RuntimeError, match="generation-snapshot"):
        service(store).generate("C", request_id=REQUEST, session_id=SESSION, task_id="t")
    with sessions() as session:
        for model in (AllocationPlan, AllocationPlanItem, AllocationGeneration):
            assert session.scalar(select(func.count()).select_from(model)) == 0
