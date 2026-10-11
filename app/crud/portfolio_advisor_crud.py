"""净值与 AI 账本的 PostgreSQL 存储。

两条语义只能在真库上成立，因此都写在这里而不是服务层：

1. **真数据优先覆盖合成。** 靠 ``ON CONFLICT (product_code, trade_date) DO UPDATE``
   加 ``WHERE`` 条件实现。服务层的内存假存储只是模拟这条规则；线上由本模块负责，
   并且**两个方向都要成立**：真能压合成，合成压不了真。
2. **两级幂等键的成交级唯一。** ``UNIQUE (customer_id, trade_key)``。若所有成交共用
   一个键，采纳一个多标的方案时第二笔就会撞约束并被误判成重复请求 —— 结果是
   只成交一笔且不报错。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
from datetime import date, datetime
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.security import UniqueConstraintError, is_unique_violation
from app.db.models.portfolio_advisor import (
    AiAccount,
    AiPortfolioSnapshot,
    AiPosition,
    AiTrade,
    AllocationPlan,
    AllocationGeneration,
    AllocationPlanItem,
    ProductNavHistory,
)
from app.db.models.user import User
from app.db.session import ensure_schema, session_scope
from app.services.portfolio_advisor.nav import REAL, SYNTHETIC, NavRow

#: 未设置密码的占位口令散列。集成测试用户不用于登录。
_PLACEHOLDER_HASH = "!"


def _money(value: Any) -> float:
    return float(value or 0.0)


def ensure_test_customer(customer_id: str, *, display_name: str = "集成测试用户") -> str:
    """确保 ``finance.users`` 里有这个客户（AI 账本有外键指向它）。

    仅用于集成测试与本地演示；生产路径的用户由注册接口创建。

    注意 ``User`` 的主键是自增 ``id``，``customer_id`` 只是唯一键，因此必须用
    ``select`` 而不是 ``session.get``（后者会把 customer_id 当 bigint 主键解析）。
    """
    ensure_schema()
    cid = customer_id.upper()
    with session_scope() as session:
        existing = session.scalars(
            select(User).where(User.customer_id == cid).limit(1)
        ).first()
        if existing is not None:
            return cid
        session.add(
            User(
                customer_id=cid,
                username=cid.lower(),
                display_name=display_name,
                password_hash=_PLACEHOLDER_HASH,
                salt=_PLACEHOLDER_HASH,
                created_at=datetime.now().isoformat(),
            )
        )
    return cid


# ── 净值 ──────────────────────────────────────────────────────────────


class PostgresNavStore:
    """实现 ``NavStore`` 协议。"""

    def upsert_many(self, rows: Sequence[NavRow]) -> int:
        if not rows:
            return 0
        ensure_schema()
        written = 0
        with session_scope() as session:
            for row in rows:
                statement = pg_insert(ProductNavHistory).values(
                    product_code=row.product_code,
                    trade_date=row.trade_date,
                    nav=float(row.nav),
                    data_source=row.data_source,
                    updated_at=func.now(),
                )
                # 覆盖规则：真数据可以覆盖任何来源；合成数据只能覆盖合成数据。
                # 换句话说，合成永远压不过真数据。
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        ProductNavHistory.product_code,
                        ProductNavHistory.trade_date,
                    ],
                    set_={
                        "nav": statement.excluded.nav,
                        "data_source": statement.excluded.data_source,
                        "updated_at": func.now(),
                    },
                    where=(
                        (statement.excluded.data_source == REAL)
                        | (ProductNavHistory.__table__.c.data_source == SYNTHETIC)
                    ),
                )
                # 必须用 RETURNING 计数：这种带 WHERE 的 ON CONFLICT DO UPDATE
                # 形式下 psycopg 的 rowcount 是 -1（语句未给出精确计数），
                # 直接累加会得到负数。
                result = session.execute(statement.returning(ProductNavHistory.nav))
                written += len(result.fetchall())
        return written

    def history(
        self, product_code: str, start: date, end: date
    ) -> list[tuple[date, float]]:
        ensure_schema()
        with session_scope() as session:
            rows = session.execute(
                select(ProductNavHistory.trade_date, ProductNavHistory.nav)
                .where(
                    ProductNavHistory.product_code == product_code,
                    ProductNavHistory.trade_date >= start,
                    ProductNavHistory.trade_date <= end,
                )
                .order_by(ProductNavHistory.trade_date.asc())
            ).all()
        return [(row[0], float(row[1])) for row in rows]

    def latest(self, product_code: str) -> tuple[date, float] | None:
        ensure_schema()
        with session_scope() as session:
            row = session.execute(
                select(ProductNavHistory.trade_date, ProductNavHistory.nav)
                .where(ProductNavHistory.product_code == product_code)
                .order_by(ProductNavHistory.trade_date.desc())
                .limit(1)
            ).first()
        return (row[0], float(row[1])) if row else None

    def delete_synthetic(self, product_code: str | None = None) -> int:
        ensure_schema()
        with session_scope() as session:
            statement = delete(ProductNavHistory).where(
                ProductNavHistory.data_source == SYNTHETIC
            )
            if product_code:
                statement = statement.where(ProductNavHistory.product_code == product_code)
            result = session.execute(statement)
        return result.rowcount or 0


# ── AI 账本 ───────────────────────────────────────────────────────────


def _account_dict(row: AiAccount) -> dict[str, Any]:
    return {
        "customer_id": row.customer_id,
        "initial_capital": _money(row.initial_capital),
        "opening_plan_id": row.opening_plan_id,
        "cash_balance": _money(row.cash_balance),
        "unit_nav": float(row.unit_nav or 0.0),
        "total_units": float(row.total_units or 0.0),
        "cumulative_inflow": _money(row.cumulative_inflow),
        "cumulative_outflow": _money(row.cumulative_outflow),
        "nav_as_of": row.nav_as_of,
    }


def _position_dict(row: AiPosition) -> dict[str, Any]:
    return {
        "customer_id": row.customer_id,
        "product_code": row.product_code,
        "shares": float(row.shares or 0.0),
        "cost_amount": _money(row.cost_amount),
        "avg_cost": float(row.avg_cost or 0.0),
    }


class PostgresLedgerStore:
    """实现 ``LedgerStore`` 协议。

    刻意与 ``PostgresPortfolioStore`` 分开：AI 账本绝不能拿到写用户持仓/委托的入口。
    """

    def establish_account(
        self, customer_id: str, *, initial_capital: float, opening_plan_id: str, nav_as_of: date
    ) -> dict:
        ensure_schema()
        cid = customer_id.upper()
        with session_scope() as session:
            row = session.get(AiAccount, cid)
            if row is None:
                row = AiAccount(
                    customer_id=cid,
                    initial_capital=initial_capital,
                    opening_plan_id=opening_plan_id or None,
                    cash_balance=0,
                    unit_nav=1.0,
                    total_units=initial_capital,
                    nav_as_of=nav_as_of,
                )
                session.add(row)
                session.flush()
            return _account_dict(row)

    def get_account(self, customer_id: str) -> dict | None:
        ensure_schema()
        with session_scope() as session:
            row = session.get(AiAccount, customer_id.upper())
            return _account_dict(row) if row else None

    def update_account(self, customer_id: str, **fields) -> dict:
        ensure_schema()
        cid = customer_id.upper()
        with session_scope() as session:
            row = session.get(AiAccount, cid)
            if row is None:
                raise KeyError(cid)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.flush()
            return _account_dict(row)

    def get_position(self, customer_id: str, product_code: str) -> dict | None:
        ensure_schema()
        with session_scope() as session:
            row = session.get(AiPosition, (customer_id.upper(), product_code))
            return _position_dict(row) if row else None

    def list_positions(self, customer_id: str) -> list[dict]:
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(AiPosition).where(AiPosition.customer_id == customer_id.upper())
            ).all()
        return [_position_dict(row) for row in rows]

    def upsert_position(
        self, customer_id: str, product_code: str, *, shares: float, cost_amount: float
    ) -> None:
        ensure_schema()
        cid = customer_id.upper()
        with session_scope() as session:
            row = session.get(AiPosition, (cid, product_code))
            if shares <= 0:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = AiPosition(
                    customer_id=cid,
                    product_code=product_code,
                    shares=shares,
                    cost_amount=cost_amount,
                    avg_cost=cost_amount / shares if shares else 0.0,
                )
                session.add(row)
            else:
                row.shares = shares
                row.cost_amount = cost_amount
                row.avg_cost = cost_amount / shares if shares else 0.0
                row.updated_at = datetime.now()

    def insert_trade(self, record: Mapping[str, object]) -> dict:
        ensure_schema()
        payload = dict(record)
        trade_key = str(payload.pop("trade_key", "") or "")
        adoption_key = str(payload.pop("adoption_key", "") or "")
        payload.pop("idempotency_key", None)
        row = AiTrade(
            trade_id=str(uuid4()),
            trade_key=trade_key,
            adoption_key=adoption_key,
            **payload,
        )
        with session_scope() as session:
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                if not is_unique_violation(exc):
                    raise
                # 撞到 UNIQUE (customer_id, trade_key)：让上层走幂等重放分支。
                raise UniqueConstraintError() from exc
        return {"trade_id": row.trade_id, "trade_key": trade_key}

    def find_trade_by_key(self, customer_id: str, adoption_key: str) -> dict | None:
        if not adoption_key:
            return None
        ensure_schema()
        with session_scope() as session:
            row = session.scalars(
                select(AiTrade)
                .where(
                    AiTrade.customer_id == customer_id.upper(),
                    AiTrade.adoption_key == adoption_key,
                )
                .limit(1)
            ).first()
        if row is None:
            return None
        return {"trade_id": row.trade_id, "trade_key": row.trade_key}

    def mark_plan_adopted(self, plan_id: str, *, adopted_at: datetime) -> bool:
        """原子抢占方案状态：仅当尚未 adopted 时才写入，返回是否抢到。

        并发或重复采纳时只有一个请求能返回 ``True``，其余为 ``False``，
        从而堵住"先查状态、再写账本"之间的 TOCTOU 窗口。
        """
        ensure_schema()
        with session_scope() as session:
            result = session.execute(
                update(AllocationPlan)
                .where(AllocationPlan.plan_id == plan_id)
                .where(AllocationPlan.status != "adopted")
                .values(status="adopted", adopted_at=adopted_at)
            )
            return bool(result.rowcount)

    def get_plan(self, plan_id: str) -> dict | None:
        ensure_schema()
        with session_scope() as session:
            row = session.get(AllocationPlan, plan_id)
        if row is None:
            return None
        return {
            "plan_id": row.plan_id,
            "status": row.status,
            "adopted_at": row.adopted_at,
        }


__all__ = [
    "PostgresLedgerStore",
    "PostgresNavStore",
    "PostgresPlanStore",
    "PostgresSnapshotStore",
    "ensure_test_customer",
    "get_ledger_store",
    "get_nav_store",
    "get_plan_store",
    "get_snapshot_store",
]

# ── 方案 ──────────────────────────────────────────────────────────────


class PostgresPlanStore:
    """方案与明细的持久化。

    方案必须**先落库再采纳**：``ai_trades.plan_id`` 有外键指向 ``allocation_plans``，
    成交不能引用一个不存在的方案。
    """

    def __init__(self) -> None:
        self._generation_session: ContextVar[Any] = ContextVar("allocation_generation_session", default=None)

    @contextmanager
    def generation_context(self, customer_id: str, *, request_id: str | None = None,
                           session_id: str | None = None, task_id: str = ""):
        """整个生成批次和完成快照共用一个事务；同一可信键跨进程串行。"""
        ensure_schema()
        if self._generation_session.get() is not None:
            raise RuntimeError("allocation_generation_context_already_bound")
        with session_scope() as session:
            if request_id and session_id and task_id:
                packed = json.dumps([customer_id.upper(), str(UUID(request_id)), str(UUID(session_id)), task_id], ensure_ascii=False)
                lock_key = int.from_bytes(hashlib.sha256(packed.encode()).digest()[:8], "big", signed=True)
                session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
            token = self._generation_session.set(session)
            try:
                yield
            finally:
                self._generation_session.reset(token)

    @contextmanager
    def _session(self):
        bound = self._generation_session.get()
        if bound is not None:
            yield bound
        else:
            with session_scope() as session:
                yield session

    def get_generation(self, customer_id: str, *, request_id: str, session_id: str, task_id: str) -> dict | None:
        ensure_schema()
        with self._session() as session:
            row = session.get(AllocationGeneration, (customer_id.upper(), request_id, session_id, task_id))
            return deepcopy(row.response) if row is not None else None

    def save_generation(self, customer_id: str, response: Mapping[str, Any], *, request_id: str,
                        session_id: str, task_id: str) -> dict:
        session = self._generation_session.get()
        if session is None:
            raise RuntimeError("allocation_generation_context_not_bound")

        def encode_date(value):
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            raise TypeError("unsupported_allocation_snapshot_value")

        snapshot = json.loads(json.dumps(dict(response), ensure_ascii=False, default=encode_date))
        session.add(AllocationGeneration(customer_id=customer_id.upper(), request_id=request_id,
                                        session_id=session_id, task_id=task_id, response=snapshot))
        session.flush()
        return deepcopy(snapshot)

    def insert_plan(self, plan: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> str:
        ensure_schema()
        payload = dict(plan)
        plan_id = str(payload.get("plan_id") or uuid4())
        with self._session() as session:
            session.add(
                AllocationPlan(
                    plan_id=plan_id,
                    customer_id=str(payload["customer_id"]).upper(),
                    request_id=payload.get("request_id") or None,
                    session_id=payload.get("session_id") or None,
                    task_id=str(payload.get("task_id") or ""),
                    tier=str(payload["tier"]),
                    status=str(payload.get("status") or "offered"),
                    turnover_rate=payload.get("turnover_rate"),
                    estimated_fee=payload.get("estimated_fee"),
                    risk_level_before=payload.get("risk_level_before"),
                    risk_level_after=payload.get("risk_level_after"),
                    volatility_before=payload.get("volatility_before"),
                    volatility_after=payload.get("volatility_after"),
                    concentration_after=payload.get("concentration_after"),
                    rationale=str(payload.get("rationale") or ""),
                    repairs=list(payload.get("repairs") or []),
                    limitations=list(payload.get("limitations") or []),
                    unavailable_reason=str(payload.get("unavailable_reason") or ""),
                    nav_as_of=payload.get("nav_as_of"),
                )
            )
            for item in items:
                session.add(
                    AllocationPlanItem(
                        plan_id=plan_id,
                        product_code=str(item["product_code"]),
                        product_name=str(item.get("product_name") or ""),
                        asset_class=str(item.get("asset_class") or ""),
                        action=str(item.get("action") or "hold"),
                        current_weight=item.get("current_weight"),
                        target_weight=float(item.get("target_weight") or 0.0),
                        delta_weight=float(item.get("delta_weight") or 0.0),
                        current_amount=item.get("current_amount"),
                        target_amount=float(item.get("target_amount") or 0.0),
                        reason=str(item.get("reason") or ""),
                    )
                )
            session.flush()
        return plan_id

    def get_plan(self, plan_id: str) -> dict | None:
        ensure_schema()
        with self._session() as session:
            row = session.get(AllocationPlan, plan_id)
            if row is None:
                return None
            items = session.scalars(
                select(AllocationPlanItem)
                .where(AllocationPlanItem.plan_id == plan_id)
                .order_by(AllocationPlanItem.id.asc())
            ).all()
            return {
                "plan_id": row.plan_id,
                "customer_id": row.customer_id,
                "request_id": row.request_id,
                "session_id": row.session_id,
                "task_id": row.task_id,
                "tier": row.tier,
                "status": row.status,
                "turnover_rate": row.turnover_rate,
                "estimated_fee": _money(row.estimated_fee),
                "rationale": row.rationale,
                "limitations": list(row.limitations or []),
                "unavailable_reason": row.unavailable_reason,
                "adopted_at": row.adopted_at,
                "nav_as_of": row.nav_as_of,
                "items": [
                    {
                        "product_code": item.product_code,
                        "action": item.action,
                        "current_weight": item.current_weight,
                        "target_weight": item.target_weight,
                        "delta_weight": item.delta_weight,
                        "reason": item.reason,
                        "current_amount": item.current_amount,
                        "target_amount": item.target_amount,
                    }
                    for item in items
                ],
            }

    def find_generation(self, customer_id: str, *, request_id: str, session_id: str, task_id: str) -> list[dict]:
        """列出任务方案；完整生成与重放只能以 get_generation 快照为准。"""
        ensure_schema()
        with self._session() as session:
            ids = list(session.scalars(select(AllocationPlan.plan_id)
                .where(AllocationPlan.customer_id == customer_id.upper())
                .where(AllocationPlan.request_id == request_id)
                .where(AllocationPlan.session_id == session_id)
                .where(AllocationPlan.task_id == task_id)
                .order_by(AllocationPlan.created_at.asc(), AllocationPlan.plan_id.asc())))
        return [plan for plan_id in ids if (plan := self.get_plan(plan_id)) is not None]

    def list_plans(self, customer_id: str, *, limit: int = 50) -> list[dict]:
        ensure_schema()
        with self._session() as session:
            rows = session.scalars(
                select(AllocationPlan)
                .where(AllocationPlan.customer_id == customer_id.upper())
                .order_by(AllocationPlan.created_at.desc())
                .limit(max(1, min(limit, 200)))
            ).all()
            return [
                {
                    "plan_id": row.plan_id,
                    "tier": row.tier,
                    "status": row.status,
                    "turnover_rate": row.turnover_rate,
                    "estimated_fee": _money(row.estimated_fee),
                    "unavailable_reason": row.unavailable_reason,
                    "created_at": row.created_at,
                }
                for row in rows
            ]


def get_nav_store() -> PostgresNavStore:
    return PostgresNavStore()


def get_ledger_store() -> PostgresLedgerStore:
    return PostgresLedgerStore()


def get_plan_store() -> PostgresPlanStore:
    return PostgresPlanStore()


# ── 每日快照 ──────────────────────────────────────────────────────────


class PostgresSnapshotStore:
    """用户线与 AI 线共用的每日快照。``owner`` 区分两条线。"""

    def list_snapshots(
        self,
        customer_id: str,
        owner: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[tuple[date, float]]:
        ensure_schema()
        with session_scope() as session:
            statement = select(
                AiPortfolioSnapshot.snapshot_date, AiPortfolioSnapshot.unit_nav
            ).where(
                AiPortfolioSnapshot.customer_id == customer_id.upper(),
                AiPortfolioSnapshot.owner == owner,
            )
            if start is not None:
                statement = statement.where(AiPortfolioSnapshot.snapshot_date >= start)
            if end is not None:
                statement = statement.where(AiPortfolioSnapshot.snapshot_date <= end)
            rows = session.execute(
                statement.order_by(AiPortfolioSnapshot.snapshot_date.asc())
            ).all()
        return [(row[0], float(row[1])) for row in rows]

    def latest_snapshot(self, customer_id: str, owner: str) -> dict | None:
        ensure_schema()
        with session_scope() as session:
            row = session.scalars(
                select(AiPortfolioSnapshot)
                .where(
                    AiPortfolioSnapshot.customer_id == customer_id.upper(),
                    AiPortfolioSnapshot.owner == owner,
                )
                .order_by(AiPortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return {
            "snapshot_date": row.snapshot_date,
            "unit_nav": float(row.unit_nav),
            "total_assets": _money(row.total_assets),
            # 基线随快照继承，读回来才能保证跨天稳定。
            "baseline_units": (float(row.baseline_units) if row.baseline_units else None),
        }

    def upsert_snapshot(
        self, customer_id: str, owner: str, record: Mapping[str, Any]
    ) -> None:
        """幂等写入：(customer_id, owner, snapshot_date) 冲突即更新。"""
        ensure_schema()
        cid = customer_id.upper()
        payload = dict(record)
        snapshot_date = payload.pop("snapshot_date")
        with session_scope() as session:
            statement = pg_insert(AiPortfolioSnapshot).values(
                customer_id=cid,
                owner=owner,
                snapshot_date=snapshot_date,
                total_assets=payload.get("total_assets") or 0,
                cash_balance=payload.get("cash_balance") or 0,
                market_value=payload.get("market_value") or 0,
                unit_nav=payload.get("unit_nav") or 0,
                baseline_units=payload.get("baseline_units"),
                daily_return=payload.get("daily_return"),
                cumulative_return=payload.get("cumulative_return"),
                max_drawdown=payload.get("max_drawdown"),
                annualized_volatility=payload.get("annualized_volatility"),
                nav_as_of=payload.get("nav_as_of"),
                limitations=list(payload.get("limitations") or []),
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        AiPortfolioSnapshot.customer_id,
                        AiPortfolioSnapshot.owner,
                        AiPortfolioSnapshot.snapshot_date,
                    ],
                    set_={
                        "total_assets": statement.excluded.total_assets,
                        "cash_balance": statement.excluded.cash_balance,
                        "market_value": statement.excluded.market_value,
                        "unit_nav": statement.excluded.unit_nav,
                        "baseline_units": statement.excluded.baseline_units,
                        "daily_return": statement.excluded.daily_return,
                        "max_drawdown": statement.excluded.max_drawdown,
                        "annualized_volatility": statement.excluded.annualized_volatility,
                        "nav_as_of": statement.excluded.nav_as_of,
                        "limitations": statement.excluded.limitations,
                    },
                )
            )


def get_snapshot_store() -> PostgresSnapshotStore:
    return PostgresSnapshotStore()
