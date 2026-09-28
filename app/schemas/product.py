"""货架视图与商品 REST 模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SourcedValue(BaseModel):
    """带来源与截止日的数值；用于净值等可溯源字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float | None = None
    source: str = "unavailable"
    as_of: str = ""


class ProductView(BaseModel):
    """商品货架上的一项（含用于购买决策的费率与最新净值）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    name: str
    type: str = "fund"
    risk_level: str = ""
    company: str = ""
    manager: str = ""
    scale: float | None = None
    nav: SourcedValue = Field(default_factory=SourcedValue)
    return_1y: float | None = None
    max_drawdown: float | None = None
    sharpe_ratio: float | None = None
    subscription_fee: float | None = None
    redemption_fee: str = ""
    recommended_holding_period: str = ""
    investment_target: str = ""
    is_active: bool = True
    tradable: bool = False
    limitations: list[str] = Field(default_factory=list)


class ProductShelfResponse(BaseModel):
    """商品货架。"""

    products: list[ProductView] = Field(default_factory=list)
    simulated: bool = True


__all__ = ["ProductShelfResponse", "ProductView", "SourcedValue"]
