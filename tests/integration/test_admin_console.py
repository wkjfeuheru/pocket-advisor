"""管理后台测试：角色解析、上下架语义与用户总览授权。

用内存假存储与假库替代 PostgreSQL（沿用仓库既有"手写 fake + monkeypatch"惯例），
因此不依赖真实数据库即可覆盖：非管理员被拒、下架只挡买入不挡赎回、
下架商品不再出现在管理端列表之外的用户货架上。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from app.services.admin_service import AdminService
from app.routers import admin as admin_routes
from app.services.portfolio_service import PortfolioDeps, PortfolioError, PortfolioService
from app.services.product_service import NavQuote, ProductDeps, ProductError, ProductLibraryNavSource, ProductService
from app.core import dependencies as r
from app.core.dependencies import _is_admin
from app.utils.errors import BusinessError


# ── 假存储与假库 ──────────────────────────────────────────────────

class _Request:
    def __init__(self, token: str = "admin-token"):
        self.headers = {"Authorization": f"Bearer {token}"}


class _FakeAuthStore:
    """内存用户表：只需覆盖管理后台用到的读与写。"""

    def __init__(self, users=None):
        self._users = users or {
            "CUST000001": {"customer_id": "CUST000001", "username": "jason",
                           "display_name": "jason", "created_at": "2026-09-04T20:00:31",
                           "is_admin": False},
            "CUST000002": {"customer_id": "CUST000002", "username": "admin",
                           "display_name": "admin", "created_at": "2026-09-17T10:00:00",
                           "is_admin": True},
        }

    def list_users(self):
        return [dict(row) for row in self._users.values()]

    def get_user_by_customer_id(self, customer_id):
        row = self._users.get(str(customer_id).upper())
        return dict(row) if row else None

    def is_admin(self, customer_id):
        row = self._users.get(str(customer_id).upper())
        return bool(row and row.get("is_admin"))

    def set_admin(self, customer_id, is_admin):
        row = self._users.get(str(customer_id).upper())
        if row is None:
            return False
        row["is_admin"] = bool(is_admin)
        return True


class _FakeLibrary:
    """内存产品库：上下架状态真正改变，供断言读取。

    也实现 ``upsert_product`` / ``upsert_performance``，让"发行商品 + 写入净值"
    这条管理链路可以在不接数据库的情况下端到端验证。
    """

    def __init__(self, products):
        self._products = {code: dict(data) for code, data in products.items()}

    def _shape(self, code):
        raw = self._products.get(str(code).strip())
        if raw is None:
            return None
        return {
            "basic_info": {
                "code": str(code).strip(), "name": raw["name"],
                "type": raw.get("type", "fund"), "is_active": raw.get("is_active", True),
                "risk_level": raw.get("risk_level", ""), "company": raw.get("company", ""),
                "manager": raw.get("manager", ""),
                "scale": raw.get("scale"), "recommended_holding_period": "", "investment_target": "",
            },
            "performance": {"nav": raw.get("nav"), "as_of": raw.get("as_of", ""),
                            "return_1y": raw.get("return_1y"),
                            "max_drawdown": raw.get("max_drawdown"),
                            "sharpe_ratio": raw.get("sharpe_ratio")},
            "fee": {"subscription_fee": raw.get("subscription_fee"),
                    "redemption_fee": raw.get("redemption_fee", "")},
        }

    def query_by_code(self, code):
        return self._shape(str(code).strip())

    def query_by_codes(self, codes):
        return [item for item in (self._shape(c) for c in codes) if item]

    def list_products(self, product_type="fund", include_inactive=False):
        kind = (product_type or "fund").strip()
        return [
            {"code": code, "name": data["name"]}
            for code, data in self._products.items()
            if (include_inactive or data.get("is_active", True))
            and (kind.lower() == "all" or data.get("type", "fund") == kind)
        ]

    def set_product_active(self, code, active):
        raw = self._products.get(str(code).strip())
        if raw is None:
            return False
        raw["is_active"] = bool(active)
        return True

    def upsert_product(self, data):
        code = str(data.get("code", "")).strip()
        if not code or not str(data.get("name", "")).strip():
            return False
        raw = self._products.setdefault(code, {})
        # 与真实存储一致：只覆盖传入的非 None 列，未传的保持原值。
        for field in ("name", "type", "risk_level", "company", "manager", "scale",
                      "subscription_fee", "redemption_fee"):
            if data.get(field) is not None:
                raw[field] = data[field]
        return True

    def upsert_performance(self, code, perf):
        raw = self._products.get(str(code).strip())
        if raw is None:
            return False
        raw["nav"] = perf.get("nav")
        raw["as_of"] = perf.get("update_date", "")
        for field in ("return_1y", "max_drawdown", "sharpe_ratio"):
            raw[field] = perf.get(field)
        return True


class _FakeNavSource:
    def __init__(self, navs):
        self._navs = navs

    def latest_nav(self, product_code):
        nav = self._navs.get(str(product_code).strip())
        return NavQuote(product_code=str(product_code), nav=nav, as_of="2026-09-08") if nav else None


class _FakePortfolioStore:
    """最小账户/持仓存储：有资金、无持仓。

    申购在走到商品校验前会先 ``ensure_account``，因此这里必须有账户行；
    持仓为空则让赎回止步于 ``PortfolioError(code=no_position)``，正好用来验证"下架不挡赎回"。
    """

    def ensure_account(self, customer_id):
        return {"customer_id": str(customer_id).upper(), "cash_balance": 10000.0,
                "frozen_balance": 0.0, "total_deposit": 10000.0, "updated_at": None}

    def list_positions(self, customer_id):
        return []

    def realized_pnl_total(self, customer_id):
        return 0.0

    @contextmanager
    def transaction(self):
        yield self

    def lock_account(self, session, customer_id):
        return self.ensure_account(customer_id)

    def get_position_row(self, session, customer_id, product_code):
        return None


PRODUCTS = {
    "110011": {"name": "易方达中小盘混合", "nav": 3.85, "subscription_fee": 1.5},
    "003003": {"name": "华夏现金增利货币A", "nav": 1.0, "subscription_fee": 0.0},
}


def _portfolio(library=None):
    return PortfolioService(PortfolioDeps(
        store=_FakePortfolioStore(),
        library=library or _FakeLibrary(PRODUCTS),
        nav_source=_FakeNavSource({"110011": 3.85, "003003": 1.0}),
    ))


def _products(library=None, nav_source=None):
    lib = library or _FakeLibrary(PRODUCTS)
    return ProductService(ProductDeps(
        library=lib,
        nav_source=nav_source or _FakeNavSource({"110011": 3.85, "003003": 1.0}),
    ))


def _admin(library=None, *, on_library=False):
    lib = library or _FakeLibrary(PRODUCTS)
    nav = ProductLibraryNavSource(lib) if on_library else _FakeNavSource(
        {"110011": 3.85, "003003": 1.0},
    )
    return AdminService(
        auth_store=_FakeAuthStore(),
        portfolio=_portfolio(lib) if not on_library else PortfolioService(PortfolioDeps(
            store=_FakePortfolioStore(),
            library=lib,
            nav_source=nav,
        )),
        products=_products(lib, nav),
    )


# ── 角色解析 ──────────────────────────────────────────────────────

@pytest.fixture()
def roles(monkeypatch):
    """装一个库内有角色的 store，并清空白名单。"""
    from app.core import dependencies as deps

    store = _FakeAuthStore()
    monkeypatch.setattr(r, "get_user_store", lambda: store)
    monkeypatch.setattr(r, "ADMIN_CUSTOMER_IDS", set())
    monkeypatch.setattr(deps, "get_user_store", lambda: store)
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())
    return store


def test_db_role_grants_admin_without_whitelist(roles):
    """库内 is_admin=true 即可成为管理员，无需环境变量白名单。"""
    assert _is_admin("CUST000002") is True
    assert _is_admin("cust000002") is True
    assert _is_admin("CUST000001") is False


def test_whitelist_still_grants_admin(monkeypatch):
    """白名单与库内角色取并集：改配置即可授权的既有运维方式继续可用。"""
    from app.core import dependencies as deps

    monkeypatch.setattr(r, "get_user_store", lambda: _FakeAuthStore())
    monkeypatch.setattr(r, "ADMIN_CUSTOMER_IDS", {"CUST000001"})
    monkeypatch.setattr(deps, "get_user_store", lambda: _FakeAuthStore())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", {"CUST000001"})

    assert _is_admin("CUST000001") is True


def test_role_lookup_failure_fails_closed(monkeypatch):
    """角色查询异常时必须拒绝分类，不能把管理员降级成普通用户。"""
    from app.core import dependencies as deps

    class _Broken:
        def is_admin(self, customer_id):
            raise RuntimeError("db down")

    monkeypatch.setattr(r, "get_user_store", lambda: _Broken())
    monkeypatch.setattr(r, "ADMIN_CUSTOMER_IDS", set())
    monkeypatch.setattr(deps, "get_user_store", lambda: _Broken())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())

    with pytest.raises(RuntimeError, match="db down"):
        _is_admin("CUST000002")


def test_demoted_user_loses_admin(roles):
    """把用户降级后，其管理员权限立即消失（不依赖重启或缓存）。"""
    roles.set_admin("CUST000002", False)

    assert _is_admin("CUST000002") is False


# ── 授权门禁 ──────────────────────────────────────────────────────

def test_non_admin_cannot_list_users(monkeypatch):
    """普通用户调用管理接口被 403 拦下。"""
    from app.core import dependencies as deps

    monkeypatch.setattr(r, "get_user_store", lambda: _FakeAuthStore())
    monkeypatch.setattr(r, "ADMIN_CUSTOMER_IDS", set())
    monkeypatch.setattr(r, "resolve_customer_id", lambda request: "CUST000001")
    monkeypatch.setattr(deps, "get_user_store", lambda: _FakeAuthStore())
    monkeypatch.setattr(deps, "ADMIN_CUSTOMER_IDS", set())
    monkeypatch.setattr(deps, "resolve_customer_id", lambda request: "CUST000001")

    with pytest.raises(HTTPException) as exc:
        admin_routes.list_users(_Request())
    assert exc.value.status_code == 403


def test_admin_can_list_users_through_route(monkeypatch):
    """管理员经路由拿到用户总览。"""
    from app.core import dependencies as deps

    store = _FakeAuthStore()
    monkeypatch.setattr(r, "get_user_store", lambda: store)
    monkeypatch.setattr(r, "resolve_customer_id", lambda request: "CUST000002")
    monkeypatch.setattr(deps, "get_user_store", lambda: store)
    monkeypatch.setattr(deps, "resolve_customer_id", lambda request: "CUST000002")
    monkeypatch.setattr(admin_routes, "get_admin_service",
                        lambda: AdminService(auth_store=store, portfolio=_portfolio()))

    response = admin_routes.list_users(_Request())

    assert {item.customer_id for item in response.users} == {"CUST000001", "CUST000002"}
    assert response.users[1].is_admin is True


# ── 用户总览 ──────────────────────────────────────────────────────

def test_admin_lists_users_with_account_snapshot():
    """用户列表带账户口径，且与用户端同源（都由 PortfolioService 计算）。"""
    service = AdminService(auth_store=_FakeAuthStore(), portfolio=_portfolio())

    users = {row["customer_id"]: row for row in service.list_users()}

    assert set(users) == {"CUST000001", "CUST000002"}
    assert users["CUST000002"]["is_admin"] is True
    snapshot = users["CUST000001"]["account"]
    assert snapshot is not None
    assert snapshot.customer_id == "CUST000001"


def test_user_portfolio_reports_missing_user():
    """不存在的用户返回 exists=False，而不是伪造一个空账户。"""
    service = AdminService(auth_store=_FakeAuthStore(), portfolio=_portfolio())

    payload = service.get_user_portfolio("CUST999999")

    assert payload["exists"] is False
    assert payload["account"] is None


# ── 上下架 ────────────────────────────────────────────────────────

def test_offline_removes_product_from_user_shelf_but_keeps_admin_view():
    library = _FakeLibrary(PRODUCTS)
    service = _admin(library)

    service.set_product_active("110011", False)

    user_codes = [item.code for item in service.products.list_products()]
    admin_codes = [item.code for item in service.list_products()]
    assert "110011" not in user_codes
    assert "110011" in admin_codes


def test_offline_blocks_buy_but_allows_sell():
    """下架只挡买入：既有持仓必须还能赎回，否则用户被困。"""
    library = _FakeLibrary(PRODUCTS)
    library.set_product_active("110011", False)
    service = _portfolio(library)

    with pytest.raises(PortfolioError) as exc:
        service.buy("CUST000001", "110011", amount=1000.0)
    assert exc.value.code == "product_offline"

    # 赎回走的是商品存在性 + 持仓校验，不应再撞上下架拦截。
    with pytest.raises(PortfolioError) as exc:
        service.sell("CUST000001", "110011", shares=10.0)
    assert exc.value.code == "no_position"


def test_reactivating_restores_tradability():
    library = _FakeLibrary(PRODUCTS)
    service = _admin(library)

    service.set_product_active("110011", False)
    product = service.set_product_active("110011", True)

    assert product.is_active is True
    assert product.tradable is True
    assert "product_offline" not in product.limitations


def test_offline_unknown_product_raises_not_found():
    service = _admin()

    with pytest.raises(ProductError) as exc:
        service.set_product_active("000000", False)
    assert exc.value.code == "product_not_found"


def test_active_product_view_exposes_is_active():
    products = _products()

    shelf = {item.code: item for item in products.list_products()}
    assert shelf["110011"].is_active is True
    assert shelf["110011"].tradable is True


# ── 商品发行/编辑时的业绩数据 ─────────────────────────────────────

def test_new_product_without_performance_is_not_tradable():
    """只填商品静态字段的新品：没有净值，照常上架但不可申购。"""
    library = _FakeLibrary({})
    service = _admin(library, on_library=True)

    product = service.upsert_product({"code": "012345", "name": "科技创新混合C", "risk_level": "R4 中高风险"})

    assert product.code == "012345"
    assert product.nav.value is None
    assert product.return_1y is None
    assert product.tradable is False
    assert "pricing_unavailable" in product.limitations


def test_issue_product_with_nav_makes_it_displayable_and_tradable():
    """提交净值后，货架立即有净值和近一年收益，且变为可申购。

    这是这次改动的核心：管理端写入的业绩要落到 ``product_performance``，
    取价链路才能读到，否则新发的商品永远显示"—"且买不了。
    """
    library = _FakeLibrary({})
    service = _admin(library, on_library=True)

    product = service.upsert_product({
        "code": "012345", "name": "科技创新混合C", "risk_level": "R4 中高风险",
        "nav": 1.2345, "nav_date": "2026-09-08",
        "return_1y": 0.126, "max_drawdown": 0.187, "sharpe_ratio": 0.78,
    })

    assert product.nav.value == pytest.approx(1.2345)
    assert product.nav.as_of == "2026-09-08"
    assert product.return_1y == pytest.approx(0.126)
    assert product.max_drawdown == pytest.approx(0.187)
    assert product.tradable is True

    # 用户侧货架同样可见，不只是管理端回读。
    shelf = {item.code: item for item in service.products.list_products()}
    assert shelf["012345"].nav.value == pytest.approx(1.2345)


def test_editing_product_without_performance_does_not_write_empty_nav():
    """编辑商品名时不传业绩字段：不得写进一条空净值把商品"假装"标成有数据。"""
    library = _FakeLibrary({})
    service = _admin(library, on_library=True)

    service.upsert_product({"code": "012345", "name": "科技创新混合C", "nav": 1.2})
    service.upsert_product({"code": "012345", "name": "科技创新混合C（改名）"})

    product = service.products.get_product("012345")
    assert product.name == "科技创新混合C（改名）"
    # 上一次写入的净值保留（业绩表按追加行取最新一条），没有被空值覆盖。
    assert product.nav.value == pytest.approx(1.2)
    assert product.tradable is True


def test_editing_only_return_keeps_existing_nav():
    """只改近一年收益、不重填净值：必须沿用上一条净值。

    业绩表只追加不更新且按最新行取净值，若新行写成 ``nav=NULL``，一个本来可
    申购的商品会静默变成"缺净值"。
    """
    library = _FakeLibrary({})
    service = _admin(library, on_library=True)

    service.upsert_product({"code": "012345", "name": "科技创新混合C", "nav": 1.2, "nav_date": "2026-09-01"})
    product = service.upsert_product({"code": "012345", "name": "科技创新混合C", "return_1y": 0.2})

    assert product.nav.value == pytest.approx(1.2)
    assert product.return_1y == pytest.approx(0.2)
    assert product.tradable is True


def test_non_positive_nav_is_rejected():
    """非正净值存进去会被取价逻辑判为不可用，因此必须当场拒绝而非静默落库。"""
    library = _FakeLibrary({})
    service = _admin(library, on_library=True)

    with pytest.raises(BusinessError, match="最新净值必须大于 0"):
        service.upsert_product({"code": "012345", "name": "科技创新混合C", "nav": 0.0})


def test_upsert_request_rejects_non_positive_nav_at_schema_level():
    """净值必须为正：在请求模型这一层就挡住，不依赖业务层兜底。"""
    from pydantic import ValidationError

    from app.schemas.admin import AdminProductUpsertRequest

    with pytest.raises(ValidationError):
        AdminProductUpsertRequest(code="012345", name="科技创新混合C", nav=-1.0)

    payload = AdminProductUpsertRequest(
        code="012345", name="科技创新混合C", nav=1.2345, return_1y=0.126, nav_date="2026-09-08",
    )
    assert payload.nav == pytest.approx(1.2345)
    assert payload.return_1y == pytest.approx(0.126)
    assert payload.nav_date == "2026-09-08"
