"""投顾图的状态、意图与按 worker 边界的结构化结果。"""

from __future__ import annotations

from typing import Annotated, Literal, Any

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from langgraph.graph.message import add_messages

from app.agents.portfolio.contracts import AllocationOffer, AllocationOutcome
from app.agents.stock.contracts import CandidateStock, StockResultV1

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

WorkerStatus = Literal["ok", "degraded", "failed"]


class Intent(BaseModel):
    """一条可派发的子任务。"""

    agent: AgentName
    task: str
    kind: IntentKind = "general"
    needs_budget: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    task_id: str = Field(default="", max_length=128, pattern=r"^[\w:.-]*$")
    dependencies: list[str] = Field(default_factory=list)
    operation: str = ""


class IntentResult(BaseModel):
    intents: list[Intent]
    pending_action: Literal["continue", "switch", "unclear"] = "switch"


class RewrittenQuery(BaseModel):
    """结合上文改写后的自包含问题。``changed=False`` 表示按原文放行。"""

    task: str
    changed: bool = False


class WorkerBase(BaseModel):
    """4 个 worker 结果的公共外壳。

    ``status`` 是三态：``ok`` 正常、``degraded`` 有产出但证据不足/带告警、
    ``failed`` 分支报错或上下文缺失。只有 ``failed`` 参与“全分支失败”判定。
    """

    model_config = ConfigDict(extra="forbid")

    status: WorkerStatus = "ok"
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    source: str = ""
    as_of: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)


class StockResult(WorkerBase):
    """股票 worker 结果。``result`` 是不含候选池的版本化结果，候选池单独放 ``candidates``。"""

    result: StockResultV1 | None = None
    candidates: list[CandidateStock] = Field(default_factory=list)


class ProductResult(WorkerBase):
    recommended_codes: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class PortfolioResult(WorkerBase):
    """持仓 worker 结果。

    两个字段分别承载两种产物：``offer`` 是挂起前的三档方案集，
    ``adopted`` 是采纳后的成交摘要；普通持仓查询两者都为 None。
    """

    recommended_codes: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    offer: AllocationOffer | None = None
    adopted: AllocationOutcome | None = None


class ChatResult(WorkerBase):
    """通用问答结果，没有推荐字段。"""


class SuitabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = ""
    recommendations: list[str] = Field(default_factory=list)


class ReviewState(BaseModel):
    """生成-校验中间态。``needs_rewrite`` 为真时 compliance_node 自环重写一次。"""

    model_config = ConfigDict(extra="forbid")

    rewrite_count: int = 0
    needs_rewrite: bool = False
    feedback: str = ""


GRAPH_VERSION = "advisor-v2"


def checkpoint_thread_id(session_id: str, request_id: str) -> str:
    return f"{GRAPH_VERSION}:{session_id}:{request_id}"


class TaskSpec(Intent):
    context_id: str = ""
    pending_fields: list[str] = Field(default_factory=list)
    defer_input: bool = False
    refresh_only: bool = False


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: TaskSpec
    slots: dict[str, Any] = Field(default_factory=dict)
    user_query: str
    profile: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[dict[str, Any]] = Field(default_factory=list)


class WorkerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    agent: AgentName
    attempt: int = Field(default=1, ge=1, le=2)
    result: StockResult | ProductResult | PortfolioResult | ChatResult
    clarification: dict[str, Any] | None = None


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_tasks: list[str] = Field(default_factory=list)
    retry_queries: dict[str, str] = Field(default_factory=dict)
    additional_tasks: list[Intent] = Field(default_factory=list)
    finish: bool = False
    reason: str = ""


class PlanPoint(BaseModel):
    """骨架要点：只描述"说什么"与引用哪些事实，不承载数字/代码。"""
    model_config = ConfigDict(extra="forbid")
    point: str = ""
    fact_keys: list[str] = Field(default_factory=list)


class PlanSection(BaseModel):
    """骨架小节：标题 + 若干要点。"""
    model_config = ConfigDict(extra="forbid")
    heading: str = ""
    points: list[PlanPoint] = Field(default_factory=list)


class AnswerPlan(BaseModel):
    """回答骨架：隐藏阶段产出并校验，随后据此做受约束的自然语言流式成文。

    正文数值一律以 ``{{fact_key}}`` 占位符表达，由服务端用已核验事实渲染；
    推荐代码只放在 ``recommendations`` 字段，正文不得出现。
    """
    model_config = ConfigDict(extra="forbid")
    sections: list[PlanSection] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


def merge_outcomes(left: dict, right: dict) -> dict:
    merged = dict(left or {})
    for key, raw in (right or {}).items():
        item = raw if isinstance(raw, WorkerOutcome) else WorkerOutcome.model_validate(raw)
        if key != f"{item.task_id}:{item.attempt}":
            raise ValueError("结果键与任务身份不一致")
        if key in merged and merged[key] != item:
            raise ValueError("同一任务尝试不能回写不同结果")
        merged[key] = item
    return merged


