from dataclasses import replace

import pytest

from app.agents.stock.evidence_store import EvidenceAccessError, EvidenceContext, MemoryAgentEvidenceStore


def context(task_id="a", dependencies=()):
    return EvidenceContext("request", "session", "customer", task_id=task_id, dependency_task_ids=dependencies)


def save(store, ctx, payload):
    return store.put(ctx, tool_call_id="tool", tool_name="get_product_performance", evidence_type="product",
                     payload=payload, source="postgresql", as_of="2026-10-10")


@pytest.mark.asyncio
async def test_chat_receives_verified_facts_from_authorized_dependency(monkeypatch):
    import json
    from app.agents import builders
    from app.agents.graphs.state import ChatResult
    store = MemoryAgentEvidenceStore()
    row = save(store, context(), {"data": {"code": "001", "nav": 2.5}})
    fact = {"id": "nav", "evidence_id": row["id"], "field_path": "data.nav", "subject": "001",
            "metric": "nav", "value": 2.5, "unit": "", "as_of": "2026-10-10"}
    monkeypatch.setattr(builders, "get_config", lambda: {"configurable": {
        "request_id": "request", "session_id": "session", "customer_id": "customer",
        "task_id": "b", "dependency_task_ids": ["a"]}})
    class Agent:
        async def ainvoke(self, payload, config):
            data = json.loads(payload["messages"][0]["content"])
            assert data["dependency_results"][0]["facts"] == [fact]
            return {"messages": [{"content": "已解释查询事实"}]}
    result = await builders._runner(Agent(), "chat_agent", "", "不可用", ChatResult,
                                    evidence_store=store)("解释查询结果", {"dependency_results": [{"facts": [fact]}]})
    assert result.summary == "已解释查询事实"


@pytest.mark.asyncio
async def test_production_chat_factory_keeps_dependency_evidence_access(monkeypatch):
    import json
    from app.agents import builders
    store = MemoryAgentEvidenceStore()
    row = save(store, context(), {"data": {"code": "001", "nav": 2.5}})
    fact = {"id": "nav", "evidence_id": row["id"], "field_path": "data.nav", "subject": "001",
            "metric": "nav", "value": 2.5, "unit": "", "as_of": "2026-10-10"}
    monkeypatch.setattr(builders, "get_config", lambda: {"configurable": {
        "request_id": "request", "session_id": "session", "customer_id": "customer",
        "task_id": "b", "dependency_task_ids": ["a"]}})
    class Agent:
        async def ainvoke(self, payload, config):
            data = json.loads(payload["messages"][0]["content"])
            assert data["dependency_results"][0]["facts"] == [fact]
            return {"messages": [{"content": "已解释"}]}
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: Agent())
    runners = builders.build_specialists(object(), evidence_store=store)
    result = await runners["chat_agent"]("解释", {"dependency_results": [{"facts": [fact]}]})
    assert result.summary == "已解释"


def test_task_evidence_is_readable_only_by_owner_or_authorized_dependency():
    store = MemoryAgentEvidenceStore()
    row = save(store, context(), {"data": {"code": "001", "nav": 2.5}})
    assert row["task_id"] == "a"
    with pytest.raises(EvidenceAccessError):
        store.get(row["id"], context("b"))
    assert store.get(row["id"], context("b", ("a",)))["id"] == row["id"]
    with pytest.raises(EvidenceAccessError):
        store.get(row["id"], replace(context("b", ("a",)), request_id="other"))
    with pytest.raises(EvidenceAccessError):
        store.get(row["id"], EvidenceContext("request", "session", "customer"))


