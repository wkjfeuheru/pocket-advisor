"""模拟交易账户、流水、委托与持仓的数据访问层。

只做数据读写与行锁；费率与盈亏口径在 ``portfolio.service``。
``transaction()`` 直接产出 SQLAlchemy Session。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import ensure_schema, session_scope
from app.db.models.portfolio import Account, CashTransaction, Order, Position
from app.core.security import is_unique_violation


def _account_dict(row: Account) -> dict[str, Any]:
    return {
        "customer_id": row.customer_id,
        "cash_balance": row.cash_balance,
        "frozen_balance": row.frozen_balance,
        "total_deposit": row.total_deposit,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _position_dict(row: Position) -> dict[str, Any]:
    return {
        "customer_id": row.customer_id,
        "product_code": row.product_code,
        "shares": row.shares,
        "cost_amount": row.cost_amount,
        "avg_cost": row.avg_cost,
        "opened_at": row.opened_at,
        "updated_at": row.updated_at,
    }


def _order_dict(row: Order) -> dict[str, Any]:
    return {
        "order_id": row.order_id,
        "customer_id": row.customer_id,
        "product_code": row.product_code,
        "side": row.side,
        "shares": row.shares,
        "price": row.price,
        "gross_amount": row.gross_amount,
        "fee": row.fee,
        "net_amount": row.net_amount,
        "realized_pnl": row.realized_pnl,
        "fee_limitations": row.fee_limitations,
        "idempotency_key": row.idempotency_key,
        "created_at": row.created_at,
    }


def _transaction_dict(row: CashTransaction) -> dict[str, Any]:
    return {
        "txn_id": row.txn_id,
        "customer_id": row.customer_id,
        "kind": row.kind,
        "amount": row.amount,
        "balance_after": row.balance_after,
        "ref_id": row.ref_id,
        "note": row.note,
        "idempotency_key": row.idempotency_key,
        "created_at": row.created_at,
    }


class PostgresPortfolioStore:
    """模拟交易账户、流水、委托与持仓的 PostgreSQL 存储。"""

    def ensure_account(self, customer_id: str) -> dict[str, Any]:
        """确保账户行存在并返回之（幂等）。"""
        ensure_schema()
        cid = customer_id.upper()
        with session_scope() as session:
            row = session.get(Account, cid)
            if row is None:
                try:
                    with session.begin_nested():
                        row = Account(customer_id=cid)
                        session.add(row)
                        session.flush()
                except IntegrityError as exc:
                    if not is_unique_violation(exc):
                        raise
                    row = None
                for _ in range(5):
                    if row is not None:
                        break
                    session.expire_all()
                    row = session.get(Account, cid)
                    if row is None:
                        time.sleep(0.05)
                if row is None:
                    raise RuntimeError(f"账户 {cid} 并发创建失败，请重试")
            return _account_dict(row)

    def lock_account(self, session: Session, customer_id: str) -> dict[str, Any] | None:
        """在调用方事务内锁定账户行，返回其当前值。"""
        row = session.get(Account, customer_id.upper(), with_for_update=True)
        return _account_dict(row) if row else None

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """暴露事务上下文，供服务层把一次成交的多个写操作包成原子操作。"""
        ensure_schema()
        with session_scope() as session:
            yield session

    def find_transaction_by_key(self, customer_id: str, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        ensure_schema()
        with session_scope() as session:
            row = session.scalars(
                select(CashTransaction).where(
                    CashTransaction.customer_id == customer_id.upper(),
                    CashTransaction.idempotency_key == idempotency_key,
                ).limit(1)
            ).first()
            return _transaction_dict(row) if row else None

    def find_order_by_key(self, customer_id: str, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        ensure_schema()
        with session_scope() as session:
            row = session.scalars(
                select(Order).where(
                    Order.customer_id == customer_id.upper(),
                    Order.idempotency_key == idempotency_key,
                ).limit(1)
            ).first()
            return _order_dict(row) if row else None

    def insert_transaction(self, session: Session, record: dict[str, Any]) -> dict[str, Any]:
        row = CashTransaction(
            txn_id=record["txn_id"],
            customer_id=str(record["customer_id"]).upper(),
            kind=record["kind"],
            amount=record["amount"],
            balance_after=record["balance_after"],
            ref_id=record.get("ref_id", ""),
            note=record.get("note", ""),
            idempotency_key=record.get("idempotency_key", ""),
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return _transaction_dict(row)

    def insert_order(self, session: Session, record: dict[str, Any]) -> dict[str, Any]:
        row = Order(
            order_id=record["order_id"],
            customer_id=str(record["customer_id"]).upper(),
            product_code=record["product_code"],
            side=record["side"],
            shares=record["shares"],
            price=record["price"],
            gross_amount=record["gross_amount"],
            fee=record["fee"],
            net_amount=record["net_amount"],
            realized_pnl=record.get("realized_pnl"),
            fee_limitations=list(record.get("fee_limitations", [])),
            idempotency_key=record.get("idempotency_key", ""),
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return _order_dict(row)

    def update_cash_balance(self, session: Session, customer_id: str, cash_balance: float) -> None:
        row = session.get(Account, customer_id.upper())
        if row is not None:
            row.cash_balance = cash_balance

    def add_deposit(self, session: Session, customer_id: str, amount: float) -> None:
        row = session.get(Account, customer_id.upper())
        if row is not None:
            row.total_deposit = float(row.total_deposit or 0) + amount

    def get_position_row(self, session: Session, customer_id: str, product_code: str) -> dict[str, Any] | None:
        row = session.get(
            Position, (customer_id.upper(), product_code), with_for_update=True,
        )
        return _position_dict(row) if row else None

    def upsert_position(
        self, session: Session, customer_id: str, product_code: str,
        shares: float, cost_amount: float,
    ) -> None:
        cid = customer_id.upper()
        row = session.get(Position, (cid, product_code))
        if shares <= 0:
            if row is not None:
                session.delete(row)
            return
        avg_cost = cost_amount / shares if shares else 0.0
        if row is None:
            row = Position(customer_id=cid, product_code=product_code)
            session.add(row)
        row.shares = shares
        row.cost_amount = cost_amount
        row.avg_cost = avg_cost

    def list_positions(self, customer_id: str) -> list[dict[str, Any]]:
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(Position)
                .where(Position.customer_id == customer_id.upper())
                .order_by(Position.updated_at.desc(), Position.product_code)
            )
            return [_position_dict(row) for row in rows]

    def list_orders(self, customer_id: str, limit: int = 100) -> list[dict[str, Any]]:
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(Order)
                .where(Order.customer_id == customer_id.upper())
                .order_by(Order.created_at.desc(), Order.order_id.desc())
                .limit(max(1, min(int(limit), 500)))
            )
            return [_order_dict(row) for row in rows]

    def list_transactions(self, customer_id: str, limit: int = 100) -> list[dict[str, Any]]:
        ensure_schema()
        with session_scope() as session:
            rows = session.scalars(
                select(CashTransaction)
                .where(CashTransaction.customer_id == customer_id.upper())
                .order_by(CashTransaction.created_at.desc())
                .limit(max(1, min(int(limit), 500)))
            )
            return [_transaction_dict(row) for row in rows]

    def realized_pnl_total(self, customer_id: str) -> float:
        """累计已实现盈亏（卖出成交之和）。"""
        ensure_schema()
        with session_scope() as session:
            total = session.scalar(
                select(func.coalesce(func.sum(Order.realized_pnl), 0))
                .where(Order.customer_id == customer_id.upper(), Order.side == "sell")
            )
            return float(total or 0)


_portfolio_store: PostgresPortfolioStore | None = None


def get_portfolio_store() -> PostgresPortfolioStore:
    global _portfolio_store
    if _portfolio_store is None:
        _portfolio_store = PostgresPortfolioStore()
    return _portfolio_store


__all__ = ["PostgresPortfolioStore", "get_portfolio_store"]
