"""股票模块的请求、结果和澄清载荷。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Scenario = Literal["market_overview", "equity_research", "stock_screening"]
Strategy = Literal["value_quality", "growth_quality", "trend_momentum"]
Horizon = Literal["short", "medium", "long"]
EvidenceQuality = Literal["ok", "partial", "failed"]
ResolveStatus = Literal["resolved", "ambiguous", "none"]


class ScenarioClassification(BaseModel):
    """意图分类的结构化输出。symbols 此时仍是用户原文，尚未变成 thscode。"""

    model_config = ConfigDict(extra="forbid")

    scenario: Scenario
    symbols: list[str] = Field(default_factory=list)
    strategy: Strategy | None = None
    horizon: Horizon | None = None
    industries: list[str] = Field(default_factory=list)
    index_scope: list[str] = Field(default_factory=list)
    candidate_count: int = Field(default=5, ge=1, le=10)
    filter_by_profile: bool = False


class StockRequest(BaseModel):
    """规范化后的股票子图请求。symbols 必须是 thscode。"""

    model_config = ConfigDict(extra="forbid")

    scenario: Scenario
    symbols: list[str] = Field(default_factory=list)
    strategy: Strategy | None = None
    horizon: Horizon | None = None
    industries: list[str] = Field(default_factory=list)
    index_scope: list[str] = Field(default_factory=list)
    candidate_count: int = Field(default=5, ge=1, le=10)
    filter_by_profile: bool = False
    raw_queries: list[str] = Field(default_factory=list)


class CandidateOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class ClarificationPayload(BaseModel):
    """与画像中断分离的澄清载荷。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"] = "clarification"
    reason: str
    question: str
    fields: list[str] = Field(default_factory=list)
    options: list[CandidateOption] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)


class SourcedNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    source: str = ""
    as_of: str = ""


class CandidateStock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thscode: str
    name: str = ""
    score: float | None = None


class StockResultV1(BaseModel):
    """版本化联合结果。场景块三选一，缺省为 None。"""

    model_config = ConfigDict(extra="forbid")

    scenario: Scenario
    summary: str = ""
    as_of: str = ""
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = "partial"
    market_overview: dict[str, Any] | None = None
    equity_research: list[dict[str, Any]] | None = None
    stock_screening: dict[str, Any] | None = None


class StockAgentResponse(BaseModel):
    """ReAct Agent 只返回叙事与证据句柄，数值 artifact 由服务端组装。"""

    model_config = ConfigDict(extra="forbid")

    scenario: Scenario
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = "partial"
    failed: bool = False


__all__ = [
    "CandidateOption",
    "CandidateStock",
    "ClarificationPayload",
    "Horizon",
    "ScenarioClassification",
    "SourcedNumber",
    "StockRequest",
    "StockAgentResponse",
    "StockResultV1",
    "Strategy",
]
