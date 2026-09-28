"""模拟交易的不可变领域契约与 REST 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Side = Literal["buy", "sell"]


class PositionView(BaseModel):
    """一笔持仓及其按最新净值计算的浮动盈亏。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: str
    product_name: str = ""
    product_type: str = ""
    risk_level: str = ""
    shares: float = 0.0
    cost_amount: float = 0.0
    avg_cost: float = 0.0
    nav: float | None = None
    nav_as_of: str = ""
    market_value: float | None = None
    unrealized_pnl: float | None = None
    return_rate: float | None = None
    weight: float | None = None
    opened_at: datetime | None = None
    updated_at: datetime | None = None
    pricing_status: Literal["priced", "unavailable"] = "priced"
    limitations: list[str] = Field(default_factory=list)


class OrderView(BaseModel):
    """一条已成交的委托记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    product_code: str
    product_name: str = ""
    side: Side
    shares: float
    price: float
    gross_amount: float
    fee: float
    net_amount: float
    realized_pnl: float | None = None
    fee_limitations: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class TransactionView(BaseModel):
    """一条资金流水。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    kind: Literal["deposit", "buy", "sell", "fee"]
    amount: float
    balance_after: float
    ref_id: str = ""
    note: str = ""
    created_at: datetime | None = None


class AccountSnapshot(BaseModel):
    """账户数据面板的口径。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    cash_balance: float = 0.0
    frozen_balance: float = 0.0
    market_value: float = 0.0
    total_assets: float = 0.0
    total_deposit: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float | None = None
    realized_pnl: float = 0.0
    position_pnl: float = 0.0
    position_cost: float = 0.0
    position_count: int = 0
    market_value_complete: bool = True
    pricing_issues: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None
    simulated: bool = True
    disclaimer: str = "模拟交易数据，不构成投资建议。"


class TradeResult(BaseModel):
    """一次成交的结果（下单/清仓共用）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order: OrderView
    account: AccountSnapshot
    position: PositionView | None = None
    idempotent_replay: bool = False


class LiquidationResult(BaseModel):
    """一键清仓的汇总结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    orders: list[OrderView] = Field(default_factory=list)
    cleared_codes: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)
    total_cash_in: float = 0.0
    total_realized_pnl: float = 0.0
    account: AccountSnapshot


class DepositRequest(BaseModel):
    """充值请求。"""

    amount: float = Field(..., gt=0, description="充值金额（元）")
    idempotency_key: str = Field(default="", max_length=128, description="幂等键")


class OrderRequest(BaseModel):
    """下单请求（买入按金额/份额，卖出按份额或全部）。"""

    product_code: str = Field(..., min_length=1, max_length=32, description="商品代码")
    side: Literal["buy", "sell"] = Field(..., description="买卖方向")
    amount: float | None = Field(default=None, gt=0, description="买入金额（元）")
    shares: float | None = Field(default=None, gt=0, description="份额")
    all: bool = Field(default=False, description="卖出时是否全部赎回")
    idempotency_key: str = Field(default="", max_length=128, description="幂等键")


class LiquidationRequest(BaseModel):
    """一键清仓请求。"""

    idempotency_key: str = Field(default="", max_length=128, description="幂等键")


class PositionListResponse(BaseModel):
    """持仓列表。"""

    customer_id: str
    positions: list[PositionView] = Field(default_factory=list)
    account: AccountSnapshot
    simulated: bool = True


class OrderListResponse(BaseModel):
    """成交记录。"""

    customer_id: str
    orders: list[OrderView] = Field(default_factory=list)
    simulated: bool = True


class TransactionListResponse(BaseModel):
    """资金流水。"""

    customer_id: str
    transactions: list[TransactionView] = Field(default_factory=list)
    simulated: bool = True


class DepositResponse(BaseModel):
    """充值结果。"""

    transaction: TransactionView
    account: AccountSnapshot
    idempotent_replay: bool = False
    simulated: bool = True


class TradeResponse(TradeResult):
    """下单结果。"""


class LiquidationResponse(LiquidationResult):
    """清仓结果。"""


__all__ = [
    "AccountSnapshot",
    "DepositRequest",
    "DepositResponse",
    "LiquidationRequest",
    "LiquidationResponse",
    "LiquidationResult",
    "OrderListResponse",
    "OrderRequest",
    "OrderView",
    "PositionListResponse",
    "PositionView",
    "Side",
    "TradeResponse",
    "TradeResult",
    "TransactionListResponse",
    "TransactionView",
]
