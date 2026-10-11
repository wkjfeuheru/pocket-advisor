"""投顾轮次：幂等、画像确认、checkpoint 恢复与 SSE 事件。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import Any, AsyncIterator
from uuid import UUID

from anyio import to_thread
from langgraph.types import Command

from app.agents.artifacts import public_artifacts
from app.agents.graphs.state import GRAPH_VERSION, RESULT_KEY, WorkerBase, WorkerOutcome, checkpoint_thread_id
from app.agents.graphs.task_agent import (
    STAGE_LABELS,
    AllBranchesFailed,
)
from app.agents.portfolio.branch import ALLOCATION_WAITING_STAGE
from app.agents.portfolio.contracts import (
    AllocationInterruptError,
    validate_allocation_resume,
)
from app.agents.runtime import get_agent_runtime
from app.utils.sanitize import sanitize_text
from app.agents.tools.database import bind_readonly_services, reset_readonly_services
from app.core.security import UniqueConstraintError
from app.core.settings_models import load_advisor_settings
from app.crud.agent_session_crud import conversation_title, get_advisor_store

logger = logging.getLogger(__name__)

_PROFILE_ENUMS = {
    "risk_level": {"R1", "R2", "R3", "R4", "R5"},
    "investment_horizon": {"短期", "中期", "长期"},
    "investment_goal": {"保本", "收益", "均衡增长", "成长"},
    "liquidity_need": {"高", "中", "低"},
}


def _classify_failure(exc: BaseException) -> tuple[str, bool]:
    """把图执行异常映射成 (脱敏错误码, 是否可重试)。

    原则：输入/业务/数据类错误是确定性的，重试无意义；网络/限流/超时等瞬时故障才可重试。
    """
    if isinstance(exc, AllBranchesFailed):
        return "all_branches_failed", True
    # ValueError 族：校验、计划、候选、权重等业务错误——输入不变重试结果也不变。
    if isinstance(exc, ValueError):
        return "invalid_input", False
    # 数据缺失：重试不会凭空产生数据。
    if isinstance(exc, LookupError):
        return "not_found", False
    # 编程错误：重试同样会触发，应修代码而非让用户重试。
    if isinstance(exc, (TypeError, AttributeError)):
        return "internal_error", False
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return "timeout", True
    if "429" in text or ("rate" in text and "limit" in text):
        return "rate_limited", True
    if "connection" in name or "connection" in text or "reset by peer" in text:
        return "connection_error", True
    return "turn_failed", True


class AdvisorServiceError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


@dataclass
class PreparedTurn:
    mode: str
    session_id: str
    request_id: str
    turn: dict[str, Any]
    message: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    resume_value: dict[str, Any] | None = None


def validate_profile_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """只接受用户明确提交的画像字段。"""
    if not isinstance(patch, dict) or not patch:
        raise AdvisorServiceError(400, "请提交画像字段", code="invalid_profile")
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        if key in _PROFILE_ENUMS:
            if value not in _PROFILE_ENUMS[key]:
                raise AdvisorServiceError(400, "画像字段无效", code="invalid_profile")
            cleaned[key] = value
        elif key == "budget_amount":
            try:
                amount = float(value)
            except (TypeError, ValueError) as exc:
                raise AdvisorServiceError(400, "预算必须大于 0", code="invalid_profile") from exc
            if amount <= 0:
                raise AdvisorServiceError(400, "预算必须大于 0", code="invalid_profile")
            cleaned[key] = amount
        else:
            raise AdvisorServiceError(400, "画像字段无效", code="invalid_profile")
    return cleaned


def validate_clarify_answers(payload: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """只接受服务端声明的字段和候选值。"""
    if not isinstance(answers, dict) or not answers:
        raise AdvisorServiceError(422, "请提交澄清字段", code="invalid_clarification")
    fields = {str(item) for item in payload.get("fields") or []}
    extra = [key for key in answers if key not in fields]
    if extra:
        raise AdvisorServiceError(422, "只能提交本轮声明的字段", code="invalid_clarification")
    missing = [field for field in fields if answers.get(field) in (None, "")]
    if missing:
        raise AdvisorServiceError(400, "请补全澄清字段", code="clarification_incomplete")
    tagged: dict[str, set[str]] = {}
    shared: set[str] = set()
    for item in payload.get("options") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        option_id = str(item["id"])
        field = str(item.get("field") or "")
        if field:
            tagged.setdefault(field, set()).add(option_id)
        else:
            shared.add(option_id)
    cleaned: dict[str, Any] = {}
    for key, value in answers.items():
        values = value if isinstance(value, list) else [value]
        allowed = tagged.get(str(key), set()) or shared
        if allowed:
            for item in values:
                if str(item) not in allowed:
                    raise AdvisorServiceError(422, "请从候选中选择", code="invalid_clarification")
        cleaned[key] = value
    return cleaned


def public_interrupt_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if payload.get("kind") == "allocation":
        # 复用 contracts 的脱敏逻辑，避免这里再维护一份"哪些字段能出网"的清单。
        from app.agents.portfolio.contracts import public_allocation_payload

        return public_allocation_payload(payload)
    if payload.get("kind") == "clarification":
        options = []
        for item in payload.get("options") or []:
            if isinstance(item, dict):
                packed = {"id": str(item.get("id") or ""), "label": str(item.get("label") or "")}
                if item.get("field"):
                    packed["field"] = str(item["field"])
                options.append(packed)
        return "clarification_required", {
            "kind": "clarification",
            "agent_version": str(payload.get("agent_version") or ""),
            "reason": str(payload.get("reason") or ""),
            "question": str(payload.get("question") or ""),
            "fields": [str(item) for item in payload.get("fields") or []],
            "options": options,
            "request": dict(payload.get("request") or {}),
        }
    return "profile_required", {
        "kind": "profile",
        "missing_fields": list(payload.get("missing_fields") or []),
        "profile": dict(payload.get("profile") or {}),
    }


class AgentService:
    def __init__(self, store: Any | None = None, runtime_getter: Any | None = None) -> None:
        self.store = store or get_advisor_store()
        self._runtime_getter = runtime_getter or get_agent_runtime

    def stream_turn(self, customer_id: str, request: Any) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_chat(
            customer_id,
            request_id=str(request.request_id),
            session_id=str(request.session_id) if request.session_id else None,
            message=request.message,
        )
        return self.play(customer_id, prepared)

    def resume_turn(
        self,
        customer_id: str,
        request_id: str,
        profile_patch: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_resume(customer_id, request_id, profile_patch)
        return self.play(customer_id, prepared)

    def retry_turn(self, customer_id: str, request_id: str) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_retry(customer_id, request_id)
        return self.play(customer_id, prepared)

    def clarify_turn(
        self,
        customer_id: str,
        request_id: str,
        answers: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_clarify(customer_id, request_id, answers)
        return self.play(customer_id, prepared)

    def adopt_allocation_turn(
        self,
        customer_id: str,
        request_id: str,
        plan_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        prepared = self.prepare_allocation_adopt(customer_id, request_id, {"plan_id": plan_id})
        return self.play(customer_id, prepared)

    def prepare_chat(
        self,
        customer_id: str,
        *,
        request_id: str,
        session_id: str | None,
        message: str,
    ) -> PreparedTurn:
        customer_id = customer_id.upper()
        text = (message or "").strip()
        limit = load_advisor_settings().max_message_chars
        if not text:
            raise AdvisorServiceError(400, "请输入内容", code="empty_message")
        if len(text) > limit:
            raise AdvisorServiceError(400, f"消息不能超过 {limit} 个字符", code="message_too_long")
        try:
            with self.store.transaction():
                existing = self._owned_turn(customer_id, request_id)
                if existing is not None:
                    return self._existing_turn(existing)
            self._require_ready()
            with self.store.transaction():
                existing = self._owned_turn(customer_id, request_id)
                if existing is not None:
                    return self._existing_turn(existing)
                if session_id:
                    parent = self.store.get_session(customer_id, session_id)
                    if parent is None:
                        raise AdvisorServiceError(404, "会话不存在", code="session_not_found")
                    self.store.lock_session(session_id)
                    for old_turn in self.store.list_turns(session_id):
                        self._invalidate_legacy(old_turn)
                    active = self.store.active_turn(session_id)
                    if active is not None:
                        raise AdvisorServiceError(409, "会话已有进行中的轮次", code="active_turn")
                    target = parent
                else:
                    target = self.store.insert_session(customer_id, conversation_title(text))
                self.store.insert_message(target["id"], request_id, "user", text, {})
                turn = self.store.insert_turn(target["id"], request_id, "pending", graph_version=GRAPH_VERSION)
                turn = self.store.update_turn(request_id, status="running")
                profile = self.store.get_profile(customer_id)
        except UniqueConstraintError as exc:
            # 唯一约束可能来自两种并发：同一 request_id 重复提交（PK/消息唯一索引），
            # 或会话已存在活动轮次。前者应按幂等重放处理，不能再报"会话繁忙"。
            with self.store.transaction():
                existing = self._owned_turn(customer_id, request_id)
            if existing is not None:
                return self._existing_turn(existing)
            raise AdvisorServiceError(409, "会话已有进行中的轮次", code="active_turn") from exc
        return PreparedTurn(
            mode="run",
            session_id=target["id"],
            request_id=request_id,
            turn=turn,
            message=text,
            profile=profile,
        )

    def prepare_resume(self, customer_id: str, request_id: str, profile_patch: dict[str, Any]) -> PreparedTurn:
        customer_id = customer_id.upper()
        self._ensure_current_turn(customer_id, request_id)
        cleaned = validate_profile_patch(profile_patch)
        with self.store.transaction():
            turn = self._require_turn(customer_id, request_id)
            if turn["status"] != "interrupted":
                raise AdvisorServiceError(409, "当前轮次不在等待画像确认", code="not_interrupted")
            payload = turn.get("interrupt_payload") or {}
            if payload.get("kind") in {"clarification", "allocation"}:
                raise AdvisorServiceError(409, "当前轮次不在等待画像确认", code="not_interrupted")
            missing = list(payload.get("missing_fields") or [])
            # 缺失字段必须出现在本次用户提交中，不能用模型猜测补齐。
            absent = [field for field in missing if cleaned.get(field) in (None, "")]
            if absent:
                raise AdvisorServiceError(400, "请补全缺失的画像字段", code="profile_incomplete")
        self._require_ready()
        with self.store.transaction():
            turn = self.store.claim_turn(
                customer_id,
                request_id,
                "interrupted",
                status="running",
                stage="",
                interrupt_payload=None,
            )
            if turn is None:
                self._require_turn(customer_id, request_id)
                raise AdvisorServiceError(409, "当前轮次不在等待画像确认", code="not_interrupted")
            profile = self.store.upsert_profile(customer_id, cleaned)
        return PreparedTurn(
            mode="resume",
            session_id=turn["session_id"],
            request_id=request_id,
            turn=turn,
            profile=profile,
            resume_value=profile,
        )

    def prepare_clarify(self, customer_id: str, request_id: str, answers: dict[str, Any]) -> PreparedTurn:
        customer_id = customer_id.upper()
        self._ensure_current_turn(customer_id, request_id)
        version_changed = False
        with self.store.transaction():
            turn = self._require_turn(customer_id, request_id)
            if turn["status"] != "interrupted":
                raise AdvisorServiceError(409, "当前轮次不在等待澄清", code="not_interrupted")
            payload = turn.get("interrupt_payload") or {}
            if payload.get("kind") != "clarification":
                raise AdvisorServiceError(409, "当前轮次不在等待澄清", code="not_interrupted")
            if payload.get("agent_version") != "stock-react-v1":
                self.store.update_turn(
                    request_id,
                    status="failed",
                    stage="失败",
                    error_code="stock_agent_version_changed",
                    retryable=True,
                )
                version_changed = True
                cleaned = {}
            else:
                cleaned = validate_clarify_answers(payload, answers)
        if version_changed:
            raise AdvisorServiceError(
                409,
                "Stock Agent 版本已更新，请重新发起请求",
                code="stock_agent_version_changed",
            )
        self._require_ready()
        with self.store.transaction():
            turn = self.store.claim_turn(
                customer_id,
                request_id,
                "interrupted",
                status="running",
                stage="",
                interrupt_payload=None,
            )
            if turn is None:
                self._require_turn(customer_id, request_id)
                raise AdvisorServiceError(409, "当前轮次不在等待澄清", code="not_interrupted")
            profile = self.store.get_profile(customer_id)
        return PreparedTurn(
            mode="resume",
            session_id=turn["session_id"],
            request_id=request_id,
            turn=turn,
            profile=profile,
            resume_value=cleaned,
        )

    def prepare_allocation_adopt(
        self, customer_id: str, request_id: str, resume_value: dict[str, Any]
    ) -> PreparedTurn:
        """校验并接受"采纳某个方案"的选择，然后 resume 原轮次。

        与 ``prepare_clarify`` 同构，但校验对象是 ``plan_id``：**只能从本轮载荷里声明的、
        且可用的方案中选**。这样模型或前端都无法凭空指定一个方案来绕过校验器。
        """
        customer_id = customer_id.upper()
        self._ensure_current_turn(customer_id, request_id)
        with self.store.transaction():
            turn = self._require_turn(customer_id, request_id)
            if turn["status"] != "interrupted":
                raise AdvisorServiceError(409, "当前轮次不在等待方案选择", code="not_interrupted")
            payload = turn.get("interrupt_payload") or {}
            if payload.get("kind") != "allocation":
                raise AdvisorServiceError(409, "当前轮次不在等待方案选择", code="not_interrupted")
            try:
                cleaned = validate_allocation_resume(payload, resume_value)
            except AllocationInterruptError as exc:
                raise AdvisorServiceError(422, str(exc), code=exc.code) from exc

        self._require_ready()
        with self.store.transaction():
            claimed = self.store.claim_turn(
                customer_id,
                request_id,
                "interrupted",
                status="running",
                stage="",
                interrupt_payload=None,
            )
            if claimed is None:
                self._require_turn(customer_id, request_id)
                raise AdvisorServiceError(409, "当前轮次不在等待方案选择", code="not_interrupted")
            profile = self.store.get_profile(customer_id)
        return PreparedTurn(
            mode="resume",
            session_id=claimed["session_id"],
            request_id=request_id,
            turn=claimed,
            profile=profile,
            resume_value=cleaned,
        )

    def prepare_retry(self, customer_id: str, request_id: str) -> PreparedTurn:
        customer_id = customer_id.upper()
        self._ensure_current_turn(customer_id, request_id)
        self._require_ready()
        with self.store.transaction():
            turn = self.store.claim_turn(
                customer_id,
                request_id,
                "failed",
                require_retryable=True,
                status="running",
                stage="",
                error_code="",
                retryable=False,
            )
            if turn is None:
                self._require_turn(customer_id, request_id)
                raise AdvisorServiceError(409, "当前轮次不能重试", code="not_retryable")
            profile = self.store.get_profile(customer_id)
        return PreparedTurn(
            mode="retry",
            session_id=turn["session_id"],
            request_id=request_id,
            turn=turn,
            profile=profile,
        )

    async def play(self, customer_id: str, prepared: PreparedTurn) -> AsyncIterator[dict[str, Any]]:
        customer_id = customer_id.upper()
        yield _event("run", {"request_id": prepared.request_id, "session_id": prepared.session_id})
        if prepared.mode == "replay":
            yield _event("done", {
                "request_id": prepared.request_id,
                "session_id": prepared.session_id,
                "answer": prepared.turn.get("final_answer") or "",
            })
            return
        if prepared.mode == "status":
            if prepared.turn.get("error_code") == "graph_version_changed":
                yield _event("error", {"code": "graph_version_changed", "retryable": False})
                return
            yield _event("status", {
                "stage": prepared.turn.get("stage") or prepared.turn.get("status") or "",
                "retryable": bool(prepared.turn.get("retryable")),
                "error_code": prepared.turn.get("error_code") or "",
            })
            if prepared.turn.get("status") == "interrupted":
                name, payload = public_interrupt_payload(prepared.turn.get("interrupt_payload") or {})
                yield _event(name, payload)
            return
        tokens = None
        try:
            runtime = self._runtime_getter()
            graph_input: Any
            if prepared.mode == "retry":
                graph_input = None
            elif prepared.mode == "resume":
                graph_input = Command(resume=prepared.resume_value or {})
            else:
                graph_input = await to_thread.run_sync(self._graph_input, customer_id, prepared)
            tokens = bind_readonly_services(runtime.library, runtime.portfolio)
            async for event in self._consume_graph(customer_id, prepared, graph_input):
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端断流或任务被取消：此时不会走 _consume_graph 的 except，
            # 若不落库，轮次会永久停在 running 并锁死会话（且 running 无法重试）。
            await to_thread.run_sync(partial(self._fail, prepared.request_id, "client_disconnected", retryable=True))
            raise
        except Exception as exc:  # noqa: BLE001 - 执行准备阶段也已发出 SSE 响应头
            code, retryable = _classify_failure(exc)
            logger.error("advisor_turn_prepare_failed code=%s reason=%s", code, sanitize_text(type(exc).__name__))
            await to_thread.run_sync(partial(self._fail, prepared.request_id, code, retryable=retryable))
            yield _event("error", {"code": code, "retryable": retryable})
        finally:
            if tokens is not None:
                reset_readonly_services(tokens)

    def get_run(self, customer_id: str, request_id: str) -> dict[str, Any]:
        with self.store.transaction():
            turn = self._require_turn(customer_id.upper(), request_id)
        return turn

    def list_sessions(self, customer_id: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            return self.store.list_sessions(customer_id.upper())

    def list_messages(self, customer_id: str, session_id: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            parent = self.store.get_session(customer_id.upper(), session_id)
            if parent is None:
                raise AdvisorServiceError(404, "会话不存在", code="session_not_found")
            return self.store.list_messages(customer_id.upper(), session_id)

    def get_profile(self, customer_id: str) -> dict[str, Any]:
        with self.store.transaction():
            return self.store.get_profile(customer_id.upper())

    def patch_profile(self, customer_id: str, profile_patch: dict[str, Any]) -> dict[str, Any]:
        cleaned = validate_profile_patch(profile_patch)
        with self.store.transaction():
            return self.store.upsert_profile(customer_id.upper(), cleaned)

    async def delete_session(self, customer_id: str, session_id: str) -> None:
        customer_id = customer_id.upper()
        runtime = self._runtime_getter()
        checkpointer = getattr(runtime, "checkpointer", None)
        if checkpointer is None:
            raise AdvisorServiceError(503, "投顾暂不可用", code="advisor_unavailable")
        # 先删业务记录（唯一事实来源），其返回值即是否真的删掉；
        # 再删 checkpoint：顺序倒过来会出现"checkpoint 已删、业务记录还在"的不一致。
        checkpoint_ids = await to_thread.run_sync(self._delete_session_record, customer_id, session_id)
        if checkpoint_ids is None:
            raise AdvisorServiceError(404, "会话不存在", code="session_not_found")
        for thread_id in checkpoint_ids:
            try:
                await checkpointer.adelete_thread(thread_id)
            except Exception:  # noqa: BLE001
                logger.warning("checkpoint_cleanup_failed session_id=%s", session_id)

    async def _consume_graph(
        self,
        customer_id: str,
        prepared: PreparedTurn,
        graph_input: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            runtime = self._runtime_getter()
            profile = await to_thread.run_sync(self.get_profile, customer_id)
            config = {
                "configurable": {
                    "thread_id": checkpoint_thread_id(prepared.session_id, prepared.request_id),
                    "session_id": prepared.session_id,
                    "customer_id": customer_id,
                    "request_id": prepared.request_id,
                    "verified_profile": _profile_snapshot(profile),
                },
                # 主控图每轮任务要经过「参数抽取 → 主控 → 专家」三个节点，最多 3 批 × 8 任务，
                # 40 步在任务真正派发后不够用；任务/批次上限仍是真正的成本闸门。
                "recursion_limit": 80,
            }
            async with asyncio.timeout(load_advisor_settings().turn_timeout_seconds):
                async for item in runtime.graph.astream(
                    graph_input, config, stream_mode=["updates", "custom"], durability="sync",
                ):
                    event = _stream_item(item)
                    if event is not None:
                        if event["event"] == "status":
                            await to_thread.run_sync(self._update_stage, prepared.request_id, event["data"]["stage"])
                        yield event
                snapshot = await runtime.graph.aget_state(config)

            # 中断与完成落库同样在 try 内：此时 SSE 响应头已发出，
            # 落库异常若逃出去会截断数据流并把轮次留在 running。
            interrupted = _interrupt_value(snapshot)
            if interrupted is not None:
                payload = interrupted if isinstance(interrupted, dict) else {"missing_fields": [], "profile": {}}
                event_name, safe_payload = public_interrupt_payload(payload)
                await to_thread.run_sync(
                    self._persist_interrupt,
                    prepared.request_id,
                    safe_payload,
                )
                yield _event(event_name, safe_payload)
                return

            values = getattr(snapshot, "values", {}) or {}
            artifacts, candidates = _public_artifacts(values)
            final = values.get("final")
            answer = str(getattr(final, "answer", "") or "")
            warnings = _collect_warnings(values)
            persisted = await to_thread.run_sync(
                self._persist_completion,
                prepared,
                answer,
                warnings,
                artifacts,
                candidates,
                values.get("session_context"),
            )
            if not persisted:
                yield _event("error", {"code": "turn_state_changed", "retryable": False})
                return
            for artifact in artifacts:
                yield _event("artifact", artifact)
            for warning in warnings:
                yield _event("warning", {"message": sanitize_text(warning)})
            # 正文已由合规节点经 custom 流按增量下发（含尾部），这里不再重复发整段 token，
            # 否则前端会把同一份答案渲染两遍。`done` 事件仍携带权威 answer。
            yield _event("done", {
                "request_id": prepared.request_id,
                "session_id": prepared.session_id,
                "answer": answer,
            })
        except Exception as exc:  # noqa: BLE001 - 返回脱敏错误码，不回传正文
            code, retryable = _classify_failure(exc)
            logger.error(
                "advisor_turn_failed code=%s reason=%s",
                code,
                sanitize_text(type(exc).__name__),
            )
            await to_thread.run_sync(
                partial(self._fail, prepared.request_id, code, retryable=retryable)
            )
            yield _event("error", {"code": code, "retryable": retryable})
            return

    def _graph_input(self, customer_id: str, prepared: PreparedTurn) -> dict[str, Any]:
        limit = load_advisor_settings().context_message_limit
        with self.store.transaction():
            history = self.store.list_messages(customer_id, prepared.session_id)
            session = self.store.get_session(customer_id, prepared.session_id) or {}
            profile = self.store.get_profile(customer_id)
        # 注入的消息必须带落库时的稳定 id：同一个 thread 的 checkpoint 会持续累积历史，
        # add_messages 以 id 去重，重复注入同一批历史才不会再次追加（否则历史会平方级膨胀）。
        messages = [
            {"id": item["id"], "role": item["role"], "content": item["content"]}
            for item in history[-max(limit, 1):]
        ]
        return {
            "messages": messages,
            "profile_snapshot": _profile_snapshot(profile),
            "user_query": prepared.message,
            "session_context": session.get("context") or {"version": 2, "slots": {}, "pending_tasks": []},
            "task_specs": [],
            "prepared": {},
            "outcomes": {},
            "planned": False,
            "batch_count": 0,
            "decision_count": 0,
            "attempted_tasks": {},
            "allocation_queue": [],
            "active_task": None,
            "task_spec": None,
            "domain_slots": {},
            "dependency_results": [],
            # 以下每轮通道必须显式置空：LangGraph 按 channel 合并，未写入的通道会沿用
            # 上一轮 checkpoint 的值，漏清即造成跨轮泄漏（守卫测试见 test_advisor_service）。
            "resolved_task": None,
            "intents": [],
            "stock_result": None,
            "product_result": None,
            "portfolio_result": None,
            "chat_result": None,
            "suitability": None,
            "review": None,
            "final": None,
            "turn_warnings": [],
        }

    def _require_session(self, customer_id: str, session_id: str) -> None:
        with self.store.transaction():
            if self.store.get_session(customer_id, session_id) is None:
                raise AdvisorServiceError(404, "会话不存在", code="session_not_found")

    def _delete_session_record(self, customer_id: str, session_id: str) -> list[str] | None:
        with self.store.transaction():
            if self.store.get_session(customer_id, session_id) is None:
                return None
            self.store.lock_session(session_id)
            turns = self.store.list_turns(session_id)
            ids = {
                checkpoint_thread_id(session_id, turn["request_id"])
                if turn.get("graph_version") == GRAPH_VERSION else str(session_id)
                for turn in turns
            }
            if not self.store.delete_session(customer_id, session_id):
                return None
            return sorted(ids)

    def _update_stage(self, request_id: str, stage: str) -> None:
        with self.store.transaction():
            self.store.update_turn(request_id, stage=stage)

    def _persist_interrupt(self, request_id: str, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "clarification":
            stage = "等待澄清"
        elif kind == "allocation":
            stage = ALLOCATION_WAITING_STAGE
        else:
            stage = "等待画像"
        with self.store.transaction():
            self.store.update_turn(
                request_id,
                status="interrupted",
                stage=stage,
                interrupt_payload=payload,
                retryable=False,
            )

    def _persist_completion(
        self,
        prepared: PreparedTurn,
        answer: str,
        warnings: list[str],
        artifacts: list[dict[str, Any]] | None = None,
        candidate_stocks: list[dict[str, Any]] | None = None,
        session_context: dict[str, Any] | None = None,
    ) -> bool:
        metadata = {
            "warnings": warnings,
            "artifacts": list(artifacts or []),
            "candidate_stocks": list(candidate_stocks or []),
        }
        with self.store.transaction():
            self.store.lock_session(prepared.session_id)
            # 先原子落完成：若轮次已被并发标记为失败/重试，则跳过写消息，
            # 避免晚到的完成覆盖终态并留下孤立助手消息。
            if not self.store.complete_turn(prepared.request_id, final_answer=answer):
                logger.warning("advisor_completion_skipped request_id=%s", prepared.request_id)
                return False
            if session_context is not None:
                self.store.update_session_context(prepared.session_id, session_context)
            self.store.insert_message(
                prepared.session_id,
                prepared.request_id,
                "assistant",
                answer,
                metadata,
            )
            self.store.touch_session(prepared.session_id)
        return True

    def _existing_turn(self, turn: dict[str, Any]) -> PreparedTurn:
        turn = self._invalidate_legacy(turn)
        status = turn.get("status")
        if status == "completed":
            return PreparedTurn("replay", turn["session_id"], turn["request_id"], turn)
        return PreparedTurn("status", turn["session_id"], turn["request_id"], turn)

    def _invalidate_legacy(self, turn: dict[str, Any]) -> dict[str, Any]:
        if turn.get("graph_version") == GRAPH_VERSION or turn.get("status") == "completed":
            return turn
        if turn.get("status") in {"pending", "running", "interrupted"} or (turn.get("status") == "failed" and turn.get("retryable")):
            return self.store.update_turn(
                turn["request_id"], status="failed", stage="失败", retryable=False,
                error_code="graph_version_changed", interrupt_payload=None,
            )
        return turn

    def _ensure_current_turn(self, customer_id: str, request_id: str) -> None:
        with self.store.transaction():
            turn = self._require_turn(customer_id, request_id)
            changed = turn.get("graph_version") != GRAPH_VERSION
            if changed:
                self.store.lock_session(turn["session_id"])
                self._invalidate_legacy(turn)
        if changed:
            raise AdvisorServiceError(409, "投顾版本已更新，请在输入框重新发送问题", code="graph_version_changed")

    def _owned_turn(self, customer_id: str, request_id: str) -> dict[str, Any] | None:
        found = self.store.find_turn(request_id)
        if found is None:
            return None
        if found.get("customer_id") != customer_id:
            raise AdvisorServiceError(404, "请求不存在", code="not_found")
        return found

    def _require_turn(self, customer_id: str, request_id: str) -> dict[str, Any]:
        found = self._owned_turn(customer_id, request_id)
        if found is None:
            raise AdvisorServiceError(404, "请求不存在", code="not_found")
        return found

    def _require_ready(self) -> None:
        runtime = self._runtime_getter()
        if getattr(runtime, "status", "") != "ready" or getattr(runtime, "graph", None) is None:
            raise AdvisorServiceError(503, "投顾暂不可用", code="advisor_unavailable")

    def _fail(self, request_id: str, code: str, *, retryable: bool) -> None:
        """把仍在活动的轮次标记为失败。已是终态（完成/失败）的轮次不会被覆盖。"""
        with self.store.transaction():
            self.store.fail_active_turn(request_id, code=code, retryable=retryable)


def _profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "risk_level": profile.get("risk_level"),
        "investment_horizon": profile.get("investment_horizon"),
        "investment_goal": profile.get("investment_goal"),
        "liquidity_need": profile.get("liquidity_need"),
        "budget_amount": profile.get("budget_amount"),
    }


def _collect_warnings(values: dict[str, Any]) -> list[str]:
    """图级告警 + 各 worker 告警，保持顺序并去重（对外 ``warning`` 事件的唯一来源）。"""
    collected: list[str] = []
    for item in values.get("turn_warnings") or []:
        text = str(item or "").strip()
        if text and text not in collected:
            collected.append(text)
    for result in _worker_results(values):
        for item in result.warnings or []:
            text = str(item or "").strip()
            if text and text not in collected:
                collected.append(text)
    from app.agents.public_warnings import public_warnings
    return public_warnings(collected)


def _public_artifacts(values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocked = {"tool_args", "prompt", "reasoning", "kline", "bars"}
    artifacts, candidates = public_artifacts(_worker_results(values, artifacts_only=True))
    cleaned = [
        {key: value for key, value in item.items() if key not in blocked}
        for item in artifacts
        if isinstance(item, dict)
    ]
    return cleaned, candidates


def _worker_results(values: dict[str, Any], *, artifacts_only: bool = False) -> list[WorkerBase]:
    """以任务最新尝试为事实源；旧结果投影仅用于旧图历史兼容。"""
    outcomes = values.get("outcomes")
    if isinstance(outcomes, dict) and outcomes:
        latest: dict[str, WorkerOutcome] = {}
        for raw in outcomes.values():
            outcome = raw if isinstance(raw, WorkerOutcome) else WorkerOutcome.model_validate(raw)
            previous = latest.get(outcome.task_id)
            if previous is None or outcome.attempt > previous.attempt:
                latest[outcome.task_id] = outcome
        return [
            item.result for item in latest.values()
            if not artifacts_only or (item.result.status != "failed" and item.clarification is None)
        ]
    return [item for key in RESULT_KEY.values() if isinstance(item := values.get(key), WorkerBase)
            and (not artifacts_only or item.status != "failed")]


def _event(name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event": name, "data": data}


def _stream_item(item: Any) -> dict[str, Any] | None:
    """把图流片段映射成公开 SSE 事件。

    - ``updates``：节点名 → 阶段文案，产生 ``status``；
    - ``custom``：仅由合规节点经 ``get_stream_writer`` 下发的**已过滤**增量，产生 ``token``；
    - 原始模型片段不再订阅（``messages`` 模式已移除），从源头杜绝草稿外泄。
    """
    if not isinstance(item, tuple) or len(item) != 2:
        return None
    mode, payload = item
    if mode == "updates" and isinstance(payload, dict):
        for node in payload:
            label = STAGE_LABELS.get(node)
            if label:
                return _event("status", {"stage": label})
        return None
    if mode == "custom" and isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text:
            return _event("token", {"text": text})
        return None
    return None


def _interrupt_value(snapshot: Any) -> Any:
    direct = getattr(snapshot, "interrupts", None) or ()
    if direct:
        first = direct[0]
        return getattr(first, "value", first)
    for task in getattr(snapshot, "tasks", ()) or ():
        found = getattr(task, "interrupts", None) or ()
        if found:
            return getattr(found[0], "value", found[0])
    return None


_service: AgentService | None = None


def get_agent_service() -> AgentService:
    global _service
    if _service is None:
        _service = AgentService()
    return _service


def parse_request_id(value: UUID | str) -> str:
    return str(value)


__all__ = [
    "AdvisorServiceError",
    "AgentService",
    "PreparedTurn",
    "get_agent_service",
    "public_interrupt_payload",
    "validate_clarify_answers",
    "validate_profile_patch",
]
