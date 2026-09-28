"""模拟交易 REST：账户、持仓、充值、下单、清仓、流水。仅普通用户。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas.portfolio import (
    DepositRequest,
    DepositResponse,
    LiquidationRequest,
    LiquidationResponse,
    OrderListResponse,
    OrderRequest,
    PositionListResponse,
    TradeResponse,
    TransactionListResponse,
)
from app.services.portfolio_service import PortfolioError, PortfolioService, get_portfolio_service
from app.core.dependencies import require_user
from app.utils.errors import BusinessError, http_500

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_STATUS_BY_CODE = {
    "product_not_found": 404,
    "invalid_amount": 400,
    "pricing_unavailable": 409,
    "insufficient_funds": 409,
    "insufficient_shares": 409,
    "no_position": 409,
    "product_offline": 409,
}


def get_service() -> PortfolioService:
    return get_portfolio_service()


def _fail(exc: BusinessError) -> HTTPException:
    status = _STATUS_BY_CODE.get(exc.code, 400)
    return HTTPException(status_code=status, detail=f"{exc.message}（{exc.code}）")


def _guard(action: str, call: Any) -> Any:
    try:
        return call()
    except BusinessError as exc:
        raise _fail(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise http_500(action, exc) from exc


@router.get("/account")
def get_account(http_request: Request) -> dict[str, Any]:
    customer_id = require_user(http_request)
    snapshot = _guard("读取账户", lambda: get_service().get_account(customer_id))
    return snapshot.model_dump(mode="json")


@router.get("/positions", response_model=PositionListResponse)
def list_positions(http_request: Request) -> PositionListResponse:
    customer_id = require_user(http_request)
    service = get_service()
    positions = _guard("读取持仓", lambda: service.list_positions(customer_id))
    account = _guard("读取账户", lambda: service.get_account(customer_id))
    return PositionListResponse(
        customer_id=str(customer_id).upper(), positions=positions, account=account,
    )


@router.post("/deposit", response_model=DepositResponse)
def deposit(http_request: Request, payload: DepositRequest) -> DepositResponse:
    customer_id = require_user(http_request)
    txn, account, replay = _guard(
        "充值",
        lambda: get_service().deposit(
            customer_id, payload.amount, idempotency_key=payload.idempotency_key,
        ),
    )
    return DepositResponse(transaction=txn, account=account, idempotent_replay=replay)


@router.post("/orders", response_model=TradeResponse)
def create_order(http_request: Request, payload: OrderRequest) -> TradeResponse:
    customer_id = require_user(http_request)
    service = get_service()

    def _place() -> Any:
        if payload.side == "buy":
            return service.buy(
                customer_id, payload.product_code,
                amount=payload.amount, shares=payload.shares,
                idempotency_key=payload.idempotency_key,
            )
        if payload.amount is not None:
            raise PortfolioError("卖出请使用份额（shares）或全部赎回（all）", code="invalid_amount")
        return service.sell(
            customer_id, payload.product_code,
            shares=payload.shares, all_shares=payload.all,
            idempotency_key=payload.idempotency_key,
        )

    return TradeResponse(**_guard("下单", _place).model_dump())


@router.post("/liquidate", response_model=LiquidationResponse)
def liquidate(http_request: Request, payload: LiquidationRequest) -> LiquidationResponse:
    customer_id = require_user(http_request)
    result = _guard("清仓", lambda: get_service().liquidate_all(customer_id))
    return LiquidationResponse(**result.model_dump())


@router.get("/orders", response_model=OrderListResponse)
def list_orders(http_request: Request, limit: int = 100) -> OrderListResponse:
    customer_id = require_user(http_request)
    orders = _guard("读取成交记录", lambda: get_service().list_orders(customer_id, limit))
    return OrderListResponse(customer_id=str(customer_id).upper(), orders=orders)


@router.get("/transactions", response_model=TransactionListResponse)
def list_transactions(http_request: Request, limit: int = 100) -> TransactionListResponse:
    customer_id = require_user(http_request)
    items = _guard("读取资金流水", lambda: get_service().list_transactions(customer_id, limit))
    return TransactionListResponse(customer_id=str(customer_id).upper(), transactions=items)


__all__ = ["get_service", "router"]
