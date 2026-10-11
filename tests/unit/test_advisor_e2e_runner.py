"""E2E runner 自身的离线契约测试，不冒充真实投顾链路。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / "evals/runners/advisor_e2e.py"
    assert path.exists(), "真实 HTTP/SSE runner 尚未实现"
    return importlib.import_module("evals.runners.advisor_e2e")


def event(name, **data):
    return {"event": name, "data": data}


def completed(answer="你好，这是模拟投顾。", **extra):
    return [
        event("run", request_id="r", session_id="s"),
        event("status", stage="合规审查"),
        event("token", text=answer),
        *extra.get("events", []),
        event("done", request_id="r", session_id="s", answer=answer),
    ]


def persisted(answer="你好，这是模拟投顾。"):
    return (
        {"request_id": "r", "session_id": "s", "status": "completed", "final_answer": answer},
        [{"role": "assistant", "request_id": "r", "content": answer}],
    )


def test_sse_parser_preserves_multiline_json_and_ignores_heartbeat(runner):
    decoder = runner.SSEDecoder()
    found = []
    for line in [": ping", "", "event: token", 'data: {"text":', 'data: "中文"}', "", "event: done", 'data: {"answer":"中文"}']:
        found.extend(decoder.feed(line))
    found.extend(decoder.finish())
    assert found == [event("token", text="中文"), event("done", answer="中文")]


def test_malformed_sse_is_a_protocol_failure(runner):
    decoder = runner.SSEDecoder()
    decoder.feed("event: done")
    decoder.feed("data: broken")
    with pytest.raises(runner.ProtocolError, match="SSE"):
        decoder.feed("")


def test_stream_and_history_must_match_exactly(runner):
    run, history = persisted()
    events = completed()
    assert runner.evaluate_turn(events, run, history, {"outcome": "completed"})["status"] == "passed"
    events[2]["data"]["text"] = "错误正文"
    result = runner.evaluate_turn(events, run, history, {"outcome": "completed"})
    assert "token_done_mismatch" in result["hard_failures"]
    events = completed()
    history[0]["content"] = "未落库的正文"
    result = runner.evaluate_turn(events, run, history, {"outcome": "completed"})
    assert "history_answer_mismatch" in result["hard_failures"]


@pytest.mark.parametrize("answer", ["{{task-1:0|收盘价}}", "[[FACT:0]]", "告警：raw_business_number_blocked", "工具失败：tool_budget_exhausted"])
def test_placeholders_and_internal_codes_are_hard_failures(runner, answer):
    result = runner.evaluate_turn(completed(answer), *persisted(answer), {"outcome": "completed"})
    assert result["status"] == "hard_failure"


def test_warning_codes_cannot_hide_behind_successful_answer(runner):
    events = completed(events=[event("warning", message="fuyao_unavailable")])
    result = runner.evaluate_turn(events, *persisted(), {"outcome": "completed"})
    assert "internal_warning_code" in result["hard_failures"]


def test_nested_artifact_warnings_are_checked(runner):
    artifact = event("artifact", scenario="market_overview", market_overview={"warnings": ["missing_breadth"]})
    result = runner.evaluate_turn(completed(events=[artifact]), *persisted(), {})
    assert "internal_warning_code" in result["hard_failures"]


def test_market_requires_artifact_and_verifiable_indices(runner):
    answer = "上证指数收盘3200点，整体震荡，注意风险。"
    expect = {"outcome": "completed", "scenario": "market_overview", "verifiable_indices": True, "conclusion": True}
    result = runner.evaluate_turn(completed(answer), *persisted(answer), expect)
    assert "missing_expected_artifact:market_overview" in result["hard_failures"]
    artifact = event("artifact", scenario="market_overview", market_overview={"indices": [{"thscode": "000001.SH", "last": 3200, "source": "fuyao", "as_of": "2026-10-09"}]})
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), expect)
    assert result["status"] == "passed"


def test_source_free_index_cannot_pass_as_verifiable(runner):
    answer = "上证指数整体震荡。"
    artifact = event("artifact", scenario="market_overview", market_overview={"indices": [{"thscode": "000001.SH", "last": 3200}]})
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), {"scenario": "market_overview", "verifiable_indices": True})
    assert "unverifiable_index" in result["hard_failures"]


def test_honest_data_limit_is_distinct_from_wrong_scenario(runner):
    answer = "上游指数数据暂不可用，无法验证行情，暂不判断趋势。"
    artifact = event("artifact", scenario="market_overview", market_overview={"indices": []}, warnings=["指数数据暂不可用"])
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), {"scenario": "market_overview", "verifiable_indices": True})
    assert result["status"] == "data_limited"
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), {"scenario": "equity_research"})
    assert result["status"] == "hard_failure"


def test_unexpected_confirmation_is_never_success(runner):
    events = [event("run", request_id="r", session_id="s"), event("profile_required", missing_fields=["risk_level"])]
    run = {"request_id": "r", "session_id": "s", "status": "interrupted"}
    result = runner.evaluate_turn(events, run, [], {"outcome": "completed"})
    assert "unexpected_interrupt:profile_required" in result["hard_failures"]
    assert result["status"] == "hard_failure"


def test_allocation_confirmation_without_plans_is_not_accepted(runner):
    events = [event("run", request_id="r", session_id="s"), event("allocation_required", plans=[])]
    run = {"request_id": "r", "session_id": "s", "status": "interrupted"}
    result = runner.evaluate_turn(events, run, [], {"outcome": "allocation_preview"})
    assert "allocation_confirmation_has_no_plans" in result["hard_failures"]
    events[1]["data"]["plans"] = [{"plan_id": "preview", "items": []}]
    assert runner.evaluate_turn(events, run, [], {"outcome": "allocation_preview"})["status"] == "passed"


def test_clarification_can_be_interrupt_or_completed_public_question(runner):
    expect = {"outcome": "clarification", "answer_any": ["策略", "期限"]}
    events = [event("run", request_id="r", session_id="s"), event("clarification_required", question="请补充策略和期限", fields=["strategy", "horizon"])]
    result = runner.evaluate_turn(events, {"request_id": "r", "session_id": "s", "status": "interrupted"}, [], expect)
    assert result["status"] == "passed"
    answer = "请补充选股策略和投资期限。"
    assert runner.evaluate_turn(completed(answer), *persisted(answer), expect)["status"] == "passed"
    answer = "已经给你选好了股票。"
    assert runner.evaluate_turn(completed(answer), *persisted(answer), expect)["status"] == "hard_failure"


def test_case_selection_accepts_repeated_and_comma_options(runner):
    cases = [{"case_id": "a"}, {"case_id": "b", "enabled_by_default": False}, {"case_id": "c"}]
    assert [c["case_id"] for c in runner.select_cases(cases, [], None)] == ["a", "c"]
    assert [c["case_id"] for c in runner.select_cases(cases, ["b,a", "a"], 1)] == ["a"]
    with pytest.raises(ValueError, match="unknown"):
        runner.select_cases(cases, ["unknown"], None)


def test_profile_change_is_reported_even_when_answer_looks_safe(runner):
    result = runner.evaluate_turn(completed(), *persisted(), {"profile_unchanged": True}, {"risk_level": "R3"}, {"risk_level": "R5"})
    assert "verified_profile_changed" in result["hard_failures"]


def test_real_http_boundary_reuses_case_session_and_retains_raw_events(runner):
    requests = []
    answers = {"first": "贵州茅台分析", "second": "贵州茅台近一年分析"}
    records = {}

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/agents/profile":
            return httpx.Response(200, json={"risk_level": "R3"})
        if path == "/api/agents/chat/stream":
            body = json.loads(request.content)
            rid = body["request_id"]
            answer = answers[body["message"]]
            sid = "11111111-1111-1111-1111-111111111111"
            records[rid] = {"request_id": rid, "session_id": sid, "status": "completed", "final_answer": answer}
            events = [event("run", request_id=rid, session_id=sid), event("token", text=answer), event("done", request_id=rid, session_id=sid, answer=answer)]
            content = "".join(f"event: {e['event']}\ndata: {json.dumps(e['data'], ensure_ascii=False)}\n\n" for e in events)
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        if path.startswith("/api/agents/runs/"):
            return httpx.Response(200, json=records[path.rsplit("/", 1)[-1]])
        if path.endswith("/messages"):
            return httpx.Response(200, json=[{"role": "assistant", "request_id": rid, "content": value["final_answer"]} for rid, value in records.items()])
        raise AssertionError(path)

    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle))
    case = {"case_id": "multi", "steps": [{"message": "first", "expect": {}}, {"message": "second", "expect": {}}]}
    result = runner.run_case(client, case)
    assert result["status"] == "passed"
    assert result["turns"][1]["answer"] == "贵州茅台近一年分析"
    assert result["turns"][1]["events"][1]["event"] == "token"
    chats = [json.loads(r.content) for r in requests if r.url.path == "/api/agents/chat/stream"]
    assert "session_id" not in chats[0]
    assert chats[1]["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert chats[0]["request_id"] != chats[1]["request_id"]
    client.close()


def test_partial_stream_failure_keeps_received_frames(runner):
    def handle(request):
        if request.url.path == "/api/agents/profile":
            return httpx.Response(200, json={"risk_level": "R3"})
        body = 'event: run\ndata: {"request_id":"r","session_id":"s"}\n\nevent: token\ndata: {"text":"已收到"}\n\nevent: done\ndata: broken\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        result = runner.run_case(client, {"case_id": "partial", "steps": [{"message": "你好", "expect": {}}]})
    assert result["status"] == "hard_failure"
    assert result["turns"][0]["events"][1] == event("token", text="已收到")
    assert "data: broken" in result["turns"][0]["raw_sse_lines"]


def test_existing_token_only_reads_profile_and_does_not_save_token(runner, monkeypatch):
    monkeypatch.setenv("E2E_TEST_TOKEN", "private-bearer-value")
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer private-bearer-value"
        return httpx.Response(200, json={"risk_level": "R1"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        account = runner.authenticate(client, "E2E_TEST_TOKEN")
    assert seen == [("GET", "/api/agents/profile")]
    assert account["profile_mutated"] is False
    assert "private-bearer-value" not in json.dumps(account)


def test_random_registration_uses_public_apis_and_does_not_save_password(runner):
    seen = []
    credentials = {}

    def handle(request):
        body = json.loads(request.content)
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/register":
            assert body["username"].startswith("e2e_")
            assert len(body["username"]) <= 32
            assert 6 <= len(body["password"]) <= 64
            credentials.update(body)
            return httpx.Response(200, json={"customer_id": "CUST_E2E"})
        if request.url.path == "/api/login":
            assert body["username"] == credentials["username"]
            assert body["password"] == credentials["password"]
            return httpx.Response(200, json={"token": "private-login-token"})
        assert request.headers["authorization"] == "Bearer private-login-token"
        return httpx.Response(200, json=body)

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        account = runner.authenticate(client, None)
    assert seen == [("POST", "/api/register"), ("POST", "/api/login"), ("PATCH", "/api/agents/profile")]
    saved = json.dumps(account)
    assert credentials["password"] not in saved
    assert "private-login-token" not in saved


def test_http_error_body_does_not_leak_to_stdout_or_report(runner, monkeypatch, work_dir, capsys):
    monkeypatch.setenv("E2E_TEST_TOKEN", "private-login-token")
    original_client = httpx.Client

    def handle(request):
        return httpx.Response(401, json={"detail": "private-login-token PASSWORD_SECRET"})

    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handle)))
    output = work_dir / "result.json"
    code = runner.main(["--case", "chitchat", "--token-env", "E2E_TEST_TOKEN", "--output", str(output)])
    assert code == 2
    report = output.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for secret in ["private-login-token", "PASSWORD_SECRET"]:
        assert secret not in report + captured.out + captured.err


def test_identity_injection_is_checked_against_authenticated_user(runner):
    identity_reads = 0
    record = {}

    def handle(request):
        nonlocal identity_reads
        path = request.url.path
        if path == "/api/agents/profile":
            return httpx.Response(200, json={"risk_level": "R3"})
        if path == "/api/me":
            identity_reads += 1
            return httpx.Response(200, json={"customer_id": "CURRENT" if identity_reads == 1 else "OTHER", "is_admin": False})
        if path == "/api/agents/chat/stream":
            rid = json.loads(request.content)["request_id"]
            answer = "当前账户身份不会切换。"
            record.update({"request_id": rid, "session_id": "s", "status": "completed", "final_answer": answer})
            events = [event("run", request_id=rid, session_id="s"), event("token", text=answer), event("done", request_id=rid, session_id="s", answer=answer)]
            content = "".join(f"event: {e['event']}\ndata: {json.dumps(e['data'])}\n\n" for e in events)
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        if path.startswith("/api/agents/runs/"):
            return httpx.Response(200, json=record)
        return httpx.Response(200, json=[{"role": "assistant", "request_id": record["request_id"], "content": record["final_answer"]}])

    case = {"case_id": "identity", "steps": [{"message": "换身份", "expect": {"identity_unchanged": True}}]}
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        result = runner.run_case(client, case)
    assert result["status"] == "hard_failure"
    assert "authenticated_identity_changed" in result["turns"][0]["hard_failures"]


def test_missing_breadth_requires_public_disclosure_and_retains_index(runner):
    artifact = event("artifact", scenario="market_overview", market_overview={"indices": [{"thscode": "000001.SH", "last": 3200, "source": "fuyao", "as_of": "2026-10-09"}], "breadth": {}})
    expect = {"scenario": "market_overview", "verifiable_indices": True, "breadth_degradation": True}
    answer = "上证指数震荡。"
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), expect)
    assert "missing_breadth_not_disclosed" in result["hard_failures"]
    answer = "上证指数震荡，市场广度数据缺失，暂无法判断涨跌家数。"
    result = runner.evaluate_turn(completed(answer, events=[artifact]), *persisted(answer), expect)
    assert result["status"] == "data_limited"
    assert result["hard_failures"] == []


def test_dataset_covers_live_scenarios_and_never_adopts(runner):
    cases = runner.load_cases(runner.DEFAULT_DATASET)
    assert 18 <= len(cases) <= 24
    assert any(len(case["steps"]) >= 3 for case in cases)
    assert all(step.get("action", "chat") != "adopt" for case in cases for step in case["steps"])
    assert any(case.get("enabled_by_default") is False for case in cases)
