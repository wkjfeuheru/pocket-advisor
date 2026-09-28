"""投顾 HTTP：仅普通用户，SSE 只携带公开事件。"""

from __future__ import annotations

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


def _service():
    return get_agent_service()


def _raise(exc: AdvisorServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _sse(events: AsyncIterator[dict]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        async for event in events:
            payload = json.dumps(event["data"], ensure_ascii=False)
            yield f"event: {event['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
