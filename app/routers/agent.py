"""投顾 HTTP：仅普通用户，SSE 只携带公开事件。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.dependencies import require_user
from app.schemas.agent import (
    AgentHealth,
    AllocationAdoptRequest,
    ChatStreamRequest,
    ClarifyRequest,
    MessageView,
    ProfilePatch,
    ProfileView,
    ResumeRequest,
    RunView,
    SessionView,
)
from app.services.agent_service import AdvisorServiceError, get_agent_service
from app.agents.runtime import get_agent_runtime

router = APIRouter(prefix="/api/agents", tags=["advisor"])

#: 空闲心跳间隔（秒）。长图执行期间可能长时间没有新事件，靠 SSE 注释行保活，
#: 避免 nginx / 中间层按空闲超时切断连接。
_HEARTBEAT_SECONDS = 15.0


def _service():
    return get_agent_service()


def _raise(exc: AdvisorServiceError) -> None:
    if exc.code == "graph_version_changed":
        raise HTTPException(status_code=exc.status_code, detail={"message": exc.detail, "code": exc.code}) from exc
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _sse(events: AsyncIterator[dict]) -> StreamingResponse:
    """把图事件流包装成 SSE，并在空闲时发送心跳注释行。

    事件生产者放在独立任务里，消费者只对队列取元素做超时等待；超时只取消
    ``queue.get()`` 这个普通协程，**不会**取消事件生成器，因此不会把一次心跳
    误判成客户端断流（那会触发 ``play()`` 里的 client_disconnected 处理）。
    """

    async def body() -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            try:
                async for event in events:
                    await queue.put(event)
            finally:
                await queue.put(None)

        producer = asyncio.create_task(pump())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                payload = json.dumps(item["data"], ensure_ascii=False)
                yield f"event: {item['event']}\ndata: {payload}\n\n"
        finally:
            # 客户端断流时（生成器被关闭）取消生产者，让事件生成器收到取消并落库。
            if not producer.done():
                producer.cancel()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/health", response_model=AgentHealth)
def agent_health(_customer_id: str = Depends(require_user)) -> AgentHealth:
    runtime = get_agent_runtime()
    status = "ready" if runtime.status == "ready" else "degraded"
    return AgentHealth(status=status, reason=runtime.reason or "")


@router.get("/sessions", response_model=list[SessionView])
def list_sessions(customer_id: str = Depends(require_user)) -> list[SessionView]:
    try:
        rows = _service().list_sessions(customer_id)
    except AdvisorServiceError as exc:
        _raise(exc)
    return [SessionView(**row) for row in rows]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageView])
def list_messages(session_id: UUID, customer_id: str = Depends(require_user)) -> list[MessageView]:
    try:
        rows = _service().list_messages(customer_id, str(session_id))
    except AdvisorServiceError as exc:
        _raise(exc)
    return [MessageView(**row) for row in rows]


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: UUID, customer_id: str = Depends(require_user)) -> dict:
    try:
        await _service().delete_session(customer_id, str(session_id))
    except AdvisorServiceError as exc:
        _raise(exc)
    return {"deleted": True}


@router.get("/profile", response_model=ProfileView)
def get_profile(customer_id: str = Depends(require_user)) -> ProfileView:
    try:
        profile = _service().get_profile(customer_id)
    except AdvisorServiceError as exc:
        _raise(exc)
    return ProfileView(**profile)


@router.patch("/profile", response_model=ProfileView)
def patch_profile(
    payload: ProfilePatch,
    customer_id: str = Depends(require_user),
) -> ProfileView:
    try:
        profile = _service().patch_profile(customer_id, payload.model_dump(exclude_none=True))
    except AdvisorServiceError as exc:
        _raise(exc)
    return ProfileView(**profile)


@router.post("/chat/stream")
def chat_stream(payload: ChatStreamRequest, customer_id: str = Depends(require_user)):
    try:
        events = _service().stream_turn(customer_id, payload)
    except AdvisorServiceError as exc:
        _raise(exc)
    return _sse(events)


@router.get("/runs/{request_id}", response_model=RunView)
def get_run(request_id: UUID, customer_id: str = Depends(require_user)) -> RunView:
    try:
        turn = _service().get_run(customer_id, str(request_id))
    except AdvisorServiceError as exc:
        _raise(exc)
    return RunView(**turn)


@router.post("/runs/{request_id}/retry/stream")
def retry_stream(request_id: UUID, customer_id: str = Depends(require_user)):
    try:
        events = _service().retry_turn(customer_id, str(request_id))
    except AdvisorServiceError as exc:
        _raise(exc)
    return _sse(events)


@router.post("/runs/{request_id}/resume/stream")
def resume_stream(
    request_id: UUID,
    payload: ResumeRequest,
    customer_id: str = Depends(require_user),
):
    patch = payload.profile.model_dump(exclude_none=True)
    try:
        events = _service().resume_turn(customer_id, str(request_id), patch)
    except AdvisorServiceError as exc:
        _raise(exc)
    return _sse(events)


@router.post("/runs/{request_id}/clarify/stream")
def clarify_stream(
    request_id: UUID,
    payload: ClarifyRequest,
    customer_id: str = Depends(require_user),
):
    try:
        events = _service().clarify_turn(customer_id, str(request_id), payload.answers)
    except AdvisorServiceError as exc:
        _raise(exc)
    return _sse(events)


@router.post("/runs/{request_id}/allocation/stream")
def allocation_stream(
    request_id: UUID,
    payload: AllocationAdoptRequest,
    customer_id: str = Depends(require_user),
):
    try:
        events = _service().adopt_allocation_turn(customer_id, str(request_id), payload.plan_id)
    except AdvisorServiceError as exc:
        _raise(exc)
    return _sse(events)


__all__ = ["router"]
