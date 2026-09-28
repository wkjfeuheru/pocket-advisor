"""模拟交易 ORM 映射（账户、资金流水、委托成交、持仓）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from app.db.base import Base

MONEY = Numeric(18, 2, asdecimal=False)


class Account(Base):
    """模拟交易账户资金。"""

    __tablename__ = "accounts"
    __table_args__ = {"schema": "finance"}

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cash_balance: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    frozen_balance: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    total_deposit: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class CashTransaction(Base):
    """资金流水账本；``amount`` 为带符号变动。"""

    __tablename__ = "cash_transactions"
    __table_args__ = {"schema": "finance"}

    txn_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[float] = mapped_column(MONEY, nullable=False)
    balance_after: Mapped[float] = mapped_column(MONEY, nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Order(Base):
    """已成交委托。"""

    __tablename__ = "orders"
    __table_args__ = {"schema": "finance"}

    order_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    gross_amount: Mapped[float] = mapped_column(MONEY, nullable=False)
    fee: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    net_amount: Mapped[float] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    fee_limitations: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Position(Base):
    """持仓；份额归零时删除该行（由 store 维护）。"""

    __tablename__ = "positions"
    __table_args__ = {"schema": "finance"}

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    cost_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


__all__ = ["Account", "CashTransaction", "Order", "Position"]
