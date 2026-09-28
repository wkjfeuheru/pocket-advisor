"""投顾图的状态、意图和结果结构。"""

from __future__ import annotations

import operator
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


class AdvisorState(TypedDict, total=False):
    """主工作流状态。``agent_results`` 和 ``warnings`` 按并行分支合并。"""

    messages: Annotated[list, add_messages]
    intents: list[dict]
    resolved_task: str
    agent_results: Annotated[list[dict], operator.add]
    profile_snapshot: dict
    suitability_result: dict
    warnings: Annotated[list[str], operator.add]
    final_result: str | None
    final_recommendations: list[str]
    rewrite_count: int
    needs_rewrite: bool
    guard_feedback: str


def empty_result(agent: str, warning: str, *, source: str = "", as_of: str = "") -> dict:
    return {
        "agent": agent,
        "content": warning,
        "recommended_product_codes": [],
        "candidate_stocks": [],
        "artifacts": [],
        "warnings": [warning] if warning else [],
        "source": source,
        "as_of": as_of,
        "failed": True,
    }


__all__ = [
    "AGENT_NAMES",
    "PROFILE_FIELDS",
    "AdvisorState",
    "Intent",
    "IntentResult",
    "RewrittenQuery",
    "empty_result",
]
