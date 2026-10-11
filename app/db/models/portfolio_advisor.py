"""持仓配置与 AI 账本的 ORM 映射。

表结构的**唯一事实源**是 ``migrations/006_portfolio_advisor.sql``；这里只是映射，
改列必须同步改迁移（``tests/test_agents/test_portfolio_migration.py`` 会守住这点）。

数量口径对照（容易混）：

- 方案与明细的权重是 **0–1 小数**；
- ``finance.positions`` / ``PositionView.weight`` 是**百分数**（0–100）；
- 金额列一律 ``numeric(18, 2)``（``asdecimal=False``，读出来就是 float）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from app.db.base import Base

MONEY = Numeric(18, 2, asdecimal=False)

SCHEMA = "finance"


class ProductNavHistory(Base):
    """日频净值历史；唯一价格事实源。"""

    __tablename__ = "product_nav_history"
    __table_args__ = {"schema": SCHEMA}

    product_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    data_source: Mapped[str] = mapped_column(String(16), nullable=False, default="real")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AllocationPlan(Base):
    """一份持仓配置方案。"""

    __tablename__ = "allocation_plans"
    __table_args__ = {"schema": SCHEMA}

    plan_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offered")
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estimated_fee: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    risk_level_before: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    risk_level_after: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    volatility_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volatility_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    concentration_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    repairs: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    limitations: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    unavailable_reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    nav_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    adopted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AllocationGeneration(Base):
    """已完整提交的任务生成快照；复合主键同时保护同键唯一。"""

    __tablename__ = "allocation_generations"
    __table_args__ = {"schema": SCHEMA}

    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("finance.users.customer_id", ondelete="CASCADE"), primary_key=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    session_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AllocationPlanItem(Base):
    """方案里的单个标的调仓明细。"""

    __tablename__ = "allocation_plan_items"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    current_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_weight: Mapped[float] = mapped_column(Float, nullable=False)
    delta_weight: Mapped[float] = mapped_column(Float, nullable=False)
    current_amount: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    target_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class AiAccount(Base):
    """AI 账户。

    ``total_units`` 是**份额**（初始本金按 1 份 = 1 元换算），不是本金也不是总资产。
    """

    __tablename__ = "ai_accounts"
    __table_args__ = {"schema": SCHEMA}

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    initial_capital: Mapped[float] = mapped_column(MONEY, nullable=False)
    opening_plan_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    cash_balance: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    unit_nav: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    total_units: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    cumulative_inflow: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    cumulative_outflow: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    nav_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AiPosition(Base):
    """AI 持仓；字段与 ``finance.positions`` 同名同型以便对照。"""

    __tablename__ = "ai_positions"
    __table_args__ = {"schema": SCHEMA}

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    cost_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AiTrade(Base):
    """AI 成交，append-only。

    两级幂等键见迁移文件注释：``adoption_key`` 管"这次采纳是否处理过"，
    ``trade_key`` 管"这笔成交是否已写入"。
    """

    __tablename__ = "ai_trades"
    __table_args__ = {"schema": SCHEMA}

    trade_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    gross_amount: Mapped[float] = mapped_column(MONEY, nullable=False)
    fee: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    net_amount: Mapped[float] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    nav_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), nullable=False, default="real")
    adoption_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    trade_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AiPortfolioSnapshot(Base):
    """用户线与 AI 线共用的每日快照；``owner`` 区分两条线。"""

    __tablename__ = "ai_portfolio_snapshots"
    __table_args__ = {"schema": SCHEMA}

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_assets: Mapped[float] = mapped_column(MONEY, nullable=False)
    cash_balance: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    market_value: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    unit_nav: Mapped[float] = mapped_column(Float, nullable=False)
    #: 首日定下的份额锚，跨天继承不变；没有它 unit_nav 会恒等于 1。
    baseline_units: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    daily_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cumulative_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    annualized_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    nav_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    limitations: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "AiAccount",
    "AiPortfolioSnapshot",
    "AiPosition",
    "AiTrade",
    "AllocationPlan",
    "AllocationGeneration",
    "AllocationPlanItem",
    "MONEY",
    "ProductNavHistory",
]
