"""V2 轮次持久化、恢复与公开输出边界。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.agent_service import AdvisorServiceError
from tests.test_agents.test_advisor_service import FakeGraph, _collect, _events, _service


def test_public_outputs_preserve_multiple_tasks_and_only_latest_attempt():
    from app.agents.graphs.state import StockResult, WorkerOutcome
    from app.agents.stock.contracts import CandidateStock, StockResultV1
    from app.services.agent_service import _collect_warnings, _public_artifacts

    def result(symbol, warning, status="ok"):
        return StockResult(status=status, summary=symbol, warnings=[warning],
            result=StockResultV1(scenario="market_overview", summary=symbol),
            candidates=[CandidateStock(thscode=symbol, name=symbol, score=80)])

    old = result("OLD", "旧尝试")
    first = result("FIRST", "第一任务")
    second = result("SECOND", "第二任务")
    failed = result("FAILED", "失败告警", "failed")
    clarification = result("CLARIFY", "待澄清")
    values = {"stock_result": old, "outcomes": {
        "first:1": WorkerOutcome(task_id="first", agent="stock_agent", result=old),
        "first:2": WorkerOutcome(task_id="first", agent="stock_agent", attempt=2, result=first).model_dump(),
        "second:1": WorkerOutcome(task_id="second", agent="stock_agent", result=second),
        "failed:1": WorkerOutcome(task_id="failed", agent="stock_agent", result=failed),
        "clarify:1": WorkerOutcome(task_id="clarify", agent="stock_agent", result=clarification, clarification={"question": "请选择"}),
    }}
    artifacts, candidates = _public_artifacts(values)
    assert len(artifacts) == 2
    assert [item["thscode"] for item in candidates] == ["FIRST", "SECOND"]
    assert _collect_warnings(values) == ["第一任务", "第二任务", "失败告警", "待澄清"]


def test_completion_payloads_are_emitted_only_after_atomic_context_completion():
    """真流式的固有代价：正文 token 在落库前即可下发；但 artifact/warning/done 必须落库成功后才发。

    旧契约是"落库前不得下发任何内容"，与增量流式在语义上互斥，故此处按新契约断言：
    完成类载荷仍严格晚于原子落库，而正文增量允许先行。
    """
    service, graph, _ = _service()
    context = {"version": 2, "slots": {"stock": {"active_context_id": "s1"}}, "pending_tasks": []}
    graph.results["session_context"] = context
    graph.answer = "经过验证的答案"
    original = service._persist_completion
    persisted = []

    def persist(*args, **kwargs):
        result = original(*args, **kwargs)
        persisted.append(result)
        return result

    service._persist_completion = persist
    prepared = service.prepare_chat("CUST000001", request_id="v2-output", session_id=None, message="分析")

    async def collect():
        events = []
        async for event in service.play("CUST000001", prepared):
            if event["event"] in {"artifact", "warning", "done"}:
                assert persisted == [True]
                assert service.store.sessions[prepared.session_id]["context"] == context
            events.append(event)
        return events

    events = asyncio.run(collect())
    assert [event["data"]["text"] for event in events if event["event"] == "token"] == [graph.answer]


def test_skipped_completion_emits_no_completion_payloads():
    """落库被跳过时不再发 artifact/warning/done，只以 error 收尾。

    已经流式下发的正文 token 无法撤回（这是真流式的代价），前端据此走"本轮没有完成"提示。
    """
    service, graph, _ = _service()
    service._persist_completion = lambda *_args, **_kwargs: False
    events = _events(service, request_id="v2-skipped", session_id=None, message="分析")
    assert not {"artifact", "warning", "done"} & {event["event"] for event in events}
    assert events[-1] == {"event": "error", "data": {"code": "turn_state_changed", "retryable": False}}


def test_duplicate_legacy_active_request_emits_version_error():
    service, _, _ = _service()
    session = service.store.insert_session("CUST000001", "旧会话")
    service.store.insert_turn(session["id"], "legacy", "interrupted", graph_version="legacy")
    events = _events(service, request_id="legacy", session_id=session["id"], message="旧请求重放")
    assert events[-1] == {"event": "error", "data": {"code": "graph_version_changed", "retryable": False}}


def test_legacy_completed_request_replays_saved_answer():
    service, graph, _ = _service()
    session = service.store.insert_session("CUST000001", "旧会话")
    service.store.insert_turn(session["id"], "legacy", "completed", graph_version="legacy")
    service.store.update_turn("legacy", final_answer="原有答案")
    events = _events(service, request_id="legacy", session_id=session["id"], message="旧请求重放")
    assert events[-1]["data"]["answer"] == "原有答案"
    assert graph.inputs == []


@pytest.mark.parametrize("status", ["running", "interrupted", "failed"])
def test_new_turn_invalidates_legacy_active_or_retryable_turn(status):
    service, _, _ = _service()
    session = service.store.insert_session("CUST000001", "旧会话")
    service.store.insert_turn(session["id"], "legacy", status)
    service.store.update_turn("legacy", graph_version="legacy", retryable=True)
    prepared = service.prepare_chat("CUST000001", request_id="v2-new", session_id=session["id"], message="重新分析")
    old = service.store.find_turn("legacy")
    assert old["status"] == "failed"
    assert old["error_code"] == "graph_version_changed"
    assert old["retryable"] is False
    assert prepared.turn["graph_version"] == "advisor-v2"


@pytest.mark.parametrize("method,payload", [
    ("prepare_resume", {"risk_level": "R3"}),
    ("prepare_clarify", {"symbols": "000001.SZ"}),
    ("prepare_allocation_adopt", {"plan_id": "p1"}),
    ("prepare_retry", None),
])
def test_legacy_continuation_is_rejected_with_version_error(method, payload):
    service, _, _ = _service()
    session = service.store.insert_session("CUST000001", "旧会话")
    service.store.insert_turn(session["id"], "legacy", "failed" if payload is None else "interrupted")
    service.store.update_turn("legacy", graph_version="legacy", retryable=True)
    args = ["CUST000001", "legacy"] + ([] if payload is None else [payload])
    with pytest.raises(AdvisorServiceError) as error:
        getattr(service, method)(*args)
    assert error.value.status_code == 409
    assert error.value.code == "graph_version_changed"
    assert service.store.find_turn("legacy")["retryable"] is False


def test_request_checkpoints_are_isolated_and_use_fresh_confirmed_profile():
    class ConfigGraph(FakeGraph):
        async def astream(self, graph_input, config, **kwargs):
            self.config = config
            async for item in super().astream(graph_input, config, **kwargs):
                yield item

    graph = ConfigGraph()
    service, _, saver = _service(graph)
    events = _events(service, request_id="v2-first", session_id=None, message="分析")
    session_id = events[0]["data"]["session_id"]
    service.store.upsert_profile("CUST000001", {"risk_level": "R4"})
    _events(service, request_id="v2-second", session_id=session_id, message="继续")
    config = graph.config["configurable"]
    assert config["thread_id"] == f"advisor-v2:{session_id}:v2-second"
    assert config["session_id"] == session_id
    assert config["verified_profile"]["risk_level"] == "R4"
    asyncio.run(service.delete_session("CUST000001", session_id))
    assert set(saver.deleted) == {f"advisor-v2:{session_id}:v2-first", f"advisor-v2:{session_id}:v2-second"}


def test_execution_timeout_fails_turn_without_answer(monkeypatch):
    class SlowGraph(FakeGraph):
        async def astream(self, *_args, **_kwargs):
            await asyncio.sleep(1)
            yield ("updates", {})

    monkeypatch.setattr("app.services.agent_service.load_advisor_settings", lambda: SimpleNamespace(
        max_message_chars=500, context_message_limit=10, turn_timeout_seconds=0.01,
    ))
    service, _, _ = _service(SlowGraph())
    events = _events(service, request_id="v2-timeout", session_id=None, message="慢请求")
    assert events[-1] == {"event": "error", "data": {"code": "timeout", "retryable": True}}
    assert service.store.find_turn("v2-timeout")["status"] == "failed"


@pytest.mark.parametrize("operation", ["graph_input", "verified_profile"])
def test_pre_execution_database_failure_emits_error_and_fails_turn(monkeypatch, operation):
    service, graph, _ = _service()
    prepared = service.prepare_chat("CUST000001", request_id="v2-db-failure", session_id=None, message="分析")

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("database unavailable")

    if operation == "graph_input":
        monkeypatch.setattr(service.store, "list_messages", unavailable)
    else:
        monkeypatch.setattr(service, "get_profile", unavailable)

    events = asyncio.run(_collect(service, prepared))
    assert [event["event"] for event in events] == ["run", "error"]
    assert events[-1]["data"] == {"code": "connection_error", "retryable": True}
    assert service.store.find_turn(prepared.request_id)["status"] == "failed"
    assert graph.inputs == []
