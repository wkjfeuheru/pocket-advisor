"""用户只读商品货架 REST。路径仍挂在 ``/api/portfolio/products``。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas.product import ProductShelfResponse, ProductView
from app.services.product_service import ProductService, get_product_service
from app.core.dependencies import require_user
from app.utils.errors import BusinessError, http_500

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def get_service() -> ProductService:
    return get_product_service()


def _fail(exc: BusinessError) -> HTTPException:
    status = 404 if exc.code == "product_not_found" else 400
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


@router.get("/products", response_model=ProductShelfResponse)
def list_products(http_request: Request, product_type: str = "fund") -> ProductShelfResponse:
    """商品货架；取不到净值的商品照常展示但标记为不可交易。"""
    require_user(http_request)
    products: list[ProductView] = _guard(
        "读取商品货架", lambda: get_service().list_products(product_type)
    )
    return ProductShelfResponse(products=products)


@router.get("/products/{code}", response_model=ProductView)
def get_product(http_request: Request, code: str) -> ProductView:
    """单个商品详情（含费率与最新净值）。"""
    require_user(http_request)
    return _guard("读取商品", lambda: get_service().get_product(code))


__all__ = ["get_service", "router"]
