"""管理后台业务层：用户总览与商品运维。不重算账户数字。"""

from __future__ import annotations

from typing import Any

from app.schemas.portfolio import AccountSnapshot
from app.services.portfolio_service import PortfolioService, get_portfolio_service
from app.schemas.product import ProductView
from app.services.product_service import ProductError, ProductService, get_product_service
from app.utils.errors import BusinessError

_PERFORMANCE_FIELDS = (
    "nav", "return_1m", "return_3m", "return_6m", "return_1y", "return_3y",
    "max_drawdown", "volatility", "sharpe_ratio",
)


class AdminService:
    """管理后台的读与运维写。授权由路由层 ``require_admin`` 负责。"""

    def __init__(
        self,
        auth_store: Any = None,
        portfolio: PortfolioService | None = None,
        products: ProductService | None = None,
    ) -> None:
        self._auth_store = auth_store
        self._portfolio = portfolio
        self._products = products

    @property
    def auth_store(self) -> Any:
        if self._auth_store is not None:
            return self._auth_store
        from app.crud.user_crud import get_user_store

        return get_user_store()

    @property
    def portfolio(self) -> PortfolioService:
        if self._portfolio is not None:
            return self._portfolio
        return get_portfolio_service()

    @property
    def products(self) -> ProductService:
        if self._products is not None:
            return self._products
        return get_product_service()

    def list_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        for row in self.auth_store.list_users():
            customer_id = str(row.get("customer_id") or "")
            snapshot = self._account_snapshot(customer_id)
            users.append({
                **row,
                "account": snapshot,
            })
        return users

    def get_user_portfolio(self, customer_id: str) -> dict[str, Any]:
        user = self.auth_store.get_user_by_customer_id(customer_id)
        if user is None:
            return {"exists": False, "user": None, "account": None, "positions": []}
        snapshot = self._account_snapshot(customer_id)
        positions = self.portfolio.list_positions(customer_id)
        return {
            "exists": True,
            "user": user,
            "account": snapshot,
            "positions": positions,
        }

    def _account_snapshot(self, customer_id: str) -> AccountSnapshot | None:
        if not customer_id:
            return None
        try:
            return self.portfolio.get_account(customer_id)
        except Exception:  # noqa: BLE001
            return None

    def list_products(self, product_type: str = "fund") -> list[ProductView]:
        return self.products.list_products(product_type, include_inactive=True)

    def set_product_active(self, code: str, active: bool) -> ProductView:
        normalized = str(code).strip()
        if not normalized:
            raise ProductError("请提供商品代码", code="product_not_found")
        if self.products.library.query_by_code(normalized) is None:
            raise ProductError(f"商品 {normalized} 不存在", code="product_not_found")
        updated = self.products.library.set_product_active(normalized, active)
        if not updated:
            raise ProductError(f"商品 {normalized} 不存在", code="product_not_found")
        return self.products.get_product(normalized)

    def upsert_product(self, data: dict[str, Any]) -> ProductView:
        code = str(data.get("code", "")).strip()
        if not code or not str(data.get("name", "")).strip():
            raise ProductError("商品代码与名称不能为空", code="product_not_found")
        if not self.products.library.upsert_product(data):
            raise ProductError("商品保存失败：代码与名称不能为空", code="product_not_found")
        self._write_performance(code, data)
        return self.products.get_product(code)

    def _write_performance(self, code: str, data: dict[str, Any]) -> None:
        performance = {field: data.get(field) for field in _PERFORMANCE_FIELDS}
        nav_date = str(data.get("nav_date") or "").strip()
        if all(value is None for value in performance.values()) and not nav_date:
            return
        nav = performance.get("nav")
        if nav is not None and float(nav) <= 0:
            raise BusinessError("最新净值必须大于 0")

        current = (self.products.library.query_by_code(code) or {}).get("performance") or {}
        merged = {
            field: (performance[field] if performance[field] is not None else current.get(field))
            for field in _PERFORMANCE_FIELDS
        }
        merged["update_date"] = nav_date or str(current.get("as_of") or "")
        if not self.products.library.upsert_performance(code, merged):
            raise ProductError(f"商品 {code} 的业绩数据保存失败", code="product_not_found")


_admin_service: AdminService | None = None


def get_admin_service() -> AdminService:
    global _admin_service
    if _admin_service is None:
        _admin_service = AdminService()
    return _admin_service


__all__ = ["AdminService", "get_admin_service"]
