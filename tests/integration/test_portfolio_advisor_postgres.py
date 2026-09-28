"""净值与 AI 账本在真实 PostgreSQL 上的往返。

内存假存储只能验证服务的**意图**；真数据优先覆盖合成靠的是
``ON CONFLICT (product_code, trade_date) DO UPDATE ... WHERE``，这条语义只有真库能证明
（含"合成不得反向覆盖真数据"这个方向）。AI 账本同理，靠 ``UNIQUE (customer_id, trade_key)``
挡住"采纳一个多标的方案却只成交一笔"的复发。

需要 ``RUN_POSTGRES_INTEGRATION=1`` 与可访问的 PostgreSQL。
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip(
        "需要 RUN_POSTGRES_INTEGRATION=1 和可访问的真实 PostgreSQL",
        allow_module_level=True,
    )

from app.core.security import UniqueConstraintError  # noqa: E402
from app.crud.portfolio_advisor_crud import (  # noqa: E402
    PostgresLedgerStore,
    PostgresNavStore,
    PostgresPlanStore,
    ensure_test_customer,
)
from app.services.portfolio_advisor.ledger import LedgerService  # noqa: E402
from app.services.portfolio_advisor.nav import (  # noqa: E402
    REAL,
    SYNTHETIC,
    NavRow,
    NavService,
)

# 种子产品（migrations/002_seed.sql），外键要求 product_code 必须已存在。
PRODUCT_A = "110011"
PRODUCT_B = "510300"

# 用远古日期避免与种子产品业绩数据冲突。
FAR_PAST = date(1990, 1, 2)
FAR_PAST_2 = date(1990, 1, 3)


@pytest.fixture
def nav_service() -> NavService:
    return NavService(PostgresNavStore())


@pytest.fixture
def ledger_service() -> LedgerService:
    return LedgerService(PostgresLedgerStore())


def test_real_nav_overwrites_synthetic_in_a_real_database(nav_service):
    try:
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 1.0, SYNTHETIC)])
        assert nav_service.history(PRODUCT_A, FAR_PAST, FAR_PAST) == [(FAR_PAST, 1.0)]

        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 9.5, REAL)])
        assert nav_service.history(PRODUCT_A, FAR_PAST, FAR_PAST) == [(FAR_PAST, 9.5)]
    finally:
        nav_service.clear_synthetic(PRODUCT_A)
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 1.0, SYNTHETIC)])
        nav_service.clear_synthetic(PRODUCT_A)


def test_synthetic_never_overwrites_real_in_a_real_database(nav_service):
    """这条是"清除合成后结果不受污染"的前提，必须由数据库层保证。"""
    try:
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 9.5, REAL)])
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 1.0, SYNTHETIC)])

        assert nav_service.history(PRODUCT_A, FAR_PAST, FAR_PAST) == [(FAR_PAST, 9.5)]
    finally:
        _purge(nav_service, PRODUCT_A)


def test_clearing_synthetic_leaves_real_rows_in_a_real_database(nav_service):
    try:
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST, 9.5, REAL)])
        nav_service.upsert([NavRow(PRODUCT_A, FAR_PAST_2, 1.0, SYNTHETIC)])

        removed = nav_service.clear_synthetic(PRODUCT_A)

        assert removed >= 1
        assert nav_service.history(PRODUCT_A, FAR_PAST, FAR_PAST_2) == [(FAR_PAST, 9.5)]
    finally:
        _purge(nav_service, PRODUCT_A)


def test_write_count_reflects_rows_actually_written(nav_service):
    """返回值必须是真实写入行数。

    ``ON CONFLICT DO UPDATE ... WHERE`` 这种形式下 psycopg 的 ``rowcount`` 是 -1，
    直接返回会得到"写入 -1935 行"这种输出。这里把它钉住：返回批大小，
    且当某行因合成压不过真数据而被跳过时，计数必须相应减少。
    """
    try:
        # 先放一行真数据，让合成批次里恰好有一行压不动。
        nav_service.upsert([NavRow(PRODUCT_B, FAR_PAST_2, 8.88, REAL)])

        rows = [
            NavRow(PRODUCT_B, FAR_PAST, 1.0, SYNTHETIC),
            NavRow(PRODUCT_B, FAR_PAST_2, 1.0, SYNTHETIC),  # 会被真数据挡住
        ]
        written = nav_service._store.upsert_many(rows)

        assert written == 1, f"应只写入 1 行（另一行被真数据挡住），实际 {written}"
        assert written >= 0
        assert nav_service.history(PRODUCT_B, FAR_PAST, FAR_PAST_2) == [
            (FAR_PAST, 1.0),
            (FAR_PAST_2, 8.88),
        ]
    finally:
        _purge(nav_service, PRODUCT_B)


def test_ledger_writes_a_trade_per_product_in_a_real_database(ledger_service):
    """两级幂等键的核心回归：多标的方案必须产生多笔成交，而不是一笔。"""
    customer_id = f"ITEST{uuid.uuid4().hex[:10].upper()}"
    ensure_test_customer(customer_id)
    try:
        # ai_trades.plan_id 有外键指向 allocation_plans，必须先落库再采纳。
        plan_id = PostgresPlanStore().insert_plan(
            {
                "customer_id": customer_id,
                "tier": "moderate",
                "turnover_rate": 0.25,
                "nav_as_of": FAR_PAST,
            },
            [
                {
                    "product_code": PRODUCT_A,
                    "action": "reduce",
                    "target_weight": 0.20,
                    "delta_weight": -0.40,
                    "reason": "降低单一主动基金暴露",
                },
                {
                    "product_code": PRODUCT_B,
                    "action": "add",
                    "target_weight": 0.70,
                    "delta_weight": 0.30,
                    "reason": "提高宽基指数权重",
                },
            ],
        )

        ledger_service.establish(
            customer_id=customer_id,
            initial_capital=1000.0,
            holdings_summary={PRODUCT_A: 600.0, PRODUCT_B: 400.0},
            nav_as_of=FAR_PAST,
            navs={PRODUCT_A: 1.0, PRODUCT_B: 2.0},
        )

        result = ledger_service.adopt_plan(
            customer_id=customer_id,
            plan_id=plan_id,
            weights={PRODUCT_A: 0.20, PRODUCT_B: 0.70, "cash": 0.10},
            holdings={
                PRODUCT_A: {"market_value": 600.0, "priceable": True},
                PRODUCT_B: {"market_value": 400.0, "priceable": True},
            },
            navs={PRODUCT_A: 1.0, PRODUCT_B: 2.0},
            nav_as_of=FAR_PAST,
            idempotency_key=f"itest-{uuid.uuid4().hex}",
        )

        # 两笔：A 减仓、B 加仓。若共用单一幂等键，这里只会有一条。
        assert len(result.trades) == 2
        assert {trade.product_code for trade in result.trades} == {PRODUCT_A, PRODUCT_B}
        assert result.unit_nav < 1.0
    finally:
        _purge_customer(customer_id)


def _purge(nav_service: NavService, product_code: str) -> None:
    """只删本测试用到的两个哨兵日期，绝不用 ``<=`` 范围删。

    早期实现按 ``trade_date <= FAR_PAST_2`` 删，一旦哪天哨兵日期或数据口径变化，
    就会连带删掉同产品的其它行（甚至整段历史）。测试清理必须是**精确**的。
    """
    from sqlalchemy import text

    from app.db.session import ensure_schema, session_scope

    ensure_schema()
    with session_scope() as session:
        session.execute(
            text(
                "DELETE FROM finance.product_nav_history "
                "WHERE product_code = :code AND trade_date IN (:d1, :d2)"
            ),
            {"code": product_code, "d1": FAR_PAST, "d2": FAR_PAST_2},
        )


def _purge_customer(customer_id: str) -> None:
    from sqlalchemy import text

    from app.db.session import ensure_schema, session_scope

    ensure_schema()
    with session_scope() as session:
        # ai_* 表都带 ON DELETE CASCADE，删用户即可清干净。
        session.execute(
            text("DELETE FROM finance.users WHERE customer_id = :cid"), {"cid": customer_id}
        )
