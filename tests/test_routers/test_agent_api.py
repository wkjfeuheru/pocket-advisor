"""投顾 HTTP：鉴权、隔离、SSE 与降级。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.runtime import AgentRuntime, set_agent_runtime
from app.services.agent_service import AgentService
from tests.test_agents.test_advisor_service import FakeGraph, FakeSaver

TOKEN_BY_CUSTOMER = {
    "TOKEN-A": "CUST000001",
    "TOKEN-B": "CUST000002",
    "TOKEN-ADMIN": "CUSTADMIN",
}


class _Users:
    def verify_token(self, token: str):
        return TOKEN_BY_CUSTOMER.get(token)

    def is_admin(self, customer_id: str) -> bool:
        return str(customer_id).upper() == "CUSTADMIN"


@pytest.fixture()
def api(monkeypatch):
    from tests.test_agents.memory_store import MemoryAdvisorStore

    graph = FakeGraph()
    saver = FakeSaver()
    runtime = AgentRuntime(
        status="ready",
        reason="",
        graph=graph,
        checkpointer=saver,
        library=object(),
        portfolio=object(),
    )
    set_agent_runtime(runtime)
    service = AgentService(store=MemoryAdvisorStore(), runtime_getter=lambda: runtime)
    monkeypatch.setattr("app.core.dependencies.get_user_store", lambda: _Users())
    monkeypatch.setattr("app.routers.agent.get_agent_service", lambda: service)
    monkeypatch.setattr("app.routers.agent.get_agent_runtime", lambda: runtime)

    from app.routers.agent import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.service = service
    client.graph = graph
    client.runtime = runtime
    return client


def auth(token: str = "TOKEN-A") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_agent_routes_require_a_user(api):
    assert api.get("/api/agents/health").status_code == 401
    assert api.get("/api/agents/sessions").status_code == 401
    assert api.post("/api/agents/chat/stream", json={
        "request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "message": "你好",
    }).status_code == 401


def test_admin_is_forbidden(api):
    response = api.get("/api/agents/health", headers=auth("TOKEN-ADMIN"))
    assert response.status_code == 403


def test_stream_event_order_hides_expert_output(api):
    response = api.post("/api/agents/chat/stream", headers=auth(), json={
        "request_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "message": "你好",
    })
    assert response.status_code == 200
    body = response.text
    assert body.index("event: run") < body.index("event: status") < body.index("event: token") < body.index("event: done")
    assert "EXPERT_SECRET" not in body
    assert "function-calling" not in body
    assert api.graph.inputs


def test_other_user_cannot_resume_or_read(api):
    created = api.post("/api/agents/chat/stream", headers=auth(), json={
        "request_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "message": "只属于甲",
    })
    assert created.status_code == 200
    request_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert api.get(f"/api/agents/runs/{request_id}", headers=auth("TOKEN-B")).status_code == 404
    sessions = api.get("/api/agents/sessions", headers=auth("TOKEN-B"))
    assert sessions.json() == []


def test_profile_is_saved_only_from_the_submitted_fields(api):
    saved = api.patch("/api/agents/profile", headers=auth(), json={"risk_level": "R3", "liquidity_need": "高"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["risk_level"] == "R3"
    assert body["liquidity_need"] == "高"
    assert body["budget_amount"] is None
    assert api.patch("/api/agents/profile", headers=auth(), json={"risk_level": "nope"}).status_code == 422


def test_clarify_rejects_undeclared_fields_and_unknown_options(api):
    api.graph.mode = "interrupt"
    api.graph.interrupt_payload = {
        "kind": "clarification",
        "agent_version": "stock-react-v1",
        "reason": "ambiguous_symbol",
        "question": "请选择股票",
        "fields": ["symbols"],
        "options": [{"id": "000001.SZ", "label": "平安银行 000001.SZ"}],
        "request": {"scenario": "equity_research"},
    }
    request_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    created = api.post("/api/agents/chat/stream", headers=auth(), json={
        "request_id": request_id,
        "message": "分析平安",
    })
    assert "event: clarification_required" in created.text
    assert "event: profile_required" not in created.text
    extra = api.post(
        f"/api/agents/runs/{request_id}/clarify/stream",
        headers=auth(),
        json={"answers": {"symbols": "000001.SZ", "horizon": "short"}},
    )
    assert extra.status_code == 422
    unknown = api.post(
        f"/api/agents/runs/{request_id}/clarify/stream",
        headers=auth(),
        json={"answers": {"symbols": "999999.SH"}},
    )
    assert unknown.status_code == 422
    assert api.post(
        f"/api/agents/runs/{request_id}/clarify/stream",
        headers=auth("TOKEN-B"),
        json={"answers": {"symbols": "000001.SZ"}},
    ).status_code == 404
    api.graph.mode = "done"
    ok = api.post(
        f"/api/agents/runs/{request_id}/clarify/stream",
        headers=auth(),
        json={"answers": {"symbols": "000001.SZ"}},
    )
    assert ok.status_code == 200
    from langgraph.types import Command
    assert isinstance(api.graph.inputs[-1], Command)
    profile = api.get("/api/agents/profile", headers=auth()).json()
    assert profile["risk_level"] is None


def test_allocation_adopt_resumes_only_with_a_declared_plan(api):
    api.graph.mode = "interrupt"
    api.graph.interrupt_payload = {
        "kind": "allocation",
        "agent_version": "portfolio-allocation-v1",
        "plans": [
            {"plan_id": "p-light", "tier": "light", "items": []},
            {"plan_id": "p-moderate", "tier": "moderate", "unavailable_reason": "no_candidate", "items": []},
        ],
    }
    request_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    created = api.post("/api/agents/chat/stream", headers=auth(), json={
        "request_id": request_id,
        "message": "帮我调仓",
    })
    assert "event: allocation_required" in created.text
    assert "event: profile_required" not in created.text
    profile_resume = api.post(
        f"/api/agents/runs/{request_id}/resume/stream",
        headers=auth(),
        json={"profile": {"risk_level": "R3"}},
    )
    assert profile_resume.status_code == 409
    unknown = api.post(
        f"/api/agents/runs/{request_id}/allocation/stream",
        headers=auth(),
        json={"plan_id": "p-rebuild"},
    )
    assert unknown.status_code == 422
    extra = api.post(
        f"/api/agents/runs/{request_id}/allocation/stream",
        headers=auth(),
        json={"plan_id": "p-light", "weights": {"110011": 1}},
    )
    assert extra.status_code == 422
    api.graph.mode = "done"
    ok = api.post(
        f"/api/agents/runs/{request_id}/allocation/stream",
        headers=auth(),
        json={"plan_id": "p-light"},
    )
    assert ok.status_code == 200
    from langgraph.types import Command
    assert isinstance(api.graph.inputs[-1], Command)
    assert api.graph.inputs[-1].resume == {"plan_id": "p-light"}


def test_degraded_runtime_returns_503_without_secrets(api):
    api.runtime.status = "degraded"
    api.runtime.reason = "advisor model is not configured"
    api.runtime.graph = None
    health = api.get("/api/agents/health", headers=auth())
    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert "postgres" not in health.text
    assert "api_key" not in health.text.lower()
    response = api.post("/api/agents/chat/stream", headers=auth(), json={
        "request_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "message": "你好",
    })
    assert response.status_code == 503


def test_business_health_stays_available_when_agent_runtime_is_degraded(monkeypatch):
    from fastapi.testclient import TestClient

    async def degraded():
        set_agent_runtime(AgentRuntime(status="degraded", reason="advisor model is not configured"))

    monkeypatch.setattr("app.main.init_agents", degraded)
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/agents/health").status_code == 401