def test_fact_verifies_value_against_same_subject_metric_and_path():
    from app.agents.evidence import FactReference, verify_fact

    store = MemoryAgentEvidenceStore()
    row = save(store, context(), {"data": [{"code": "001", "nav": 2.5, "unit": "元"},
                                          {"code": "002", "nav": 9.0, "unit": "元"}]})
    reference = FactReference(id="nav-a", evidence_id=row["id"], field_path="data.0.nav", subject="001",
                              metric="nav", value=2.5, unit="元", as_of="2026-10-10")
    assert verify_fact(store, context(), reference).value == 2.5
    for updates in ({"subject": "002"}, {"value": 9.0}, {"metric": "yield"},
                    {"field_path": "data.1.nav"}, {"as_of": "2026-10-09"}, {"unit": "%"}):
        with pytest.raises(EvidenceAccessError):
            verify_fact(store, context(), reference.model_copy(update=updates))


def test_clarification_tool_raises_typed_request_without_interrupt():
    from app.agents.evidence import NeedsClarification
    from app.agents.stock.tools import build_stock_local_tools

    tool = next(t for t in build_stock_local_tools(store=MemoryAgentEvidenceStore())
                if t.name == "request_stock_clarification")
    with pytest.raises(NeedsClarification) as caught:
        tool.invoke({"reason": "ambiguous", "question": "请选择标的", "fields": ["symbols"],
                     "options": [{"id": "000001.SZ", "label": "平安银行"}]})
    assert caught.value.payload["options"][0]["id"] == "000001.SZ"


@pytest.mark.asyncio
async def test_readonly_worker_rejects_unreferenced_recommendations_and_derives_source(monkeypatch):
    from app.agents import builders
    from app.agents.evidence import capture_evidence
    from app.agents.graphs.state import ProductResult

    store = MemoryAgentEvidenceStore()
    monkeypatch.setattr(builders, "get_config", lambda: {"configurable": {
        "request_id": "request", "session_id": "session", "thread_id": "isolated-thread",
        "customer_id": "customer", "task_id": "a"}})

    class Agent:
        async def ainvoke(self, payload, config):
            assert "domain_slots" in payload["messages"][0]["content"]
            row = capture_evidence({"source": "actual-source", "as_of": "2026-10-10",
                                    "data": {"code": "001", "name": "产品A"}}, tool_name="get_product_detail")
            return {"structured_response": {"summary": "推荐001与999", "recommended_codes": ["001", "999"],
                                             "evidence_ids": [row["evidence_id"]], "facts": []}}

    runner = builders._runner(Agent(), "product_agent", "postgresql", "不可用", ProductResult,
                              evidence_store=store)
    result = await runner("查询", {"domain_slots": {"product_codes": ["001"]}, "user_query": "查询"})
    assert result.recommended_codes == ["001"]
    assert result.status == "degraded"
    assert result.source == "actual-source"
    assert result.as_of == "2026-10-10"
    assert "999" not in result.summary


@pytest.mark.asyncio
async def test_readonly_worker_missing_structure_is_not_success(monkeypatch):
    from app.agents import builders
    from app.agents.graphs.state import ProductResult

    monkeypatch.setattr(builders, "get_config", lambda: {"configurable": {
        "request_id": "request", "session_id": "session", "customer_id": "customer", "task_id": "a"}})

    class Agent:
        async def ainvoke(self, payload, config):
            return {"messages": [{"content": "推荐999收益率99%"}]}

    result = await builders._runner(Agent(), "product_agent", "postgresql", "不可用", ProductResult,
                                    evidence_store=MemoryAgentEvidenceStore())("查询", {})
    assert result.status != "ok"
    assert result.recommended_codes == []
    assert "99" not in result.summary


def test_readonly_tool_does_not_read_codes_outside_confirmed_slots():
    import json
    from app.agents.evidence import bind_execution, reset_execution
    from app.agents.tools.database import bind_readonly_services, reset_readonly_services, get_product_detail

    class Library:
        def query_by_code(self, code):
            raise AssertionError("越界代码不应触达数据源")

    services = bind_readonly_services(Library(), None)
    token = bind_execution(MemoryAgentEvidenceStore(), context(), domain_slots={"codes": ["001"]})
    try:
        result = json.loads(get_product_detail.invoke({"product_code": "999"}))
        assert result["data"] is None
        assert result["warning"] == "product_outside_confirmed_slots"
    finally:
        reset_execution(token)
        reset_readonly_services(services)


