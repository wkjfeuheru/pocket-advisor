"""投顾 LangGraph：意图、画像门禁、并行专家、适配性与汇总。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from anyio import to_thread
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config

from app.agents.graphs.state import (
    AGENT_NAMES,
    PROFILE_FIELDS,
    AdvisorState,
    IntentResult,
    RewrittenQuery,
    empty_result,
)
from app.agents.prompts.task_agent_prompts import (
    AGGREGATOR_SYSTEM_PROMPT,
    CLASSIFY_SYSTEM_PROMPT,
    RESOLVE_SYSTEM_PROMPT,
)
from app.agents.artifacts import project_artifact
from app.services.portfolio_service import SIMULATED_DISCLAIMER

_TRADE_LANGUAGE = re.compile(r"买入|卖出|加仓|减仓|目标价|买卖点|仓位比例|仓位\s*\d+\s*%")

Specialist = Callable[[str, dict], Awaitable[dict]]

_CODE_LINE = re.compile(r"推荐代码\s*[:：]\s*([^\n]+)")
_RISK = re.compile(r"R([1-5])", re.IGNORECASE)
_NON_DEGRADABLE = {
    "AllBranchesFailed",
    "GraphBubbleUp",
    "GraphInterrupt",
    "GraphRecursionError",
    "NodeInterrupt",
}

STAGE_LABELS = {
    "resolve_query": "理解追问",
    "classify_intent": "识别问题",
    "profile_gate": "核对画像",
    "dispatch_intents": "分派任务",
    "stock_agent": "查询行情",
    "product_agent": "查询商品",
    "portfolio_agent": "查询持仓",
    "chat_agent": "整理问答",
    "suitability_guard": "核对适配性",
    "aggregator": "整理回答",
    "output_guard": "校验回答",
}


class AllBranchesFailed(RuntimeError):
    """本轮派发的专业分支全部失败。"""


def is_degradable(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError, AllBranchesFailed)):
        return False
    return type(exc).__name__ not in _NON_DEGRADABLE and isinstance(exc, Exception)


def risk_rank(value: str | None) -> int | None:
    if not value:
        return None
    match = _RISK.search(str(value))
    return int(match.group(1)) if match else None


def parse_recommendations(text: str) -> list[str]:
    match = _CODE_LINE.search(text or "")
    if match is None:
        return []
    raw = match.group(1).strip()
    if raw in {"无", "没有", "none", "None"}:
        return []
    return [part.strip() for part in re.split(r"[,，\s]+", raw) if part.strip() and part.strip() != "无"]


def public_token(message: Any, metadata: dict | None) -> str | None:
    """只放行 aggregator 的增量文本。专家节点的 token 不进入用户流。"""
    if (metadata or {}).get("langgraph_node") != "aggregator":
        return None
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        joined = "".join(parts)
        return joined or None
    return None


def _intent_dicts(state: AdvisorState) -> list[dict]:
    items = []
    for item in state.get("intents") or []:
        if hasattr(item, "model_dump"):
            items.append(item.model_dump())
        else:
            items.append(dict(item))
    return items


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _task_for(state: AdvisorState, agent: str) -> str:
    for intent in _intent_dicts(state):
        if intent.get("agent") == agent and intent.get("task"):
            return str(intent["task"])
    resolved = str(state.get("resolved_task") or "")
    if resolved:
        return resolved
    messages = state.get("messages") or []
    return _message_text(messages[-1]) if messages else ""


def build_advisor_graph(
    *,
    model: Any,
    specialists: dict[str, Specialist],
    checkpointer: Any = None,
    risk_of: Callable[[str], int | None] | None = None,
    context_limit: int = 20,
    allocation_runner: Callable[..., dict[str, Any]] | None = None,
):
    """编译主工作流。正式调用要传 ``durability='sync'``。

    ``allocation_runner`` 非空时，``portfolio_agent`` 节点改为走持仓配置分支
    （生成方案后 ``interrupt`` 等用户选择）；为空则保持原有的普通专业分支。
    """

    def _risk(code: str) -> int | None:
        if risk_of is None:
            return None
        return risk_of(code)

    def _user_message_count(messages: list) -> int:
        count = 0
        for message in messages:
            if isinstance(message, dict):
                role = message.get("role")
            else:
                # add_messages 之后是 LangChain 消息对象：HumanMessage.type == "human"。
                role = getattr(message, "type", "") or getattr(message, "role", "")
            if role in {"user", "human"}:
                count += 1
        return count

    async def resolve_query(state: AdvisorState) -> dict:
        """多轮追问改写：首轮直接放行，有历史时解析指代后交给意图识别。"""
        messages = list(state.get("messages") or [])
        if _user_message_count(messages) < 2:
            return {}
        task = _message_text(messages[-1])
        if not task:
            return {}
        try:
            structured = model.with_structured_output(RewrittenQuery, method="function_calling")
            result = await structured.ainvoke([
                {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
                *messages[-max(context_limit, 1):],
            ])
        except Exception as exc:
            if not is_degradable(exc):
                raise
            return {"warnings": ["问题理解暂不可用，已按原文处理"]}
        if isinstance(result, dict):
            rewritten = str(result.get("task") or "")
        else:
            rewritten = str(getattr(result, "task", "") or "")
        if not rewritten:
            return {}
        return {"resolved_task": rewritten}

    async def classify_intent(state: AdvisorState) -> dict:
        structured = model.with_structured_output(IntentResult, method="function_calling")
        history = list(state.get("messages") or [])[-max(context_limit, 1):]
        resolved = str(state.get("resolved_task") or "")
        if resolved and history:
            history = [*history[:-1], {"role": "user", "content": resolved}]
        result = await structured.ainvoke([
            {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
            *history,
        ])
        intents = result.intents if hasattr(result, "intents") else result["intents"]
        dumped = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in intents]
        return {"intents": dumped}

    def profile_gate(state: AdvisorState) -> dict:
        intents = _intent_dicts(state)
        personalized = [item for item in intents if item.get("kind") == "personalized"]
        profile = dict(state.get("profile_snapshot") or {})
        if not personalized:
            return {"profile_snapshot": profile}
        missing = [field for field in PROFILE_FIELDS if not profile.get(field)]
        if any(item.get("needs_budget") for item in personalized) and not profile.get("budget_amount"):
            missing.append("budget_amount")
        if not missing:
            return {"profile_snapshot": profile}
        confirmed = interrupt({"missing_fields": missing, "profile": profile})
        # interrupt 返回的是用户已经确认并落库的画像，这里只放进本轮状态。
        if isinstance(confirmed, dict):
            profile.update({key: value for key, value in confirmed.items() if value not in (None, "")})
        still_missing = [field for field in missing if not profile.get(field)]
        if still_missing:
            interrupt({"missing_fields": still_missing, "profile": profile})
        return {"profile_snapshot": profile}

    def dispatch_intents(state: AdvisorState) -> Command:
        resolved = str(state.get("resolved_task") or "")
        sends = []
        for intent in _intent_dicts(state):
            agent = str(intent.get("agent") or "")
            if agent not in AGENT_NAMES or agent not in specialists:
                continue
            sends.append(Send(agent, {
                "messages": [{"role": "user", "content": str(intent.get("task") or "")}],
                "intents": [intent],
                "resolved_task": resolved,
                "profile_snapshot": state.get("profile_snapshot") or {},
            }))
        if not sends:
            sends.append(Send("chat_agent", {
                "messages": list(state.get("messages") or [])[-1:],
                "intents": [{"agent": "chat_agent", "task": _task_for(state, "chat_agent"), "kind": "general"}],
                "resolved_task": resolved,
                "profile_snapshot": state.get("profile_snapshot") or {},
            }))
        return Command(goto=sends)

    def _specialist_node(agent: str):
        async def run(state: AdvisorState) -> dict:
            task = _task_for(state, agent)
            try:
                result = await specialists[agent](task, dict(state))
            except Exception as exc:
                if not is_degradable(exc):
                    raise
                result = empty_result(agent, f"{agent} 暂不可用")
            if not isinstance(result, dict):
                result = empty_result(agent, f"{agent} 暂不可用")
            result.setdefault("agent", agent)
            result.setdefault("recommended_product_codes", [])
            result.setdefault("candidate_stocks", [])
            result.setdefault("artifacts", [])
            result.setdefault("warnings", [])
            result.setdefault("source", "")
            result.setdefault("as_of", "")
            result.setdefault("failed", False)
            warnings = [str(item) for item in result.get("warnings") or [] if item]
            return {"agent_results": [result], "warnings": warnings}

        return run

    def _allocation_node(runner: Any, fallback: Any):
        """把持仓配置分支包成图节点。

        ``runner`` 是需要 ``(customer_id, service, interrupt)`` 的可调用对象；
        这里负责从运行配置里取**可信的** ``customer_id``（绝不来自模型参数），
        并在分支内部异常时退回普通专业分支，避免整轮失败。
        """
        async def run(state: AdvisorState) -> dict:
            config = get_config()
            customer_id = str(
                (config.get("configurable") or {}).get("customer_id") or ""
            ).strip()
            if not customer_id:
                # 没有可信客户上下文时不挂起（否则 resume 无法定位账户），退回普通分支。
                return await fallback(state)
            try:
                result = await to_thread.run_sync(
                    lambda: runner(customer_id=customer_id, state=dict(state), interrupt=interrupt)
                )
            except Exception as exc:  # noqa: BLE001
                if not is_degradable(exc):
                    raise
                return await fallback(state)
            if not isinstance(result, dict):
                return await fallback(state)
            result.setdefault("agent", "portfolio_agent")
            result.setdefault("recommended_product_codes", [])
            result.setdefault("candidate_stocks", [])
            result.setdefault("artifacts", [])
            result.setdefault("warnings", [])
            result.setdefault("source", "")
            result.setdefault("as_of", "")
            result.setdefault("failed", False)
            return {
                "agent_results": [result],
                "warnings": [str(item) for item in result.get("warnings") or [] if item],
            }

        return run

    def suitability_guard(state: AdvisorState) -> dict:
        results = list(state.get("agent_results") or [])
        if not results or all(bool(item.get("failed")) for item in results):
            raise AllBranchesFailed("全部专业分支失败")
        profile = state.get("profile_snapshot") or {}
        user_rank = risk_rank(str(profile.get("risk_level") or ""))
        allowed: list[str] = []
        blocked: list[str] = []
        for result in results:
            for code in result.get("recommended_product_codes") or []:
                code = str(code).strip()
                if not code:
                    continue
                product_rank = _risk(code)
                if user_rank is None or product_rank is None or product_rank > user_rank:
                    blocked.append(code)
                elif code not in allowed:
                    allowed.append(code)
        blocked = [code for code in dict.fromkeys(blocked) if code not in allowed]
        return {"suitability_result": {"allowed": allowed, "blocked": blocked}}

    def _evidence_brief(result: dict) -> str:
        lines = [f"summary: {result.get('content') or ''}"]
        agent = str(result.get("agent") or "")
        for item in result.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            public = project_artifact(agent, item)
            if not public:
                continue
            lines.append("evidence: " + json.dumps(public, ensure_ascii=False, default=str))
        return "\n".join(lines)

    async def aggregator(state: AdvisorState) -> dict:
        suitability = state.get("suitability_result") or {}
        materials = []
        for result in state.get("agent_results") or []:
            if result.get("failed"):
                materials.append(f"{result.get('agent')}: {result.get('content') or '暂不可用'}")
                continue
            materials.append(
                "\n".join([
                    f"agent: {result.get('agent')}",
                    _evidence_brief(result),
                    f"source: {result.get('source') or ''}",
                    f"as_of: {result.get('as_of') or ''}",
                    f"warnings: {', '.join(result.get('warnings') or [])}",
                ])
            )
        feedback = state.get("guard_feedback") or ""
        human = "\n\n".join([
            "专业结果：",
            "\n---\n".join(materials) or "无",
            f"画像快照：{state.get('profile_snapshot') or {}}",
            f"允许推荐：{suitability.get('allowed') or []}",
            f"禁止推荐：{suitability.get('blocked') or []}",
            f"校验反馈：{feedback}",
        ])
        parts: list[str] = []
        async for chunk in model.astream([
            {"role": "system", "content": AGGREGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": human},
        ]):
            text = chunk.content if isinstance(getattr(chunk, "content", None), str) else ""
            if text:
                parts.append(text)
        answer = "".join(parts).strip()
        return {
            "final_result": answer,
            "final_recommendations": parse_recommendations(answer),
            "needs_rewrite": False,
        }

    def output_guard(state: AdvisorState) -> dict:
        suitability = state.get("suitability_result") or {}
        allowed = set(suitability.get("allowed") or [])
        blocked = set(suitability.get("blocked") or [])
        recommended = [str(code) for code in state.get("final_recommendations") or []]
        bad = [code for code in recommended if code in blocked or code not in allowed]
        count = int(state.get("rewrite_count") or 0)
        if bad and count < 1:
            return {
                "needs_rewrite": True,
                "rewrite_count": count + 1,
                "guard_feedback": f"推荐清单包含不适配代码 {', '.join(bad)}，请去掉这些推荐。",
            }
        text = state.get("final_result") or ""
        recommendations = [code for code in recommended if code in allowed and code not in blocked]
        if bad:
            text = "当前没有与您风险等级匹配的推荐。更高风险的产品不会进入推荐集合。"
            recommendations = []
        text = _TRADE_LANGUAGE.sub("", text)
        if SIMULATED_DISCLAIMER not in text:
            text = f"{text.rstrip()}\n\n{SIMULATED_DISCLAIMER}".strip()
        return {
            "needs_rewrite": False,
            "final_result": text,
            "final_recommendations": recommendations,
        }

    def route_after_guard(state: AdvisorState) -> str:
        if state.get("needs_rewrite"):
            return "aggregator"
        return END

    graph = StateGraph(AdvisorState)
    graph.add_node("resolve_query", resolve_query)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("profile_gate", profile_gate)
    graph.add_node(
        "dispatch_intents",
        dispatch_intents,
        destinations=AGENT_NAMES,
    )
    for agent in AGENT_NAMES:
        if agent == "portfolio_agent" and allocation_runner is not None:
            # 持仓配置是**唯一**会挂起等用户选择的专业分支：生成方案后 interrupt，
            # 用户选定后用 Command(resume={"plan_id": ...}) 继续。其余分支保持原样。
            graph.add_node(agent, _allocation_node(allocation_runner, _specialist_node(agent)))
        else:
            graph.add_node(agent, _specialist_node(agent))
        graph.add_edge(agent, "suitability_guard")
    graph.add_node("suitability_guard", suitability_guard)
    graph.add_node("aggregator", aggregator)
    graph.add_node("output_guard", output_guard)
    graph.add_edge(START, "resolve_query")
    graph.add_edge("resolve_query", "classify_intent")
    graph.add_edge("classify_intent", "profile_gate")
    graph.add_edge("profile_gate", "dispatch_intents")
    graph.add_edge("suitability_guard", "aggregator")
    graph.add_edge("aggregator", "output_guard")
    graph.add_conditional_edges("output_guard", route_after_guard, ["aggregator", END])
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "STAGE_LABELS",
    "AllBranchesFailed",
    "build_advisor_graph",
    "is_degradable",
    "parse_recommendations",
    "public_token",
    "risk_rank",
]
