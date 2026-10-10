"""投顾图的状态与意图结构。worker 结果模型见 ``results.py``。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from langgraph.graph.message import add_messages

AgentName = Literal["stock_agent", "product_agent", "portfolio_agent", "chat_agent"]
IntentKind = Literal["general", "personalized"]

AGENT_NAMES: tuple[str, ...] = (
    "stock_agent",
    "product_agent",
    "portfolio_agent",
    "chat_agent",
)

PROFILE_FIELDS: tuple[str, ...] = (
    "risk_level",
    "investment_horizon",
    "investment_goal",
    "liquidity_need",
)


class Intent(BaseModel):
    """一条可派发的子任务。"""

    agent: AgentName
    task: str
    kind: IntentKind = "general"
    needs_budget: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class IntentResult(BaseModel):
    intents: list[Intent]


class RewrittenQuery(BaseModel):
    """结合上文改写后的自包含问题。``changed=False`` 表示按原文放行。"""

    task: str
    changed: bool = False


class Suitability(TypedDict):
    allowed: list[str]
    blocked: list[str]


class ComplianceReview(TypedDict):
    rewrite_count: int
    needs_rewrite: bool
    feedback: str


class FinalAnswer(TypedDict):
    answer: str
    recommendations: list[str]


class AdvisorState(TypedDict, total=False):
    """主工作流状态。

    会话 id 即 thread_id，checkpoint 跨轮保留。只有 ``messages`` / ``profile_snapshot``
    跨轮有意义，其余字段都是本轮数据，由 ``begin_turn`` 在每轮开始时清空（``TURN_SCOPED_KEYS``）。
    四个 worker 各占一个通道，存 ``app.agents.graphs.results`` 里对应模型的 JSON 形态。
    """

    messages: Annotated[list, add_messages]
    profile_snapshot: dict
    resolved_task: str | None
    intents: list[dict] | None
    turn_warnings: list[str] | None
    stock_result: dict | None
    product_result: dict | None
    portfolio_result: dict | None
    chat_result: dict | None
    suitability: Suitability | None
    review: ComplianceReview | None
    final: FinalAnswer | None


TURN_SCOPED_KEYS: tuple[str, ...] = (
    "resolved_task",
    "intents",
    "turn_warnings",
    "stock_result",
    "product_result",
    "portfolio_result",
    "chat_result",
    "suitability",
    "review",
    "final",
)


def fresh_turn() -> dict:
    return {key: None for key in TURN_SCOPED_KEYS}


__all__ = [
    "AGENT_NAMES",
    "PROFILE_FIELDS",
    "TURN_SCOPED_KEYS",
    "AdvisorState",
    "ComplianceReview",
    "FinalAnswer",
    "Intent",
    "IntentResult",
    "RewrittenQuery",
    "Suitability",
    "fresh_turn",
]
