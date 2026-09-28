"""投顾对话的请求与响应。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatStreamRequest(BaseModel):
    request_id: UUID
    session_id: UUID | None = None
    message: str = Field(..., min_length=1, max_length=4000)


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: Literal["R1", "R2", "R3", "R4", "R5"] | None = None
    investment_horizon: Literal["短期", "中期", "长期"] | None = None
    investment_goal: Literal["保本", "收益", "均衡增长", "成长"] | None = None
    liquidity_need: Literal["高", "中", "低"] | None = None
    budget_amount: float | None = Field(default=None, gt=0)


class ResumeRequest(BaseModel):
    profile: ProfilePatch


class ClarifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] = Field(default_factory=dict)


class AllocationAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., min_length=1, max_length=128)


class AgentHealth(BaseModel):
    status: Literal["ready", "degraded"]
    reason: str = ""


class SessionView(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class MessageView(BaseModel):
    id: str
    request_id: str
    role: str
    content: str
    public_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunView(BaseModel):
    request_id: str
    session_id: str
    status: str
    stage: str = ""
    final_answer: str = ""
    error_code: str = ""
    retryable: bool = False
    interrupt_payload: dict[str, Any] | None = None


class ProfileView(BaseModel):
    risk_level: str | None = None
    investment_horizon: str | None = None
    investment_goal: str | None = None
    liquidity_need: str | None = None
    budget_amount: float | None = None


__all__ = [
    "AgentHealth",
    "AllocationAdoptRequest",
    "ChatStreamRequest",
    "ClarifyRequest",
    "MessageView",
    "ProfilePatch",
    "ProfileView",
    "ResumeRequest",
    "RunView",
    "SessionView",
]
