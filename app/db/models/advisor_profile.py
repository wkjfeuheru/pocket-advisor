"""用户风险画像 ORM。每个客户一行，只由用户通过表单写入。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdvisorProfile(Base):
    """长期风险画像。模型不得改写本表。"""

    __tablename__ = "advisor_profiles"
    __table_args__ = {"schema": "finance"}

    customer_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("finance.users.customer_id", ondelete="CASCADE"),
        primary_key=True,
    )
    risk_level: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    investment_horizon: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    investment_goal: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    liquidity_need: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    budget_amount: Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AdvisorProfile"]
