"""投顾服务的幂等、隔离与恢复。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.agents.runtime import AgentRuntime
from app.services.agent_service import AdvisorServiceError, AgentService
from app.services.portfolio_service import SIMULATED_DISCLAIMER
from tests.test_agents.memory_store import MemoryAdvisorStore


class ThreadTrackingStore(MemoryAdvisorStore):
    def __init__(self) -> None:
        super().__init__()
        self.transaction_threads: list[int] = []

    @contextmanager
    def transaction(self):
        self.transaction_threads.append(threading.get_ident())
        with super().transaction():
            yield self


class RacingRetryStore(MemoryAdvisorStore):
    def __init__(self) -> None:
        super().__init__()
        self.retry_barrier = threading.Barrier(2)

    def find_turn(self, request_id: str):
        turn = super().find_turn(request_id)
        if turn is not None and turn["status"] == "failed" and turn["retryable"]:
            self.retry_barrier.wait(timeout=5)
        return turn


class FakeSaver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class FakeGraph:
    def __init__(self) -> None:
        self.inputs: list = []
        self.mode = "done"
        self.answer = f"汇总完成\n\n{SIMULATED_DISCLAIMER}"
        self.interrupt_payload = None
        self.agent_results = []

    async def astream(self, graph_input, config, stream_mode=None, durability=None):
        assert durability == "sync"
        assert config["configurable"]["thread_id"]
        self.inputs.append(graph_input)
        yield ("updates", {"classify_intent": {}})
        yield ("messages", (SimpleNamespace(content="汇总"), {"langgraph_node": "aggregator"}))
        yield ("messages", (SimpleNamespace(content="EXPERT_SECRET"), {"langgraph_node": "stock_agent"}))
        if self.mode == "interrupt":
            return
        if self.mode == "fail":
            from app.agents.graphs.task_agent import AllBranchesFailed

            raise AllBranchesFailed("all")

    async def aget_state(self, config):
        if self.mode == "interrupt":
            payload = self.interrupt_payload or {
                "missing_fields": ["risk_level", "investment_horizon", "investment_goal", "liquidity_need"],
                "profile": {},
            }
            task = SimpleNamespace(interrupts=[SimpleNamespace(value=payload)])
            return SimpleNamespace(values={}, tasks=[task], interrupts=())
        return SimpleNamespace(
            values={
                "final_result": self.answer,
                "warnings": ["股票行情暂不可用"],
                "agent_results": self.agent_results,
            },
            tasks=[],
            interrupts=(),
        )


def _service(graph: FakeGraph | None = None, ready: bool = True):
    graph = graph or FakeGraph()
    saver = FakeSaver()
    runtime = AgentRuntime(
        status="ready" if ready else "degraded",
        reason="" if ready else "advisor model is not configured",
        graph=graph if ready else None,
        checkpointer=saver,
        library=object(),
        portfolio=object(),
    )
    service = AgentService(store=MemoryAdvisorStore(), runtime_getter=lambda: runtime)
    return service, graph, saver


def _events(service: AgentService, **kwargs):
    prepared = service.prepare_chat("cust000001", **kwargs)
    async def collect():
        return [event async for event in service.play("cust000001", prepared)]
    return asyncio.run(collect())


def test_first_turn_creates_one_user_message_and_filters_expert_tokens():
    service, graph, _saver = _service()
    message = "  帮我看看\n这只   基金的风险和持仓，另外再聊聊市场  "
    events = _events(service, request_id="11111111-1111-1111-1111-111111111111", session_id=None, message=message)
    names = [event["event"] for event in events]
    assert names[0] == "run"
    assert "status" in names
    assert "token" in names
    assert names[-1] == "done"
    assert all(event["data"].get("text") != "EXPERT_SECRET" for event in events)
    assert "EXPERT_SECRET" not in events[-1]["data"]["answer"]
    with service.store.transaction():
        sessions = service.store.list_sessions("CUST000001")
        messages = service.store.list_messages("CUST000001", sessions[0]["id"])
    assert sessions[0]["title"] == "帮我看看 这只 基金的风险和持仓，另外再聊聊市场"
    assert len(sessions[0]["title"]) <= 40
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert len(graph.inputs) == 1


def test_only_guard_approved_final_answer_is_streamed_as_tokens():
    service, graph, _saver = _service()
    graph.answer = f"合规后的最终答案\n\n{SIMULATED_DISCLAIMER}"

    events = _events(
        service,
        request_id="10101010-1010-1010-1010-101010101010",
        session_id=None,
        message="请给我投资建议",
    )

    streamed = "".join(
        event["data"]["text"] for event in events if event["event"] == "token"
    )
    assert streamed == graph.answer
    assert "汇总" not in streamed


def test_run_snapshot_persists_current_stage():
    service, _graph, _saver = _service()
    request_id = "12121212-1212-1212-1212-121212121212"

    _events(service, request_id=request_id, session_id=None, message="查看进度")

    run = service.get_run("CUST000001", request_id)
    assert run["stage"] == "完成"


def test_async_play_and_delete_do_not_run_store_transactions_on_event_loop_thread():
    graph = FakeGraph()
    saver = FakeSaver()
    runtime = AgentRuntime(
        status="ready",
        graph=graph,
        checkpointer=saver,
        library=object(),
        portfolio=object(),
    )
    store = ThreadTrackingStore()
    service = AgentService(store=store, runtime_getter=lambda: runtime)
    prepared = service.prepare_chat(
        "CUST000001",
        request_id="13131313-1313-1313-1313-131313131313",
        session_id=None,
        message="不要阻塞事件循环",
    )
    store.transaction_threads.clear()

    async def exercise():
        event_loop_thread = threading.get_ident()
        await _collect(service, prepared)
        await service.delete_session("CUST000001", prepared.session_id)
        return event_loop_thread

    event_loop_thread = asyncio.run(exercise())
    assert store.transaction_threads
    assert event_loop_thread not in store.transaction_threads


def test_duplicate_completed_request_does_not_run_again():
    service, graph, _saver = _service()
    request_id = "22222222-2222-2222-2222-222222222222"
    first = _events(service, request_id=request_id, session_id=None, message="第一问")
    session_id = first[0]["data"]["session_id"]
    second = _events(service, request_id=request_id, session_id=session_id, message="第一问")
    assert [event["event"] for event in second] == ["run", "done"]
    assert second[-1]["data"]["answer"] == first[-1]["data"]["answer"]
    assert len(graph.inputs) == 1
    with service.store.transaction():
        messages = service.store.list_messages("CUST000001", session_id)
    assert len([item for item in messages if item["role"] == "user"]) == 1


def test_customers_cannot_read_each_others_turns():
    service, _graph, _saver = _service()
    events = _events(service, request_id="33333333-3333-3333-3333-333333333333", session_id=None, message="我的问题")
    session_id = events[0]["data"]["session_id"]
    with pytest.raises(AdvisorServiceError) as exc:
        service.list_messages("CUST000002", session_id)
    assert exc.value.status_code == 404
    with pytest.raises(AdvisorServiceError) as missing:
        service.get_run("CUST000002", "33333333-3333-3333-3333-333333333333")
    assert missing.value.status_code == 404


def test_second_active_request_conflicts_and_retry_does_not_reinject_message():
    service, graph, _saver = _service()
    graph.mode = "interrupt"
    request_id = "44444444-4444-4444-4444-444444444444"
    events = _events(service, request_id=request_id, session_id=None, message="需要画像")
    assert events[-1]["event"] == "profile_required"
    session_id = events[0]["data"]["session_id"]
    with pytest.raises(AdvisorServiceError) as conflict:
        service.prepare_chat("CUST000001", request_id="55555555-5555-5555-5555-555555555555", session_id=session_id, message="另一问")
    assert conflict.value.status_code == 409
    with pytest.raises(AdvisorServiceError) as not_retry:
        service.prepare_retry("CUST000001", request_id)
    assert not_retry.value.status_code == 409

    with service.store.transaction():
        service.store.update_turn(request_id, status="failed", retryable=True, error_code="turn_failed")
    graph.mode = "done"
    prepared = service.prepare_retry("CUST000001", request_id)
    asyncio.run(_collect(service, prepared))
    assert graph.inputs[-1] is None
    with service.store.transaction():
        messages = service.store.list_messages("CUST000001", session_id)
    assert len([item for item in messages if item["role"] == "user"]) == 1


def test_only_one_concurrent_retry_can_claim_a_failed_turn():
    graph = FakeGraph()
    runtime = AgentRuntime(status="ready", graph=graph)
    store = RacingRetryStore()
    service = AgentService(store=store, runtime_getter=lambda: runtime)
    request_id = "14141414-1414-1414-1414-141414141414"
    with store.transaction():
        session = store.insert_session("CUST000001", "并发重试")
        store.insert_turn(session["id"], request_id, "failed")
        store.update_turn(request_id, retryable=True)

    def attempt():
        try:
            service.prepare_retry("CUST000001", request_id)
            return "claimed"
        except AdvisorServiceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: attempt(), range(2)))

    assert sorted(results) == ["claimed", "not_retryable"]


def test_resume_persists_only_submitted_profile_fields():
    service, graph, _saver = _service()
    graph.mode = "interrupt"
    request_id = "66666666-6666-6666-6666-666666666666"
    _events(service, request_id=request_id, session_id=None, message="个性化配置")
    with pytest.raises(AdvisorServiceError) as incomplete:
        service.prepare_resume("CUST000001", request_id, {"risk_level": "R2"})
    assert incomplete.value.status_code == 400
    graph.mode = "done"
    prepared = service.prepare_resume("CUST000001", request_id, {
        "risk_level": "R2",
        "investment_horizon": "长期",
        "investment_goal": "成长",
        "liquidity_need": "低",
    })
    assert isinstance(prepared.resume_value, dict)
    with service.store.transaction():
        profile = service.store.get_profile("CUST000001")
    assert profile["risk_level"] == "R2"
    assert profile["budget_amount"] is None


def test_clarify_answers_accept_only_the_options_of_that_field():
    from app.services.agent_service import validate_clarify_answers

    payload = {
        "kind": "clarification",
        "fields": ["strategy", "horizon"],
        "options": [
            {"field": "strategy", "id": "value_quality", "label": "价值质量"},
            {"field": "horizon", "id": "short", "label": "短线"},
            {"field": "horizon", "id": "medium", "label": "中期"},
        ],
    }
    with pytest.raises(AdvisorServiceError) as crossed:
        validate_clarify_answers(payload, {"strategy": "short", "horizon": "medium"})
    assert crossed.value.status_code == 422
    cleaned = validate_clarify_answers(payload, {"strategy": "value_quality", "horizon": "short"})
    assert cleaned == {"strategy": "value_quality", "horizon": "short"}


def test_clarification_interrupt_uses_a_separate_sse_event():
    service, graph, _saver = _service()
    graph.mode = "interrupt"
    graph.interrupt_payload = {
        "kind": "clarification",
        "agent_version": "stock-react-v1",
        "reason": "ambiguous_symbol",
        "question": "请选择要分析的股票",
        "fields": ["symbols"],
        "options": [{"id": "000001.SZ", "label": "平安银行 000001.SZ"}, {"id": "601318.SH", "label": "中国平安 601318.SH"}],
        "request": {"scenario": "equity_research"},
    }
    request_id = "15151515-1515-1515-1515-151515151515"
    events = _events(service, request_id=request_id, session_id=None, message="分析平安")
    assert events[-1]["event"] == "clarification_required"
    assert events[-1]["data"]["reason"] == "ambiguous_symbol"
    assert events[-1]["data"]["fields"] == ["symbols"]
    assert "profile_required" not in [event["event"] for event in events]
    with service.store.transaction():
        turn = service.store.find_turn(request_id)
        profile = service.store.get_profile("CUST000001")
    assert turn["stage"] == "等待澄清"
    assert profile["risk_level"] is None

    replay = asyncio.run(_collect(service, service.prepare_chat(
        "CUST000001", request_id=request_id, session_id=turn["session_id"], message="分析平安",
    )))
    assert replay[-1]["event"] == "clarification_required"

    with pytest.raises(AdvisorServiceError) as extra:
        service.prepare_clarify("CUST000001", request_id, {"symbols": "000001.SZ", "horizon": "short"})
    assert extra.value.status_code == 422
    with pytest.raises(AdvisorServiceError) as unknown:
        service.prepare_clarify("CUST000001", request_id, {"symbols": "999999.SH"})
    assert unknown.value.status_code == 422
    graph.mode = "done"
    prepared = service.prepare_clarify("CUST000001", request_id, {"symbols": "000001.SZ"})
    assert prepared.mode == "resume"
    assert prepared.resume_value == {"symbols": "000001.SZ"}
    from langgraph.types import Command
    asyncio.run(_collect(service, prepared))
    assert isinstance(graph.inputs[-1], Command)
    with service.store.transaction():
        profile = service.store.get_profile("CUST000001")
    assert profile["risk_level"] is None


def test_old_stock_clarification_checkpoint_becomes_retryable_version_failure():
    service, _graph, _saver = _service()
    request_id = "16161616-1616-1616-1616-161616161616"
    with service.store.transaction():
        session = service.store.insert_session("CUST000001", "旧股票中断")
        service.store.insert_turn(session["id"], request_id, "interrupted")
        service.store.update_turn(request_id, interrupt_payload={
            "kind": "clarification",
            "reason": "ambiguous_symbol",
            "fields": ["symbols"],
            "options": [{"id": "000001.SZ", "label": "平安银行"}],
        })

    with pytest.raises(AdvisorServiceError) as error:
        service.prepare_clarify("CUST000001", request_id, {"symbols": "000001.SZ"})

    assert error.value.status_code == 409
    assert error.value.code == "stock_agent_version_changed"
    with service.store.transaction():
        turn = service.store.find_turn(request_id)
    assert turn["status"] == "failed"
    assert turn["retryable"] is True
    assert turn["error_code"] == "stock_agent_version_changed"

def test_delete_session_removes_checkpoint_thread():
    service, _graph, saver = _service()
    events = _events(service, request_id="77777777-7777-7777-7777-777777777777", session_id=None, message="删除我")
    session_id = events[0]["data"]["session_id"]
    asyncio.run(service.delete_session("CUST000001", session_id))
    assert saver.deleted == [session_id]
    with pytest.raises(AdvisorServiceError):
        service.list_messages("CUST000001", session_id)


def test_unavailable_runtime_does_not_create_a_turn():
    service, graph, _saver = _service(ready=False)
    with pytest.raises(AdvisorServiceError) as exc:
        service.prepare_chat("CUST000001", request_id="88888888-8888-8888-8888-888888888888", session_id=None, message="你好")
    assert exc.value.status_code == 503
    assert service.store.turns == {}
    assert graph.inputs == []


def test_stale_running_turn_becomes_retryable():
    service, _graph, _saver = _service()
    with service.store.transaction():
        session = service.store.insert_session("CUST000001", "旧对话")
        service.store.insert_turn(session["id"], "99999999-9999-9999-9999-999999999999", "running")
        service.store.turns["99999999-9999-9999-9999-999999999999"]["updated_at"] = "2000-01-01T00:00:00+00:00"
        count = service.store.mark_stale_running(120)
        turn = service.store.find_turn("99999999-9999-9999-9999-999999999999")
    assert count == 1
    assert turn["status"] == "failed"
    assert turn["retryable"] is True


def test_resume_rejects_an_allocation_interrupt():
    service, _graph, _saver = _service()
    request_id = "15151515-1515-1515-1515-151515151515"
    with service.store.transaction():
        session = service.store.insert_session("CUST000001", "配置")
        service.store.insert_turn(session["id"], request_id, "interrupted")
        service.store.update_turn(request_id, interrupt_payload={
            "kind": "allocation",
            "plans": [{"plan_id": "p-light", "tier": "light"}],
        })

    with pytest.raises(AdvisorServiceError) as excinfo:
        service.prepare_resume("CUST000001", request_id, {"risk_level": "R3"})

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "not_interrupted"


def test_allocation_interrupt_is_labeled_waiting_for_a_plan():
    service, graph, _saver = _service()
    graph.mode = "interrupt"
    graph.interrupt_payload = {
        "kind": "allocation",
        "agent_version": "portfolio-allocation-v1",
        "plans": [{"plan_id": "p-light", "tier": "light", "items": []}],
    }
    request_id = "16161616-1616-1616-1616-161616161616"
    events = _events(service, request_id=request_id, session_id=None, message="帮我调仓")

    assert events[-1]["event"] == "allocation_required"
    with service.store.transaction():
        turn = service.store.find_turn(request_id)
    assert turn["stage"] == "等待选择方案"
    assert turn["status"] == "interrupted"


def test_portfolio_artifact_is_not_stripped_by_the_stock_projector():
    service, graph, _saver = _service()
    graph.agent_results = [{
        "agent": "portfolio_agent",
        "artifacts": [{
            "scenario": "allocation_adopted",
            "allocation_adopted": {
                "plan_id": "p-light",
                "tier": "light",
                "tier_label": "微调",
                "unit_nav": 1.02,
                "trade_count": 2,
                "weights": {"110011": 0.6},
            },
            "kline": [{"close": 1}],
        }],
    }]
    events = _events(
        service,
        request_id="17171717-1717-1717-1717-171717171718",
        session_id=None,
        message="采纳微调方案",
    )
    artifact = next(event["data"] for event in events if event["event"] == "artifact")
    assert artifact["schema_version"] == "portfolio-public-v1"
    assert artifact["allocation_adopted"]["plan_id"] == "p-light"
    assert artifact["allocation_adopted"]["unit_nav"] == 1.02
    assert "weights" not in artifact["allocation_adopted"]
    assert "kline" not in artifact


def test_artifact_event_is_emitted_before_token_and_restored_in_metadata():
    service, graph, _saver = _service()
    graph.agent_results = [{
        "agent": "stock_agent",
        "artifacts": [{
            "scenario": "market_overview",
            "summary": "指数平稳",
            "as_of": "2026-09-27T14:00:00+08:00",
            "tool_args": {"api_key": "secret"},
        }],
        "candidate_stocks": [{"thscode": "600519.SH", "name": "贵州茅台", "score": 88}],
        "warnings": ["stock_snapshot_stale_warning"],
    }]
    events = _events(
        service,
        request_id="14141414-1414-1414-1414-141414141414",
        session_id=None,
        message="今天大盘怎么样",
    )
    names = [event["event"] for event in events]
    assert "artifact" in names
    assert names.index("artifact") < names.index("token")
    artifact = next(event["data"] for event in events if event["event"] == "artifact")
    assert artifact["scenario"] == "market_overview"
    assert "tool_args" not in artifact
    assert "api_key" not in str(artifact)
    with service.store.transaction():
        sessions = service.store.list_sessions("CUST000001")
        messages = service.store.list_messages("CUST000001", sessions[0]["id"])
    assistant = next(item for item in messages if item["role"] == "assistant")
    metadata = assistant["public_metadata"]
    assert metadata["artifacts"][0]["scenario"] == "market_overview"
    assert metadata["candidate_stocks"][0]["thscode"] == "600519.SH"
    assert "warnings" in metadata
    assert "tool_args" not in str(metadata)


async def _collect(service: AgentService, prepared):
    return [event async for event in service.play("CUST000001", prepared)]
