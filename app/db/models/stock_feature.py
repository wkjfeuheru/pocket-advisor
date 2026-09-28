"""股票特征快照 ORM。表结构以 migrations/004_stock_features.sql 为准。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StockFeatureSnapshot(Base):
    """一次横截面发布。查询只读 status=ready 的行。"""

    __tablename__ = "stock_feature_snapshots"
    __table_args__ = {"schema": "finance"}

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    source_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockFeature(Base):
    """快照内一只股票的过滤字段与因子。"""

    __tablename__ = "stock_features"
    __table_args__ = {"schema": "finance"}

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("finance.stock_feature_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    thscode: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    industry: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    trade_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    listed_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_turnover_20: Mapped[Optional[float]] = mapped_column(Numeric(20, 2), nullable=True)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filter_flags: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


__all__ = ["StockFeature", "StockFeatureSnapshot"]