def merge_prepared(left: dict, right: dict) -> dict:
    """准备阶段只更新本任务；相同输入的重放是幂等的。"""
    return {**(left or {}), **(right or {})}


def validate_task_plan(tasks: list[TaskSpec], *, max_tasks: int = 8) -> None:
    ids = [task.task_id for task in tasks]
    if len(tasks) > max_tasks or len(ids) != len(set(ids)) or any(not key for key in ids):
        raise ValueError("任务数量或身份无效")
    by_id = {item.task_id: item for item in tasks}
    visited, visiting = set(), set()
    def visit(key: str):
        if key not in by_id or key in visiting:
            raise ValueError("任务依赖未知或存在循环")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_id[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
    for key in ids:
        visit(key)


#: agent → 对外兼容投影。v2 的权威结果在 outcomes，按任务身份合并。
RESULT_KEY: dict[str, str] = {
    "stock_agent": "stock_result",
    "product_agent": "product_result",
    "portfolio_agent": "portfolio_result",
    "chat_agent": "chat_result",
}

#: agent → 结果模型。节点在边界用它把 worker 的返回值收口成 typed 对象。
RESULT_MODEL: dict[str, type[WorkerBase]] = {
    "stock_agent": StockResult,
    "product_agent": ProductResult,
    "portfolio_agent": PortfolioResult,
    "chat_agent": ChatResult,
}

#: 每轮都必须由 `_graph_input` 显式重置的通道（`messages` 跨轮累加、`profile_snapshot`
#: 每轮由 DB 权威重建，二者不在其中）。
PER_TURN_CHANNELS: tuple[str, ...] = (
    "resolved_task",
    "intents",
    "stock_result",
    "product_result",
    "portfolio_result",
    "chat_result",
    "suitability",
    "review",
    "final",
    "turn_warnings",
    "user_query", "task_specs", "prepared", "outcomes", "planned", "batch_count",
    "decision_count", "attempted_tasks", "allocation_queue", "active_task",
    "task_spec", "domain_slots", "dependency_results",
)


class AdvisorState(TypedDict, total=False):
    """主工作流状态。

    消息用稳定 ID 合并；画像每次从可信运行配置刷新。任务结果按身份幂等合并，
    并行准备只更新自身任务。session_context 是会话数据库的持久上下文快照。
    """

    messages: Annotated[list, add_messages]
    profile_snapshot: dict
    resolved_task: str | None
    intents: list[Intent]
    stock_result: StockResult | None
    product_result: ProductResult | None
    portfolio_result: PortfolioResult | None
    chat_result: ChatResult | None
    suitability: SuitabilityResult | None
    review: ReviewState | None
    final: FinalAnswer | None
    turn_warnings: list[str]
    user_query: str
    session_context: dict
    task_specs: list[TaskSpec]
    prepared: Annotated[dict, merge_prepared]
    outcomes: Annotated[dict, merge_outcomes]
    planned: bool
    batch_count: int
    decision_count: int
    attempted_tasks: dict
    active_task: TaskSpec
    task_spec: TaskSpec
    domain_slots: dict
    dependency_results: list
    allocation_queue: list[str]


def worker_failure(agent: str, message: str, *, source: str = "", as_of: str = "") -> WorkerBase:
    """构造一个 ``status="failed"`` 的 worker 结果（分支报错/上下文缺失）。"""
    model = RESULT_MODEL.get(agent, ChatResult)
    return model(
        status="failed",
        summary=message,
        warnings=[message] if message else [],
        source=source,
        as_of=as_of,
    )


def degraded_result(agent: str, message: str, *, source: str = "", as_of: str = "") -> WorkerBase:
    """构造一个 ``status="degraded"`` 的 worker 结果（有产出但不可用，不中止整轮）。"""
    model = RESULT_MODEL.get(agent, ChatResult)
    return model(
        status="degraded",
        summary=message,
        warnings=[message] if message else [],
        source=source,
        as_of=as_of,
    )


__all__ = [
    "GRAPH_VERSION", "checkpoint_thread_id", "TaskSpec", "WorkerRequest", "WorkerOutcome",
    "SupervisorDecision", "AnswerPlan", "PlanPoint", "PlanSection",
    "merge_outcomes", "validate_task_plan",
    "AGENT_NAMES",
    "PER_TURN_CHANNELS",
    "PROFILE_FIELDS",
    "RESULT_KEY",
    "RESULT_MODEL",
    "AdvisorState",
    "ChatResult",
    "FinalAnswer",
    "Intent",
    "IntentResult",
    "PortfolioResult",
    "ProductResult",
    "ReviewState",
    "RewrittenQuery",
    "StockResult",
    "SuitabilityResult",
    "WorkerBase",
    "WorkerStatus",
    "degraded_result",
    "worker_failure",
]
