"""管理后台 REST：用户总览与商品发行/上下架。全部 ``/api/admin/*``。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas.admin import (
    AdminProductListResponse,
    AdminProductUpsertRequest,
    AdminUserEntry,
    AdminUserListResponse,
    AdminUserPortfolioResponse,
)
from app.services.admin_service import get_admin_service
from app.services.portfolio_service import SIMULATED_DISCLAIMER
from app.services.product_service import ProductError
from app.core.dependencies import require_admin
from app.utils.errors import BusinessError, http_500

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _entry(row: dict[str, Any]) -> AdminUserEntry:
    return AdminUserEntry(
        customer_id=str(row.get("customer_id") or ""),
        username=str(row.get("username") or ""),
        display_name=str(row.get("display_name") or ""),
        created_at=str(row.get("created_at") or ""),
        is_admin=bool(row.get("is_admin")),
        account=row.get("account"),
    )


@router.get("/users", response_model=AdminUserListResponse)
def list_users(http_request: Request) -> AdminUserListResponse:
    require_admin(http_request)
    try:
        users = get_admin_service().list_users()
    except Exception as exc:  # noqa: BLE001
        raise http_500("读取用户列表", exc) from exc
    return AdminUserListResponse(users=[_entry(row) for row in users])


@router.get("/users/{customer_id}/portfolio", response_model=AdminUserPortfolioResponse)
def get_user_portfolio(http_request: Request, customer_id: str) -> AdminUserPortfolioResponse:
    require_admin(http_request)
    try:
        payload = get_admin_service().get_user_portfolio(customer_id)
    except Exception as exc:  # noqa: BLE001
        raise http_500("读取用户持仓", exc) from exc
    if not payload.get("exists"):
        raise HTTPException(status_code=404, detail=f"用户 {customer_id} 不存在")
    return AdminUserPortfolioResponse(
        customer_id=str(customer_id).upper(),
        user=_entry({**payload["user"], "account": payload.get("account")}),
        account=payload.get("account"),
        positions=payload.get("positions") or [],
    )


@router.get("/products", response_model=AdminProductListResponse)
def list_products(http_request: Request, product_type: str = "fund") -> AdminProductListResponse:
    require_admin(http_request)
    try:
        products = get_admin_service().list_products(product_type)
    except Exception as exc:  # noqa: BLE001
        raise http_500("读取商品列表", exc) from exc
    return AdminProductListResponse(products=products)


@router.post("/products", status_code=201)
def upsert_product(
    http_request: Request, payload: AdminProductUpsertRequest,
) -> dict[str, Any]:
    require_admin(http_request)
    try:
        product = get_admin_service().upsert_product(payload.model_dump())
    except ProductError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BusinessError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_500("保存商品", exc) from exc
    return {"product": product.model_dump(mode="json"), "disclaimer": SIMULATED_DISCLAIMER}


@router.post("/products/{code}/offline")
def offline_product(http_request: Request, code: str) -> dict[str, Any]:
    require_admin(http_request)
    try:
        product = get_admin_service().set_product_active(code, False)
    except ProductError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_500("下架", exc) from exc
    return {"product": product.model_dump(mode="json"), "message": f"商品 {code} 已下架"}


@router.post("/products/{code}/publish")
def publish_product(http_request: Request, code: str) -> dict[str, Any]:
    require_admin(http_request)
    try:
        product = get_admin_service().set_product_active(code, True)
    except ProductError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        raise http_500("上架", exc) from exc
    return {"product": product.model_dump(mode="json"), "message": f"商品 {code} 已上架"}


__all__ = ["router"]
