"""用户只读货架：组装 ProductView，不含发行/上下架。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.schemas.product import ProductView, SourcedValue
from app.utils.errors import BusinessError

logger = logging.getLogger(__name__)


class ProductError(BusinessError):
    """商品业务失败。用 ``code`` 区分原因，不再为每种失败单独建子类。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def to_rate(value: Any) -> float | None:
    """把百分比编码统一折成小数费率；未披露返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 100.0
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
        if not text:
            return None
    try:
        rate = float(text)
    except ValueError:
        return None
    if not math.isfinite(rate):
        return None
    return rate / 100.0


@dataclass(frozen=True)
class NavQuote:
    """一个可用于成交的净值。"""

    product_code: str
    nav: float
    as_of: str = ""
    source: str = "product_performance"


@runtime_checkable
class NavSource(Protocol):
    """按商品代码提供最新净值。"""

    def latest_nav(self, product_code: str) -> NavQuote | None: ...


def require_nav(source: NavSource, product_code: str, product_name: str = "") -> NavQuote:
    """取净值，缺失时抛 ``ProductError(code=pricing_unavailable)``。"""
    quote = source.latest_nav(product_code)
    if quote is None:
        label = f"{product_name}（{product_code}）" if product_name else product_code
        raise ProductError(f"{label} 没有可用的最新净值，无法成交", code="pricing_unavailable")
    return quote


def product_fee_rates(product: dict[str, Any]) -> tuple[float | None, float | None]:
    """从产品记录中解析 (申购费率, 赎回费率) 小数形式；未披露为 ``None``。"""
    basic = product.get("basic_info") or {}
    fee = product.get("fee") or {}
    subscription = fee.get("subscription_fee", basic.get("subscription_fee"))
    redemption = fee.get("redemption_fee", basic.get("redemption_fee"))
    return to_rate(subscription), to_rate(redemption)


@dataclass
class ProductDeps:
    """由组合根注入的商品库与净值来源。"""

    library: Any = None
    nav_source: NavSource | None = None

    def build_library(self) -> Any:
        if self.library is not None:
            return self.library
        raise RuntimeError("ProductDeps.library must be provided by the composition root")

    def build_nav_source(self) -> NavSource:
        if self.nav_source is not None:
            return self.nav_source
        raise RuntimeError("ProductDeps.nav_source must be provided by the composition root")


class ProductLibraryNavSource:
    """将商品库最新净值适配为领域 ``NavSource``。"""

    def __init__(self, library: Any) -> None:
        self._library = library

    def latest_nav(self, product_code: str) -> NavQuote | None:
        product = self._library.query_by_code(product_code)
        if product is None:
            return None
        performance = product.get("performance") or {}
        raw_nav = performance.get("nav")
        if raw_nav is None:
            return None
        try:
            nav = float(raw_nav)
        except (TypeError, ValueError):
            return None
        if nav <= 0:
            return None
        return NavQuote(
            product_code=str(product_code),
            nav=nav,
            as_of=str(performance.get("as_of") or ""),
            source=str(performance.get("source") or "product_performance"),
        )


class ProductService:
    """用户侧货架只读服务。"""

    def __init__(self, deps: ProductDeps | None = None) -> None:
        self._deps = deps or ProductDeps()

    @property
    def library(self) -> Any:
        return self._deps.build_library()

    @property
    def nav_source(self) -> NavSource:
        return self._deps.build_nav_source()

    def get_product(self, product_code: str) -> ProductView:
        """单个商品的货架视图。"""
        code = str(product_code).strip()
        product = self._product_or_raise(code)
        return self._to_product_view(code, product, self.nav_source.latest_nav(code))

    def list_products(
        self, product_type: str = "fund", *, include_inactive: bool = False,
    ) -> list[ProductView]:
        """商品货架。取不到净值的商品照常展示，但 ``tradable=False``。"""
        summaries = (
            self.library.list_products(product_type, include_inactive=True)
            if include_inactive else self.library.list_products(product_type)
        )
        codes = [str(item.get("code")) for item in summaries]
        views: list[ProductView] = []
        for code in codes:
            try:
                product = self.library.query_by_code(code)
            except Exception:  # noqa: BLE001 - 单品读取失败不拖垮整个货架
                logger.warning("product_shelf_item_failed code=%s", code)
                continue
            if product is None:
                continue
            views.append(self._to_product_view(code, product, self.nav_source.latest_nav(code)))
        return views

    def _product_or_raise(self, product_code: str) -> dict[str, Any]:
        code = str(product_code).strip()
        if not code:
            raise ProductError("请提供商品代码", code="product_not_found")
        product = self.library.query_by_code(code)
        if product is None:
            raise ProductError(f"商品 {code} 不存在", code="product_not_found")
        return product

    def _to_product_view(self, code: str, product: dict[str, Any], nav_quote: Any) -> ProductView:
        basic = product.get("basic_info") or {}
        fee = product.get("fee") or {}
        performance = product.get("performance") or {}
        subscription_rate, _redemption_rate = product_fee_rates(product)
        limitations: list[str] = []
        if nav_quote is None:
            limitations.append("pricing_unavailable")
        if subscription_rate is None:
            limitations.append("fee_unavailable:subscription_fee")
        is_active = bool(basic.get("is_active", True))
        if not is_active:
            limitations.append("product_offline")
        return ProductView(
            code=code,
            name=str(basic.get("name") or ""),
            type=str(basic.get("type") or "fund"),
            risk_level=str(basic.get("risk_level") or ""),
            company=str(basic.get("company") or ""),
            manager=str(basic.get("manager") or ""),
            scale=basic.get("scale"),
            nav=SourcedValue(
                value=nav_quote.nav if nav_quote else None,
                source=nav_quote.source if nav_quote else "unavailable",
                as_of=nav_quote.as_of if nav_quote else "",
            ),
            return_1y=performance.get("return_1y"),
            max_drawdown=performance.get("max_drawdown"),
            sharpe_ratio=performance.get("sharpe_ratio"),
            subscription_fee=(
                round(subscription_rate * 100.0, 4) if subscription_rate is not None else None
            ),
            redemption_fee=str(fee.get("redemption_fee") or basic.get("redemption_fee") or ""),
            recommended_holding_period=str(basic.get("recommended_holding_period") or ""),
            investment_target=str(basic.get("investment_target") or ""),
            is_active=is_active,
            tradable=nav_quote is not None and is_active,
            limitations=limitations,
        )


_service: ProductService | None = None


def get_product_service() -> ProductService:
    """返回注入 PostgreSQL 产品库的单例货架服务。"""
    global _service
    if _service is None:
        from app.crud.product_crud import get_product_library

        library = get_product_library()
        _service = ProductService(ProductDeps(
            library=library,
            nav_source=ProductLibraryNavSource(library),
        ))
    return _service


__all__ = [
    "NavQuote",
    "NavSource",
    "ProductDeps",
    "ProductError",
    "ProductLibraryNavSource",
    "ProductService",
    "get_product_service",
    "product_fee_rates",
    "require_nav",
    "to_rate",
]
