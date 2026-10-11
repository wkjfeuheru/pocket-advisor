"""通过已启动服务的公开 HTTP/SSE 接口验收投顾，不装配任何模型或数据替身。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "evals/datasets/advisor_e2e.json"
RESULTS_DIR = ROOT / ".cache/evals/results"
TEST_PROFILE = {
    "risk_level": "R3", "investment_horizon": "长期", "investment_goal": "均衡增长",
    "liquidity_need": "中", "budget_amount": 100000,
}
INTERRUPTS = {"clarification_required", "profile_required", "allocation_required"}
PUBLIC_EVENTS = {"run", "status", "artifact", "warning", "token", "done", "error", *INTERRUPTS}
INTERNAL_CODE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", re.I)
PLACEHOLDER = re.compile(r"\{\{|\}\}|\[\[(?:FACT|SOURCE|EVIDENCE)[:|]|<fact(?:\s|:|>)", re.I)
DATA_LIMIT = re.compile(
    r"(?:数据|行情|指数|广度|来源|样本|净值|历史|指标|财务|记录).{0,16}(?:缺失|不足|不可用|受限|暂未|未提供|未返回|无法验证)"
    r"|(?:暂无|缺少|缺乏|未获取|无法获取|无法验证|没有可展示).{0,16}(?:数据|行情|指数|信息|记录|证据)"
    r"|上游.{0,16}(?:失败|不可用|受限|超时)|未能.{0,12}(?:获取|查询|验证)"
)
CONCLUSION = re.compile(r"整体|总体|综合|结论|震荡|偏强|偏弱|上涨|下跌|回落|反弹|走强|走弱|趋势|谨慎|观望")
CLARIFY = re.compile(r"请.{0,12}(?:补充|选择|确认|提供|明确)|需要.{0,12}(?:补充|选择|确认|提供)|你.{0,10}(?:希望|想看|指的是)|(?:哪只|哪个|什么策略|多长|投资期限)")


class ProtocolError(RuntimeError):
    """可公开的协议诊断；不包含响应正文、认证信息或 URL 查询参数。"""


class SSEDecoder:
    """按 SSE 帧解析 JSON，支持多行 data、心跳注释和 EOF 尾帧。"""

    def __init__(self) -> None:
        self.name = "message"
        self.data: list[str] = []

    def feed(self, line: str) -> list[dict[str, Any]]:
        if not line:
            return self._flush()
        if line.startswith(":"):
            return []
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self.name = value
        elif field == "data":
            self.data.append(value)
        return []

    def finish(self) -> list[dict[str, Any]]:
        return self._flush()

    def _flush(self) -> list[dict[str, Any]]:
        if not self.data:
            self.name = "message"
            return []
        name, raw = self.name, "\n".join(self.data)
        self.name, self.data = "message", []
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError("SSE JSON 解析失败") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("SSE data 必须是 JSON 对象")
        return [{"event": name, "data": payload}]


def _json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise ProtocolError(f"HTTP {response.status_code} {method} {path}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProtocolError(f"HTTP JSON 解析失败 {method} {path}") from exc


def stream_request(client: httpx.Client, path: str, body: dict[str, Any], observation: dict[str, Any], timeout: float = 180) -> None:
    """增量写入 observation，连接异常时仍保留已收到的原始帧和事件。"""
    decoder = SSEDecoder()
    deadline = time.monotonic() + timeout
    with client.stream("POST", path, json=body) as response:
        observation["http_status"] = response.status_code
        if response.status_code >= 400:
            raise ProtocolError(f"HTTP {response.status_code} POST {path}")
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            raise ProtocolError("响应类型不是 text/event-stream")
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise ProtocolError("SSE 总时长超过限制")
            observation["raw_sse_lines"].append(line)
            observation["events"].extend(decoder.feed(line))
        observation["events"].extend(decoder.finish())


def _events(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [item["data"] for item in events if item["event"] == name]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sourced_index(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    return bool(row.get("thscode") and row.get("source") and row.get("as_of") and _finite(row.get("last")))


def _warning_text(value: Any) -> list[str]:
    """公开 artifact 任意层的告警都是用户可见文本，不能藏内部码。"""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "warnings" and isinstance(child, list):
                found.extend(str(item) for item in child)
            else:
                found.extend(_warning_text(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_warning_text(child))
    return found


def evaluate_turn(
    events: list[dict[str, Any]], run: dict[str, Any], history: list[dict[str, Any]],
    expect: dict[str, Any], profile_before: dict[str, Any] | None = None,
    profile_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """硬契约优先；只有诚实说明数据限制且契约无误时才标记 data_limited。"""
    failures: list[str] = []
    limitations: list[str] = []
    names = [item["event"] for item in events]
    done = _events(events, "done")
    runs = _events(events, "run")
    artifacts = _events(events, "artifact")
    interrupts = [name for name in names if name in INTERRUPTS]
    answer = str(done[-1].get("answer") or "") if done else ""
    questions = "\n".join(str(item["data"].get("question") or "") for item in events if item["event"] in INTERRUPTS)
    warning_text = "\n".join(str(row.get("message") or "") for row in _events(events, "warning"))
    artifact_warnings = "\n".join(_warning_text(artifacts))
    public_text = "\n".join((answer, questions, warning_text, artifact_warnings))
    limited = bool(DATA_LIMIT.search(public_text))
    if not runs or not names or names[0] != "run":
        failures.append("missing_initial_run_event")
    if any(name not in PUBLIC_EVENTS for name in names):
        failures.append("unknown_public_event")
    if "error" in names:
        failures.append("stream_error_event")
    if any(PLACEHOLDER.search(str(item["data"].get("text") or "")) for item in events if item["event"] == "token") or PLACEHOLDER.search(public_text):
        failures.append("unresolved_placeholder")
    if INTERNAL_CODE.search(answer):
        failures.append("internal_answer_code")
    if INTERNAL_CODE.search(warning_text + "\n" + artifact_warnings):
        failures.append("internal_warning_code")
    if expect.get("profile_unchanged") and profile_before != profile_after:
        failures.append("verified_profile_changed")
    if runs:
        rid, sid = runs[0].get("request_id"), runs[0].get("session_id")
        if not rid or not sid or run.get("request_id") != rid or run.get("session_id") != sid:
            failures.append("run_identity_mismatch")
    outcome = expect.get("outcome", "completed")
    if interrupts:
        if done:
            failures.append("interrupt_and_done")
        if len(interrupts) != 1:
            failures.append("multiple_interrupts")
        if run.get("status") != "interrupted":
            failures.append("interrupt_not_persisted")
        accepted = {"clarification": {"clarification_required"}, "allocation_preview": {"allocation_required"}, "profile": {"profile_required"}}.get(outcome, set())
        if any(name not in accepted for name in interrupts):
            failures.extend(f"unexpected_interrupt:{name}" for name in interrupts if name not in accepted)
        if "allocation_required" in interrupts and outcome == "allocation_preview":
            plans = _events(events, "allocation_required")[0].get("plans")
            if not isinstance(plans, list) or not plans or not any(isinstance(plan, dict) and plan.get("plan_id") for plan in plans):
                failures.append("allocation_confirmation_has_no_plans")
        if any(item.get("role") == "assistant" and item.get("request_id") == run.get("request_id") for item in history):
            failures.append("interrupted_turn_has_completed_message")
    else:
        if len(done) != 1:
            failures.append("missing_or_duplicate_done")
        if run.get("status") != "completed":
            failures.append("run_not_completed")
        if not answer.strip():
            failures.append("empty_answer")
        if "".join(str(row.get("text") or "") for row in _events(events, "token")) != answer:
            failures.append("token_done_mismatch")
        if done and (done[-1].get("request_id") != run.get("request_id") or done[-1].get("session_id") != run.get("session_id")):
            failures.append("done_identity_mismatch")
        if run.get("final_answer") != answer:
            failures.append("run_answer_mismatch")
        saved = [item for item in history if item.get("role") == "assistant" and item.get("request_id") == run.get("request_id")]
        if len(saved) != 1 or saved[0].get("content") != answer:
            failures.append("history_answer_mismatch")
        if outcome == "clarification" and not CLARIFY.search(answer):
            failures.append("missing_clarification_question")
        if outcome == "allocation_preview":
            failures.append("missing_allocation_confirmation")
    if "allocation_required" in names and expect.get("no_allocation"):
        failures.append("diagnosis_generated_allocation")
    if expect.get("no_allocation") and any(a.get("scenario") == "allocation_adopted" for a in artifacts):
        failures.append("unexpected_allocation_adoption")
    if any(a.get("scenario") == "allocation_adopted" for a in artifacts):
        failures.append("runner_never_authorized_adoption")
    if not interrupts:
        scenario = expect.get("scenario")
        matching = [artifact for artifact in artifacts if artifact.get("scenario") == scenario] if scenario else []
        if scenario and not matching:
            failures.append(f"missing_expected_artifact:{scenario}")
        indices = [row for artifact in matching for row in (artifact.get("market_overview") or {}).get("indices") or []]
        if expect.get("verifiable_indices"):
            if not indices:
                (limitations if limited and matching else failures).append("missing_index_data")
            elif any(not _sourced_index(row) for row in indices):
                failures.append("unverifiable_index")
        expected_indices = set(expect.get("index_codes") or [])
        observed_indices = {row.get("thscode") for row in indices if isinstance(row, dict)}
        if expected_indices and not expected_indices.issubset(observed_indices):
            (limitations if limited and matching else failures).append("missing_requested_index")
        expected_symbols = set(expect.get("symbols") or [])
        equities = [row for artifact in matching for row in artifact.get("equity_research") or []]
        symbols = {row.get("thscode") for row in equities if isinstance(row, dict)}
        if expected_symbols and not expected_symbols.issubset(symbols):
            (limitations if limited and matching else failures).append("missing_requested_symbol")
        forbidden_symbols = set(expect.get("forbidden_symbols") or [])
        if forbidden_symbols.intersection(symbols):
            failures.append("previous_symbol_leaked")
        if expect.get("conclusion") and not CONCLUSION.search(answer):
            (limitations if limited else failures).append("missing_market_conclusion")
        if expect.get("breadth_degradation") and matching:
            blocks = [a.get("market_overview") or {} for a in matching]
            breadth_present = any(_finite((b.get("breadth") or {}).get("up")) and _finite((b.get("breadth") or {}).get("down")) for b in blocks)
            if not breadth_present:
                if not re.search(r"(?:广度|涨跌家数|上涨家数|市场情绪|市场宽度).{0,20}(?:缺失|不可用|缺少|未|无法|不足)|(?:缺少|暂无|无法).{0,20}(?:广度|涨跌家数|市场情绪)", public_text):
                    failures.append("missing_breadth_not_disclosed")
                else:
                    limitations.append("breadth_data_unavailable")
        if expect.get("metric") and matching:
            metric = expect["metric"]
            values = [(row.get("fundamentals") or {}).get(metric) for row in equities if isinstance(row, dict)]
            if not any(_finite(value) for value in values):
                (limitations if limited else failures).append(f"missing_requested_metric:{metric}")
    observed_answer = answer + "\n" + questions
    for pattern in expect.get("answer_all") or []:
        if not re.search(pattern, observed_answer, re.I):
            (limitations if limited and not interrupts and expect.get("allow_data_limit", True) else failures).append(f"missing_answer_pattern:{pattern}")
    if expect.get("answer_any") and not any(re.search(pattern, observed_answer, re.I) for pattern in expect["answer_any"]):
        (limitations if limited and not interrupts and expect.get("allow_data_limit", True) else failures).append("missing_answer_topic")
    for pattern in expect.get("forbidden_answer") or []:
        if re.search(pattern, observed_answer, re.I):
            failures.append(f"forbidden_answer_pattern:{pattern}")
    if limited and not interrupts:
        limitations.append("public_answer_discloses_data_limit")
    return {
        "status": "hard_failure" if failures else "data_limited" if limitations else "passed",
        "hard_failures": list(dict.fromkeys(failures)), "data_limitations": list(dict.fromkeys(limitations)),
        "answer": answer, "event_counts": dict(Counter(names)),
    }


def run_case(client: httpx.Client, case: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
    result: dict[str, Any] = {"case_id": case["case_id"], "title": case.get("title", ""), "turns": []}
    session_id = None
    previous: dict[str, Any] | None = None
    for index, step in enumerate(case["steps"]):
        started = time.monotonic()
        observation: dict[str, Any] = {
            "step": index + 1, "message": step["message"], "expect": step.get("expect", {}),
            "events": [], "raw_sse_lines": [], "run": {}, "history": [],
        }
        result["turns"].append(observation)
        try:
            before = _json(client, "GET", "/api/agents/profile")
            identity_before = _json(client, "GET", "/api/me") if step.get("expect", {}).get("identity_unchanged") else None
            path = "/api/agents/chat/stream"
            request_id = str(uuid4())
            body: dict[str, Any] = {"request_id": request_id, "message": step["message"]}
            if session_id:
                body["session_id"] = session_id
            if previous:
                interrupt = next((e for e in previous["events"] if e["event"] in INTERRUPTS), None)
                if interrupt:
                    # 只有数据集明确给出的澄清回答才能恢复；画像和方案中断不自动确认。
                    if interrupt["event"] != "clarification_required" or not step.get("clarify_answers"):
                        raise ProtocolError("前轮等待确认，未获数据集明确授权恢复")
                    request_id = previous["run"]["request_id"]
                    path = f"/api/agents/runs/{request_id}/clarify/stream"
                    declared = set(interrupt["data"].get("fields") or [])
                    answers = {key: value for key, value in step["clarify_answers"].items() if key in declared}
                    if not answers:
                        raise ProtocolError("澄清回答与公开待确认字段不匹配")
                    body = {"answers": answers}
            observation["request_id"] = request_id
            stream_request(client, path, body, observation, timeout)
            run_events = _events(observation["events"], "run")
            if run_events:
                received_session = run_events[0].get("session_id")
                if session_id and received_session != session_id:
                    raise ProtocolError("同一 case 的 session_id 发生变化")
                session_id = received_session
                if run_events[0].get("request_id") != request_id:
                    raise ProtocolError("SSE request_id 与提交值不符")
            observation["run"] = _json(client, "GET", f"/api/agents/runs/{request_id}")
            if session_id:
                observation["history"] = _json(client, "GET", f"/api/agents/sessions/{session_id}/messages")
            after = _json(client, "GET", "/api/agents/profile")
            observation["profile_before"], observation["profile_after"] = before, after
            observation.update(evaluate_turn(observation["events"], observation["run"], observation["history"], step.get("expect", {}), before, after))
            if identity_before is not None:
                identity_after = _json(client, "GET", "/api/me")
                observation["identity_before"], observation["identity_after"] = identity_before, identity_after
                if identity_before != identity_after:
                    observation["hard_failures"].append("authenticated_identity_changed")
                    observation["status"] = "hard_failure"
        except Exception as exc:
            # 第三方异常正文可能包含 token、连接串或响应，不输出 str(exc)。
            observation.update({"status": "hard_failure", "hard_failures": [str(exc) if isinstance(exc, ProtocolError) else f"transport_or_runner_error:{type(exc).__name__}"], "data_limitations": []})
            done = _events(observation["events"], "done")
            observation["answer"] = str(done[-1].get("answer") or "") if done else ""
        observation["elapsed_seconds"] = round(time.monotonic() - started, 3)
        previous = observation
        if observation["status"] == "hard_failure":
            result["remaining_steps_skipped"] = len(case["steps"]) - index - 1
            break
    result["session_id"] = session_id
    statuses = [turn["status"] for turn in result["turns"]]
    result["status"] = "hard_failure" if "hard_failure" in statuses else "data_limited" if "data_limited" in statuses else "passed"
    return result


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload["cases"]
    if not cases or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("问题集为空或 case_id 重复")
    for case in cases:
        if not case.get("steps"):
            raise ValueError(f"case 没有轮次: {case['case_id']}")
        for step in case["steps"]:
            if step.get("action", "chat") != "chat":
                raise ValueError("runner 只允许聊天和显式澄清，禁止方案采纳")
    return cases


def select_cases(cases: list[dict[str, Any]], selections: list[str], limit: int | None) -> list[dict[str, Any]]:
    wanted = {part.strip() for selection in selections for part in selection.split(",") if part.strip()}
    unknown = wanted - {case["case_id"] for case in cases}
    if unknown:
        raise ValueError("unknown case: " + ", ".join(sorted(unknown)))
    chosen = [case for case in cases if case["case_id"] in wanted] if wanted else [case for case in cases if case.get("enabled_by_default", True)]
    if limit is not None and limit < 1:
        raise ValueError("--limit 必须为正整数")
    return chosen[:limit] if limit else chosen


def authenticate(client: httpx.Client, token_env: str | None) -> dict[str, Any]:
    if token_env:
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise ProtocolError("指定的 token 环境变量为空")
        client.headers["Authorization"] = f"Bearer {token}"
        profile = _json(client, "GET", "/api/agents/profile")
        return {"mode": "existing_token", "profile": profile, "profile_mutated": False}
    username = "e2e_" + uuid4().hex[:20]
    password = secrets.token_urlsafe(30)
    _json(client, "POST", "/api/register", json={"username": username, "password": password, "display_name": "端到端评测专用"})
    login = _json(client, "POST", "/api/login", json={"username": username, "password": password})
    if not isinstance(login.get("token"), str) or not login["token"]:
        raise ProtocolError("登录成功响应缺少 token")
    client.headers["Authorization"] = f"Bearer {login['token']}"
    profile = _json(client, "PATCH", "/api/agents/profile", json=TEST_PROFILE)
    if any(profile.get(key) != value for key, value in TEST_PROFILE.items()):
        raise ProtocolError("测试用户画像未按公开接口保存")
    return {"mode": "isolated_registered_user", "username": username, "profile": profile, "profile_mutated": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 HTTP/SSE 投顾端到端评测；需预先启动服务。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case", action="append", default=[], help="case_id，可重复或逗号分隔")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--token-env", help="已有普通用户 token 的环境变量名；不改该用户画像")
    parser.add_argument("--timeout", type=float, default=180, help="每轮流总时长及读取超时秒数")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, help="结果 JSON 路径；默认 .cache/evals/results/advisor-e2e*.json")
    parser.add_argument("--list", action="store_true", help="仅列出问题，不发 HTTP 请求")
    args = parser.parse_args(argv)
    try:
        url = httpx.URL(args.base_url)
        if url.scheme not in {"http", "https"} or url.userinfo or url.query or url.fragment:
            raise ValueError("--base-url 须为不含认证信息、查询串或片段的 HTTP(S) 地址")
        if args.timeout <= 0:
            raise ValueError("--timeout 必须为正数")
        cases = select_cases(load_cases(args.dataset), args.case, args.limit)
    except (ValueError, OSError, KeyError) as exc:
        print(f"参数或问题集错误: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.list:
        for case in cases:
            print(f"{case['case_id']}: {case.get('title', '')}")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or RESULTS_DIR / f"advisor-e2e-{stamp}-{uuid4().hex[:8]}.json"
    report: dict[str, Any] = {
        "schema_version": "advisor-e2e-v1", "mode": "real_http_sse", "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": str(url), "dataset": str(args.dataset), "timeout_seconds": args.timeout, "cases": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    timeout = httpx.Timeout(args.timeout, connect=min(10, args.timeout), write=min(30, args.timeout), pool=min(10, args.timeout))
    try:
        with httpx.Client(base_url=str(url).rstrip("/"), timeout=timeout, trust_env=False) as client:
            report["account"] = authenticate(client, args.token_env)
            report["health"] = _json(client, "GET", "/api/agents/health")
            if report["health"].get("status") != "ready":
                raise ProtocolError("投顾服务未就绪")
            for case in cases:
                result = run_case(client, case, args.timeout)
                report["cases"].append(result)
                print(f"{case['case_id']}: {result['status']}", flush=True)
                save()
    except KeyboardInterrupt:
        report["setup_error"] = "interrupted_by_operator"
        print("评测已中断，已保留完成的结果。", file=sys.stderr)
    except Exception as exc:
        report["setup_error"] = str(exc) if isinstance(exc, ProtocolError) else f"setup_failure:{type(exc).__name__}"
        print("评测前置或连接失败，诊断已写入结果文件。", file=sys.stderr)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = dict(Counter(case["status"] for case in report["cases"]))
    report["all_cases_executed"] = len(report["cases"]) == len(cases) and "setup_error" not in report
    save()
    print(f"结果: {output}")
    if "setup_error" in report:
        return 2
    if report["summary"].get("hard_failure"):
        return 1
    # 数据受限不能视为全通过，单独退出码方便 CI 和人工验收辨认。
    return 3 if report["summary"].get("data_limited") else 0


if __name__ == "__main__":
    raise SystemExit(main())