def test_stock_tool_arguments_cannot_override_scoped_symbol_or_timeframe():
    from app.agents.evidence import bind_execution, reset_execution, validate_tool_arguments

    token = bind_execution(MemoryAgentEvidenceStore(), context(), domain_slots={"symbols": ["600519.SH"],
                           "timeframe": {"start": "2025-10-10", "end": "2026-10-10"}})
    try:
        assert validate_tool_arguments("get_a_share_prices_historical", {"thscode": "600519.SH",
                                       "start_date": "20251010", "end_date": "20261010"}) == ""
        assert validate_tool_arguments("get_a_share_prices_historical", {"thscode": "000001.SZ",
                                       "start_date": "20251010", "end_date": "20261010"})
        assert validate_tool_arguments("get_a_share_prices_historical", {"thscode": "600519.SH",
                                       "start_date": "20240101", "end_date": "20261010"})
    finally:
        reset_execution(token)


def test_server_fact_projection_preserves_entity_value_association():
    from app.agents.evidence import facts_from_evidence

    store = MemoryAgentEvidenceStore()
    row = save(store, context(), {"data": [{"code": "001", "nav": 2.5, "unit": "元"},
                                          {"code": "002", "nav": 9.0, "unit": "元"}]})
    facts = facts_from_evidence(store, context(), [row["id"]])
    assert {(fact.subject, fact.metric, fact.value) for fact in facts} == {("001", "nav", 2.5), ("002", "nav", 9.0)}
    assert all(fact.unit == "元" and fact.as_of == "2026-10-10" for fact in facts)


@pytest.mark.asyncio
async def test_stock_runner_preserves_pending_clarification_when_agent_wraps_tool_error(monkeypatch):
    from app.agents import builders
    from app.agents.evidence import NeedsClarification
    from app.agents.stock.tools import build_stock_local_tools

    store = MemoryAgentEvidenceStore()
    clarification = next(t for t in build_stock_local_tools(store=store) if t.name == "request_stock_clarification")
    monkeypatch.setattr(builders, "get_config", lambda: {"configurable": {
        "request_id": "request", "session_id": "session", "customer_id": "customer", "task_id": "a"}})

    class Agent:
        async def ainvoke(self, payload, config):
            try:
                clarification.invoke({"reason": "ambiguous", "question": "请选择真实标的", "fields": ["symbols"],
                                      "options": [{"id": "000001.SZ", "label": "平安银行"}]})
            except NeedsClarification:
                raise RuntimeError("工具重试包装了错误")

    with pytest.raises(NeedsClarification) as caught:
        await builders.stock_agent_runner(Agent(), store)("查询", {"domain_slots": {"symbols": ["000001.SZ"]}})
    assert caught.value.payload["options"][0]["id"] == "000001.SZ"


def test_bound_clarification_discards_model_invented_security_options():
    from app.agents.evidence import NeedsClarification, bind_execution, reset_execution
    from app.agents.stock.tools import build_stock_local_tools

    store = MemoryAgentEvidenceStore()
    token = bind_execution(store, context(), domain_slots={"symbols": ["600519.SH"]})
    try:
        clarification = next(t for t in build_stock_local_tools(store=store) if t.name == "request_stock_clarification")
        with pytest.raises(NeedsClarification) as caught:
            clarification.invoke({"reason": "ambiguous", "question": "请选择收益99%的标的", "fields": ["symbols"],
                                  "options": [{"id": "999999.SH", "label": "模型编造标的"}]})
        assert caught.value.payload["options"] == []
        assert "99" not in caught.value.payload["question"]
    finally:
        reset_execution(token)
