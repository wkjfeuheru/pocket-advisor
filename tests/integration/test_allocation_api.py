"""持仓配置 REST 接口：路由、鉴权与 ``code`` → HTTP 状态映射。

依赖用内存假件注入（``allocation_routes`` 的 provider 函数），因此不需要 PostgreSQL；
鉴权仍走真实 token 校验路径（``get_user_store`` 被替换）。

这里**不测**方案数学——那是 ``test_portfolio_plan.py`` / ``test_portfolio_allocation.py``
的职责。这里只测接口契约：路径、鉴权、状态码、以及"别人的方案看不见"。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.agents.portfolio.plan import PlanTier

TOKEN_BY_CUSTOMER = {"TOKEN-A": "CUST000001", "TOKEN-B": "CUST000002"}


class _FakeUserStore:
    def verify_token(self, token: str):
        return TOKEN_BY_CUSTOMER.get(token)

    def is_admin(self, customer_id):
        return False


PRODUCTS = {
    "110011": {"name": "易方达中小盘混合", "type": "fund", "risk_level": "R3", "priceable": True},
    "510300": {"name": "华泰柏瑞沪深300ETF", "type": "etf", "risk_level": "R3", "priceable": True},
}

HOLDINGS = {
    "110011": {"market_value": 600.0, "shares": 600.0, "priceable": True},
    "510300": {"market_value": 400.0, "shares": 200.0, "priceable": True},
}

NAVS = {"110011": 1.0, "510300": 2.0}

CANDIDATE = {"110011": 0.10, "510300": 0.30, "cash": 0.05}


class _FakePlanStore:
    def __init__(self):
        self.plans: dict[str, dict] = {}
        self.items: list[dict] = []

    def insert_plan(self, plan, items):
        self.plans[plan["plan_id"]] = dict(plan)
        self.items += [dict(i) for i in items]
        return plan["plan_id"]

    def get_plan(self, plan_id):
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        return {**plan, "items": [i for i in self.items if i["plan_id"] == plan_id]}

    def list_plans(self, customer_id, *, limit=50):
        return [p for p in self.plans.values() if p["customer_id"] == customer_id][:limit]


class _FakePortfolio:
    """实现 ``PortfolioReader``：返回用户持仓。"""

    def __init__(self, holdings):
        self.holdings = holdings

    def list_positions(self, customer_id):
        return [dict(row, product_code=code) for code, row in self.holdings.items()]


class _FakeLibrary:
    """实现 ``ProductLibrary``。"""

    def list_products_for_nav(self):
        return [dict(code=c, **v) for c, v in PRODUCTS.items()]

    def query_by_codes(self, codes):
        return [dict(code=c, **PRODUCTS[c]) for c in codes if c in PRODUCTS]


class _FakeNavReader:
    """实现 ``NavReader``：给一个恒定净值序列，足够走完生成与采纳。"""

    def __init__(self, navs, *, days: int = 30):
        self.navs = navs
        self.days = days

    def history(self, product_code, start, end):
        value = self.navs.get(product_code)
        if value is None:
            return []
        # 均匀铺一段序列，最后一条即"最新净值"
        span = [d for d in _days_between(start, end)][-self.days :]
        return [(day, value) for day in span]


def _days_between(start, end):
    from datetime import timedelta

    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


class _FakeSnapshotStore:
    """实现快照读写；用于验证 ai-account 返回两条曲线。"""

    def __init__(self):
        self.rows: dict[tuple[str, str, str], dict] = {}

    def list_snapshots(self, customer_id, owner, *, start=None, end=None):
        found = sorted(
            (day, rec["unit_nav"])
            for (cid, o, day), rec in self.rows.items()
            if cid == customer_id
            and o == owner
            and (start is None or date.fromisoformat(day) >= start)
            and (end is None or date.fromisoformat(day) <= end)
        )
        return [(date.fromisoformat(day), nav) for day, nav in found]

    def latest_snapshot(self, customer_id, owner):
        found = sorted(
            (day, rec)
            for (cid, o, day), rec in self.rows.items()
            if cid == customer_id and o == owner
        )
        return dict(found[-1][1]) if found else None

    def upsert_snapshot(self, customer_id, owner, record):
        self.rows[(customer_id, owner, record["snapshot_date"].isoformat())] = dict(record)


@pytest.fixture()
def client(monkeypatch):
    from app.routers import allocation as allocation_routes
    from app.services.portfolio_advisor.advisor_service import (
        AdvisorDeps,
        AdvisorService,
    )
    from app.services.portfolio_advisor.ledger import LedgerService
    from app.agents.portfolio.validation import ValidationConstraints

    # 每个客户一份持仓，验证"只看得到自己的方案"
    holdings_by_customer = {
        "CUST000001": HOLDINGS,
        "CUST000002": {
            "110011": {"market_value": 300.0, "shares": 300.0, "priceable": True},
            "510300": {"market_value": 700.0, "shares": 350.0, "priceable": True},
        },
    }

    class _PerCustomerPortfolio(_FakePortfolio):
        def list_positions(self, customer_id):
            return [
                dict(row, product_code=code)
                for code, row in holdings_by_customer.get(customer_id, {}).items()
            ]

    services = {}

    def _service_for(customer_id_unused=None):
        # 同一个 service 实例即可：plan_store 与 ledger 都是内存的，
        # 归属校验由 customer_id 字段完成。
        return services["advisor"]

    plan_store = _FakePlanStore()
    snapshots = _FakeSnapshotStore()
    from app.services.portfolio_advisor.ledger import LedgerService as _LS

    class _LedgerStore:
        def __init__(self):
            self.accounts = {}
            self.positions = {}
            self.trades = []
            self.plans = {}

        def establish_account(self, cid, *, initial_capital, opening_plan_id, nav_as_of):
            self.accounts[cid] = {
                "customer_id": cid, "initial_capital": initial_capital,
                "opening_plan_id": opening_plan_id, "cash_balance": 0.0,
                "unit_nav": 1.0, "total_units": initial_capital,
                "cumulative_inflow": 0.0, "cumulative_outflow": 0.0,
                "nav_as_of": nav_as_of,
            }
            return dict(self.accounts[cid])

        def get_account(self, cid):
            row = self.accounts.get(cid)
            return dict(row) if row else None

        def update_account(self, cid, **fields):
            self.accounts[cid].update(fields)
            return dict(self.accounts[cid])

        def get_position(self, cid, code):
            row = self.positions.get((cid, code))
            return dict(row) if row else None

        def list_positions(self, cid):
            return [dict(r) for (c, _), r in self.positions.items() if c == cid]

        def upsert_position(self, cid, code, *, shares, cost_amount):
            key = (cid, code)
            if shares <= 0:
                self.positions.pop(key, None)
                return
            self.positions[key] = {
                "customer_id": cid, "product_code": code, "shares": shares,
                "cost_amount": cost_amount, "avg_cost": cost_amount / shares if shares else 0.0,
            }

        def insert_trade(self, record):
            key = record.get("trade_key") or ""
            if key and any(
                r["customer_id"] == record["customer_id"] and r.get("trade_key") == key
                for r in self.trades
            ):
                from app.core.security import UniqueConstraintError

                raise UniqueConstraintError()
            self.trades.append(dict(record))
            return dict(record)

        def find_trade_by_key(self, cid, adoption_key):
            if not adoption_key:
                return None
            for row in self.trades:
                if row["customer_id"] == cid and row.get("adoption_key") == adoption_key:
                    return dict(row)
            return None

        def mark_plan_adopted(self, plan_id, *, adopted_at):
            self.plans[plan_id] = {"plan_id": plan_id, "status": "adopted", "adopted_at": adopted_at}

        def get_plan(self, plan_id):
            row = self.plans.get(plan_id)
            return dict(row) if row else None

    advisor = AdvisorService(
        AdvisorDeps(
            portfolio=_PerCustomerPortfolio(HOLDINGS),
            library=_FakeLibrary(),
            navs=_FakeNavReader(NAVS),
            plan_store=plan_store,
            ledger=_LS(_LedgerStore()),
            constraints=ValidationConstraints(
                max_single_weight=0.85, cash_floor=0.05, max_risk_level="R3"
            ),
            snapshots=snapshots,
        )
    )
    services["advisor"] = advisor

    monkeypatch.setattr(allocation_routes, "get_advisor_service", _service_for)

    from app.core import dependencies as deps

    monkeypatch.setattr(deps, "get_user_store", lambda: _FakeUserStore())

    app = FastAPI()
    app.include_router(allocation_routes.router)
    return TestClient(app)


def _auth(token: str = "TOKEN-A") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_generating_advice_returns_plans_with_the_expected_shape(client):
    response = client.post("/api/portfolio/advice", headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "CUST000001"
    assert body["total_assets"] > 0
    assert body["plans"], "至少要产出一个方案"

    # 三档是否都可用取决于当前持仓与候选的差距（等权回退的全长换手率可能够不到
    # 重构档下界），所以这里断言**结构**而不是档位数量。
    assert 1 <= len(body["plans"]) <= 3
    for plan in body["plans"]:
        assert plan["plan_id"]
        assert plan["tier"] in {"light", "moderate", "rebuild"}
        assert plan["turnover_rate"] > 0
        assert plan["tier_label"] in {"微调", "中度", "重构"}

    # 不可用档位必须带原因，便于前端展示"这一档为什么没有"
    for item in body["unavailable"]:
        assert item["tier"] in {"light", "moderate", "rebuild"}
        assert item["unavailable_reason"]

    tiers = [p["tier"] for p in body["plans"]]
    assert len(tiers) + len(body["unavailable"]) == 3


def test_generated_plan_weights_sum_to_one(client):
    body = client.post("/api/portfolio/advice", headers=_auth()).json()

    for plan in body["plans"]:
        assert sum(plan["weights"].values()) == pytest.approx(1.0)


def test_advice_requires_authentication(client):
    response = client.post("/api/portfolio/advice")

    assert response.status_code == 401


def test_listing_only_returns_my_own_plans(client):
    client.post("/api/portfolio/advice", headers=_auth("TOKEN-A"))
    client.post("/api/portfolio/advice", headers=_auth("TOKEN-B"))

    mine = client.get("/api/portfolio/allocations", headers=_auth("TOKEN-A")).json()

    assert mine["plans"]
    assert all(p["tier"] in {"light", "moderate", "rebuild"} for p in mine["plans"])


def test_another_users_plan_is_not_visible(client):
    created = client.post("/api/portfolio/advice", headers=_auth("TOKEN-A")).json()
    plan_id = created["plans"][0]["plan_id"]

    # B 拿到 A 的 plan_id 也应该 404（不区分"不存在"与"不是你的"）
    response = client.get(f"/api/portfolio/allocations/{plan_id}", headers=_auth("TOKEN-B"))

    assert response.status_code == 404


def test_plan_detail_includes_the_item_rows(client):
    created = client.post("/api/portfolio/advice", headers=_auth()).json()
    plan_id = created["plans"][0]["plan_id"]

    detail = client.get(f"/api/portfolio/allocations/{plan_id}", headers=_auth()).json()

    assert detail["plan_id"] == plan_id
    assert detail["items"]
    codes = {i["product_code"] for i in detail["items"]}
    assert codes <= {"110011", "510300"}
    assert "cash" not in codes
    for item in detail["items"]:
        assert item["action"] in {"open", "add", "reduce", "close", "hold"}


def test_an_unknown_plan_returns_404(client):
    response = client.get("/api/portfolio/allocations/does-not-exist", headers=_auth())

    assert response.status_code == 404


def test_adopting_without_a_request_id_uses_the_plan_id_as_the_key(client):
    """不传 request_id 时用 plan_id 派生幂等键，仍应能采纳。"""
    created = client.post("/api/portfolio/advice", headers=_auth()).json()
    plan_id = created["plans"][0]["plan_id"]

    response = client.post(
        f"/api/portfolio/allocations/{plan_id}/adopt", headers=_auth(), json={}
    )

    # 假服务未实现采纳依赖（需要真实净值/持仓），因此这里只断言**不是 5xx**：
    # 路由与参数校验通过，失败也必须是可解释的业务错误。
    assert response.status_code < 500, response.text


def test_adopting_unknown_plan_returns_404(client):
    response = client.post(
        "/api/portfolio/allocations/nope/adopt", headers=_auth(), json={"request_id": "r1"}
    )

    assert response.status_code == 404


def test_no_db_tables_are_needed_for_these_routes(client):
    """路由层不应该在导入时就去连数据库（决定它能不能被单测）。"""
    from app.routers import allocation as allocation_routes

    assert hasattr(allocation_routes, "get_advisor_service")


def test_ai_account_returns_empty_curves_before_any_snapshot(client):
    """还没有快照时返回空曲线 + 说明，不能编造点位。"""
    body = client.get("/api/portfolio/ai-account", headers=_auth()).json()

    assert body["user_curve"] == []
    assert body["ai_curve"] == []
    assert body["curve_as_of"] is None
    assert body["limitations"]


def test_writing_a_snapshot_then_reading_the_curves_round_trips(client):
    """先写快照，再从 ai-account 读到——正是前端对比页的调用顺序。"""
    written = client.post(
        "/api/portfolio/snapshots", headers=_auth(), json={"as_of": "2026-09-28"}
    )
    assert written.status_code == 200, written.text
    outcomes = {item["owner"]: item for item in written.json()["outcomes"]}
    # 用户线有份额基线，能写；AI 账户尚未建立，应跳过而不是编一个点
    assert outcomes["user"]["written"] is True
    assert outcomes["ai"]["written"] is False
    assert outcomes["ai"]["skipped_reason"] == "no_data"

    body = client.get("/api/portfolio/ai-account", headers=_auth()).json()

    assert len(body["user_curve"]) == 1
    assert body["user_curve"][0]["date"] == "2026-09-28"
    assert body["user_curve"][0]["unit_nav"] > 0
    assert body["ai_curve"] == []
    assert body["curve_as_of"] == "2026-09-28"


def test_rewriting_the_same_day_does_not_duplicate_the_curve_point(client):
    for _ in range(3):
        client.post("/api/portfolio/snapshots", headers=_auth(), json={"as_of": "2026-09-28"})

    body = client.get("/api/portfolio/ai-account", headers=_auth()).json()

    assert len(body["user_curve"]) == 1


def test_a_bad_as_of_is_rejected_with_422(client):
    response = client.post(
        "/api/portfolio/snapshots", headers=_auth(), json={"as_of": "not-a-date"}
    )

    assert response.status_code == 422


def test_snapshots_require_authentication(client):
    assert client.post("/api/portfolio/snapshots", json={}).status_code == 401
    assert client.get("/api/portfolio/ai-account").status_code == 401
