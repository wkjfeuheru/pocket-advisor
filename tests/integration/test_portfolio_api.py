"""模拟交易 REST 接口测试：鉴权、越权隔离、清仓与账户口径。

服务层用内存假实现注入（``portfolio.router.get_service`` / ``product.router.get_service``），
因此不需要 PostgreSQL；鉴权仍走真实 token 校验路径（``get_user_store`` 被替换）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.portfolio_service import PortfolioDeps, PortfolioService
from app.services.product_service import ProductDeps, ProductService

from tests.integration.test_portfolio_service import (  # noqa: E402 - 复用服务层的内存假件
    PRODUCTS,
    _FakeLibrary,
    _FakeNavSource,
    _FakeStore,
)

TOKEN_BY_CUSTOMER = {
    "TOKEN-A": "CUST000001",
    "TOKEN-B": "CUST000002",
    "TOKEN-ADMIN": "CUSTADMIN",
}


class _FakeUserStore:
    """只实现 token 校验与角色；路由不触碰其它认证能力。"""

    def verify_token(self, token: str):
        return TOKEN_BY_CUSTOMER.get(token)

    def is_admin(self, customer_id):
        return str(customer_id).upper() == "CUSTADMIN"


@pytest.fixture()
def client(monkeypatch):
    from app.routers import admin as admin_routes
    from app.routers import portfolio as portfolio_routes
    from app.routers import product as product_routes

    store = _FakeStore()
    library = _FakeLibrary(PRODUCTS)
    nav_source = _FakeNavSource({"110011": 3.85, "003003": 1.0})
    service = PortfolioService(PortfolioDeps(
        store=store,
        library=library,
        nav_source=nav_source,
    ))
    products = ProductService(ProductDeps(library=library, nav_source=nav_source))
    monkeypatch.setattr(portfolio_routes, "get_service", lambda: service)
    monkeypatch.setattr(product_routes, "get_service", lambda: products)
    monkeypatch.setattr(
        "app.core.dependencies.get_user_store", lambda: _FakeUserStore(),
    )

    app = FastAPI()
    app.include_router(product_routes.router)
    app.include_router(portfolio_routes.router)
    app.include_router(admin_routes.router)
    client = TestClient(app)
    client.service = service
    client.store = store
    return client


def auth(token: str = "TOKEN-A") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 鉴权 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/portfolio/products", None),
    ("get", "/api/portfolio/products/110011", None),
    ("get", "/api/portfolio/account", None),
    ("get", "/api/portfolio/positions", None),
    ("get", "/api/portfolio/orders", None),
    ("get", "/api/portfolio/transactions", None),
    ("post", "/api/portfolio/deposit", {"amount": 1000}),
    ("post", "/api/portfolio/orders", {"product_code": "110011", "side": "buy", "amount": 1000}),
    ("post", "/api/portfolio/liquidate", {}),
])
def test_all_endpoints_require_a_token(client, method, path, body):
    """没有任何模拟交易接口可以匿名访问。"""
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 401


def test_invalid_token_is_rejected(client):
    response = client.get("/api/portfolio/account", headers=auth("BAD"))
    assert response.status_code == 401


# ── 商品展示 ──────────────────────────────────────────────────────

def test_products_shelf_returns_nav_and_fees(client):
    response = client.get("/api/portfolio/products", headers=auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["simulated"] is True
    shelf = {item["code"]: item for item in payload["products"]}
    assert shelf["110011"]["nav"]["value"] == 3.85
    assert shelf["110011"]["tradable"] is True
    assert shelf["999999"]["tradable"] is False


def test_unknown_product_returns_404(client):
    response = client.get("/api/portfolio/products/000000", headers=auth())
    assert response.status_code == 404


# ── 账户与充值 ────────────────────────────────────────────────────

def test_account_starts_empty(client):
    response = client.get("/api/portfolio/account", headers=auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["cash_balance"] == 0.0
    assert payload["total_deposit"] == 0.0
    assert payload["simulated"] is True
    assert payload["disclaimer"]


def test_deposit_then_buy_then_positions(client):
    resp = client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    assert resp.status_code == 200
    assert resp.json()["account"]["cash_balance"] == 100000.0

    order = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 38500,
    }, headers=auth())
    assert order.status_code == 200
    assert order.json()["order"]["side"] == "buy"

    positions = client.get("/api/portfolio/positions", headers=auth())
    assert positions.status_code == 200
    body = positions.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["product_code"] == "110011"
    assert body["positions"][0]["market_value"] is not None
    assert body["account"]["position_count"] == 1


def test_deposit_rejects_non_positive_amount(client):
    assert client.post("/api/portfolio/deposit", json={"amount": 0}, headers=auth()).status_code == 422
    assert client.post("/api/portfolio/deposit", json={"amount": -5}, headers=auth()).status_code == 422


def test_deposit_over_cap_returns_400(client):
    response = client.post(
        "/api/portfolio/deposit", json={"amount": 99_999_999}, headers=auth()
    )
    assert response.status_code == 400


def test_deposit_is_idempotent_by_key(client):
    body = {"amount": 5000, "idempotency_key": "k-1"}
    first = client.post("/api/portfolio/deposit", json=body, headers=auth())
    second = client.post("/api/portfolio/deposit", json=body, headers=auth())
    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["account"]["cash_balance"] == 5000.0


# ── 下单错误映射 ──────────────────────────────────────────────────

def test_buy_without_funds_returns_409(client):
    response = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth())
    assert response.status_code == 409


def test_buy_product_without_nav_returns_409(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    response = client.post("/api/portfolio/orders", json={
        "product_code": "999999", "side": "buy", "amount": 10000,
    }, headers=auth())
    assert response.status_code == 409


def test_sell_without_position_returns_409(client):
    response = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "sell", "all": True,
    }, headers=auth())
    assert response.status_code == 409


def test_buy_requires_amount_or_shares(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    response = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy",
    }, headers=auth())
    assert response.status_code == 400


def test_sell_with_amount_is_rejected(client):
    """卖出用金额是常见误用，必须显式拒绝而不是猜意图。"""
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth())
    response = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "sell", "amount": 5000,
    }, headers=auth())
    assert response.status_code == 400


# ── 清仓 ──────────────────────────────────────────────────────────

def test_liquidate_clears_all_positions(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth())
    client.post("/api/portfolio/orders", json={
        "product_code": "003003", "side": "buy", "amount": 10000,
    }, headers=auth())

    response = client.post("/api/portfolio/liquidate", json={}, headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["cleared_codes"]) == ["003003", "110011"]
    assert body["account"]["position_count"] == 0
    assert body["account"]["market_value"] == 0.0

    assert client.get("/api/portfolio/positions", headers=auth()).json()["positions"] == []


def test_liquidate_with_no_positions_is_a_noop(client):
    response = client.post("/api/portfolio/liquidate", json={}, headers=auth())
    assert response.status_code == 200
    assert response.json()["cleared_codes"] == []


# ── 客户隔离 ──────────────────────────────────────────────────────

def test_customers_cannot_see_each_others_accounts(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth("TOKEN-A"))
    client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth("TOKEN-A"))

    other = client.get("/api/portfolio/account", headers=auth("TOKEN-B")).json()
    assert other["cash_balance"] == 0.0
    assert other["position_count"] == 0
    assert client.get("/api/portfolio/positions", headers=auth("TOKEN-B")).json()["positions"] == []


def test_account_reports_customer_from_token_not_request(client):
    """账户归属只来自 token，客户端无法通过参数读取他人账户。"""
    client.post("/api/portfolio/deposit", json={"amount": 100}, headers=auth("TOKEN-B"))
    payload = client.get("/api/portfolio/account", headers=auth("TOKEN-B")).json()
    assert payload["customer_id"] == "CUST000002"


# ── 账户口径不完整时显式标注 ──────────────────────────────────────

def test_account_flags_incomplete_market_value(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    client.store.positions[("CUST000001", "999999")] = {
        "customer_id": "CUST000001", "product_code": "999999", "shares": 100.0,
        "cost_amount": 100.0, "avg_cost": 1.0,
        "opened_at": datetime.now(), "updated_at": datetime.now(),
    }
    payload = client.get("/api/portfolio/account", headers=auth()).json()
    assert payload["market_value_complete"] is False
    assert "999999:unavailable" in payload["pricing_issues"]


# ── 记录 ──────────────────────────────────────────────────────────

def test_orders_and_transactions_endpoints(client):
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth())

    orders = client.get("/api/portfolio/orders", headers=auth()).json()
    assert len(orders["orders"]) == 1
    assert orders["orders"][0]["product_name"] == "易方达中小盘混合"
    assert orders["orders"][0]["fee"] > 0

    txns = client.get("/api/portfolio/transactions", headers=auth()).json()
    kinds = {item["kind"] for item in txns["transactions"]}
    assert {"deposit", "buy"} <= kinds


def test_buy_with_undisclosed_fee_surfaces_limitations(client, monkeypatch):
    """费率未披露时接口照常成交，但订单里能看到限制项。"""
    monkeypatch.setitem(PRODUCTS, "110011", {
        "name": "易方达中小盘混合", "nav": 3.85, "subscription_fee": None,
        "redemption_fee": "", "risk_level": "R3 中风险",
    })
    client.post("/api/portfolio/deposit", json={"amount": 100000}, headers=auth())
    response = client.post("/api/portfolio/orders", json={
        "product_code": "110011", "side": "buy", "amount": 10000,
    }, headers=auth())
    assert response.status_code == 200
    assert "fee_unavailable:subscription_fee" in response.json()["order"]["fee_limitations"]


# ── 角色互斥 ──────────────────────────────────────────────────────

def test_admin_token_cannot_call_user_portfolio_api(client):
    """管理员 token 不得调用用户交易接口。"""
    response = client.get("/api/portfolio/account", headers=auth("TOKEN-ADMIN"))
    assert response.status_code == 403
    assert "管理员" in response.json()["detail"]


def test_user_token_cannot_call_admin_api(client):
    """普通用户 token 不得调用管理接口。"""
    response = client.get("/api/admin/users", headers=auth())
    assert response.status_code == 403
    assert "管理员" in response.json()["detail"]
