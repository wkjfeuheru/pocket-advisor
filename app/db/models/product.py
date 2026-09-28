"""产品库 ORM 映射（``finance.products`` 及持仓/业绩子表）。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Boolean, Float, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Product(Base):
    """货架商品（基金、ETF、理财产品）。费率列一律以百分比数字存储（``1.5`` 表示 1.5%）。"""

    __tablename__ = "products"
    __table_args__ = {"schema": "finance"}

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="fund")
    establish_date: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    scale: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    manager: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    company: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    management_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    custody_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    subscription_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    redemption_fee: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    recommended_holding_period: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    investment_target: Mapped[str] = mapped_column(Text, nullable=False, default="")
    investment_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 起投金额（元）。NULL 表示未披露，读取端必须区分「未知」与 0。
    min_investment: Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProductHolding(Base):
    """产品前十大重仓（按 ``rank`` 排序）。"""

    __tablename__ = "product_holdings"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(128), nullable=False)
    stock_code: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    report_date: Mapped[str] = mapped_column(String(32), nullable=False, default="")


class ProductPerformance(Base):
    """产品业绩与净值快照；读取时取 ``id`` 最大的一条为最新。"""

    __tablename__ = "product_performance"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    nav: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_3m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_6m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_1y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_3y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    update_date: Mapped[str] = mapped_column(String(32), nullable=False, default="")


__all__ = ["Product", "ProductHolding", "ProductPerformance"]
