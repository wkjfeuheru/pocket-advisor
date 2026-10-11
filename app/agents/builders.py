"""构建四个专业 Agent 的主图适配器。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from langchain.agents import create_agent
from langgraph.config import get_config

from app.agents.graphs.state import (
    ChatResult,
    PortfolioResult,
    ProductResult,
    StockResult,
    WorkerBase,
    degraded_result,
    worker_failure,
)
from app.agents.stock.assembler import assemble_stock_result
from app.agents.stock.contracts import StockAgentResponse
from app.agents.stock.evidence_store import EvidenceContext
from app.agents.stock.tools import bind_stock_execution, reset_stock_execution
from app.agents.evidence import (FactReference, NeedsClarification, bind_execution, current_execution,
                                 facts_from_evidence, reset_execution, verify_fact)
from app.agents.stock.evidence_store import EvidenceAccessError
from app.agents.prompts.task_agent_prompts import (
    CHAT_SYSTEM_PROMPT,
    PORTFOLIO_SYSTEM_PROMPT,
    PRODUCT_SYSTEM_PROMPT,
)
from app.agents.tools.database import (
    get_account_snapshot,
    get_product_detail,
    get_product_holdings,
    get_product_performance,
    list_positions,
    list_products,
)

_PRODUCT_TOOLS = [
    list_products,
    get_product_detail,
    get_product_performance,
    get_product_holdings,
]
_PORTFOLIO_TOOLS = [
    get_account_snapshot,
    list_positions,
    get_product_detail,
    get_product_performance,
    get_product_holdings,
]

_EVIDENCE_INSTRUCTIONS = """
你会收到已核对的 domain_slots、profile_snapshot 与 dependency_results。遵照这些参数调用只读工具，
不得从 task 或历史聊天中改写客户身份、画像、任务依赖或已确认的参数。
必须通过结构化响应返回 summary、recommended_codes、evidence_ids、facts。
推荐代码只能取自引用的真实商品证据。事实项包含 id、evidence_id、field_path（例如 data.nav 或 data.0.weight）、
subject（工具返回的 code/product_code/subject）、metric（路径末尾字段）、value、unit（原数据单位，无单位则空串）、as_of。
summary 中数字事实请使用 {{fact:id}} 占位符，服务端核验后填入；没有证据时明确说明不可用。
不要把 dependency_results 的事实改为自己的查询结果，不得编造证据 ID 或字段。
"""


def _content_of(result: Any) -> str:
    messages = result.get("messages") if isinstance(result, dict) else getattr(result, "messages", None)
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    return content if isinstance(content, str) else str(content or "")


class AgentResponse(BaseModel):
    """商品与持仓模型只选择证据，事实必须由服务端核验。"""

    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    recommended_codes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    facts: list[FactReference] = Field(default_factory=list)


def _context(config: dict) -> EvidenceContext | None:
    values = config.get("configurable") or {}
    request_id = str(values.get("request_id") or "")
    session_id = str(values.get("session_id") or values.get("thread_id") or "")
    customer_id = str(values.get("customer_id") or "")
    if not request_id or not session_id or not customer_id:
        return None
    return EvidenceContext(request_id, session_id, customer_id,
                           task_id=str(values.get("task_id") or ""),
                           dependency_task_ids=tuple(values.get("dependency_task_ids") or ()))


def _worker_input(task: str, state: dict, *, store: Any = None, context: EvidenceContext | None = None) -> dict:
    task_spec = state.get("task_spec")
    if hasattr(task_spec, "model_dump"):
        task_spec = task_spec.model_dump(mode="json")
    dependencies = []
    for index, result in enumerate(state.get("dependency_results") or []):
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        if not isinstance(result, dict):
            continue
        verified = []
        dependency_id = context.dependency_task_ids[index] if context is not None and index < len(context.dependency_task_ids) else ""
        for fact in result.get("facts") or []:
            try:
                if store is not None and context is not None:
                    row = store.get(fact.get("evidence_id", "") if isinstance(fact, dict) else fact.evidence_id, context)
                    if row.get("task_id") != dependency_id:
                        raise EvidenceAccessError("dependency_fact_task_mismatch")
                    verified.append(verify_fact(store, context, fact).model_dump(mode="json"))
            except (EvidenceAccessError, ValidationError):
                continue
        dependencies.append({"task_id": dependency_id, "facts": verified})
    from datetime import datetime
    from zoneinfo import ZoneInfo
    payload = {"task": task, "task_spec": task_spec, "user_query": state.get("user_query") or task,
               "current_datetime": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
               "domain_slots": state.get("domain_slots") or {},
               "profile_snapshot": state.get("profile_snapshot") or {}, "dependency_results": dependencies}
    return {"messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _product_codes(payload: Any) -> set[str]:
    if isinstance(payload, list):
        return {code for item in payload for code in _product_codes(item)}
    if isinstance(payload, dict):
        codes = {str(payload[key]) for key in ("code", "product_code") if payload.get(key)}
        return codes | {code for value in payload.values() if isinstance(value, (dict, list))
                        for code in _product_codes(value)}
    return set()


def _runner(agent: Any, name: str, source: str, warning: str, result_model: type[WorkerBase], *, evidence_store: Any = None):
    """把 ReAct agent 适配成 worker：产出对应 ``result_model`` 的结构化结果。

    ``recommended_codes`` 只对商品/持仓结果有意义；通用问答（``ChatResult``）没有该字段，
    因此按结果模型分派构造参数，避免 ``extra="forbid"`` 因多余键报错。
    """

    async def run(task: str, state: dict) -> WorkerBase:
        if agent is None:
            # Agent 未装配：可用性降级，不中止整轮（与旧 failed=False 语义一致）。
            return degraded_result(name, warning)
        config = get_config()
        if result_model is ChatResult:
            result = await agent.ainvoke(_worker_input(task, state, store=evidence_store, context=_context(config)), config)
            return ChatResult(status="ok", summary=_content_of(result), source="")
        context = _context(config)
        if context is None:
            return worker_failure(name, "任务证据上下文缺失")
        token = bind_execution(evidence_store, context, domain_slots=state.get("domain_slots"), profile=state.get("profile_snapshot"), agent=name)
        try:
            result = await agent.ainvoke(_worker_input(task, state, store=evidence_store, context=context), config)
            execution = current_execution()
            if execution is not None and execution.pending_clarification:
                raise NeedsClarification(execution.pending_clarification)
            try:
                response = AgentResponse.model_validate(result.get("structured_response") if isinstance(result, dict) else None)
            except ValidationError:
                return degraded_result(name, "missing_structured_response")
            rows = []
            warnings = list(execution.warnings if execution else [])
            slots = state.get("domain_slots") or {}
            if slots.get("timeframe"):
                warnings.append("unsupported_timeframe: 当前只读工具仅返回最新披露数据，未完成历史区间查询")
            if slots.get("limitations"):
                warnings.append("unsupported_limitations: 未验证这些额外限制，不能据此声称已满足")
            for row in execution.rows if execution else []:
                payload = row["payload"]
                if isinstance(payload, dict) and payload.get("warning"):
                    warnings.append(str(payload["warning"]))
            for evidence_id in dict.fromkeys(response.evidence_ids):
                try:
                    row = evidence_store.get(evidence_id, context) if evidence_store else None
                    if row is None or not isinstance(row["payload"], dict) or row["payload"].get("data") is None:
                        warnings.append("evidence_unavailable")
                    else:
                        rows.append(row)
                except (EvidenceAccessError, ValueError):
                    warnings.append("invalid_evidence_reference")
            evidence_ids = [row["id"] for row in rows]
            facts = []
            for fact in response.facts:
                try:
                    if fact.evidence_id not in evidence_ids:
                        raise EvidenceAccessError("unreferenced_fact")
                    facts.append(verify_fact(evidence_store, context, fact).model_dump(mode="json"))
                except (EvidenceAccessError, ValidationError):
                    warnings.append("invalid_fact_reference")
            codes = {code for row in rows for code in _product_codes(row["payload"].get("data"))}
            fixed_codes = {str(code) for code in [*(slots.get("codes") or []), *(slots.get("symbols") or [])]}
            if fixed_codes:
                codes &= fixed_codes
            if result_model is PortfolioResult and execution is not None:
                codes &= execution.product_codes
            recommendations = [code for code in dict.fromkeys(response.recommended_codes) if code in codes]
            if slots.get("timeframe") or slots.get("limitations"):
                recommendations = []
            summary = response.summary
            if len(recommendations) != len(set(response.recommended_codes)):
                warnings.append("unsupported_recommendation")
                summary = "部分推荐缺少可核验的商品证据，已暂停输出推荐说明。"
            if not rows:
                warnings.append("no_verified_evidence")
                summary = "当前缺少可核验数据，暂不能完成该查询。"
            sources = sorted({str(row.get("source") or "") for row in rows} - {""})
            dates = [str(row["as_of"]) for row in rows if row.get("as_of")]
            if any(not row.get("source") or not row.get("as_of") for row in rows):
                warnings.append("evidence_metadata_missing")
            return result_model(status="degraded" if warnings else "ok", summary=summary,
                                source=",".join(sources), as_of=min(dates) if dates else "",
                                warnings=list(dict.fromkeys(warnings)), evidence_ids=evidence_ids, facts=facts,
                                recommended_codes=recommendations,
                                data={row["id"]: row["payload"].get("data") for row in rows})
        finally:
            reset_execution(token)

    return run


def stock_agent_runner(stock_agent: Any, evidence_store: Any, *, warning: str = "", market_tools: list | None = None):
    def degraded(message: str) -> StockResult:
        return StockResult(
            status="degraded",
            summary="股票实时证据暂不可用，仅能提供通用定性说明。",
            warnings=[message],
        )

    async def run(task: str, state: dict) -> StockResult:
        if stock_agent is None or evidence_store is None:
            return degraded(warning or "stock_agent_unavailable")
        import asyncio

        config = get_config()
        configurable = dict(config.get("configurable") or {})
        request_id = str(configurable.get("request_id") or "")
        session_id = str(configurable.get("session_id") or configurable.get("thread_id") or "")
        customer_id = str(configurable.get("customer_id") or "")
        if not request_id or not session_id or not customer_id:
            return worker_failure("stock_agent", "股票证据上下文缺失")  # type: ignore[return-value]
        context = _context(config)
        assert context is not None
        token = bind_stock_execution(context, profile=state.get("profile_snapshot") or {})
        evidence_token = bind_execution(evidence_store, context, domain_slots=state.get("domain_slots"), profile=state.get("profile_snapshot"), agent="stock_agent")
        try:
            async with asyncio.timeout(60):
                # 内层 ReAct Agent 需要独立步数预算：每次模型/工具往返都会经过若干中间件节点，
                # 直接沿用外层图较小的 recursion_limit 会把内层误判为递归超限。
                inner_config = {**config, "recursion_limit": max(int(config.get("recursion_limit") or 0), 120)}
                slots = state.get("domain_slots") or {}
                if slots.get("scenario") == "market_overview" and market_tools is not None:
                    from app.agents.stock.market_fetch import collect_market_evidence
                    result = {"structured_response": await collect_market_evidence(evidence_store, context, market_tools, slots)}
                else:
                    result = await stock_agent.ainvoke(
                        _worker_input(task, state, store=evidence_store, context=context), inner_config)
                execution = current_execution()
                if execution is not None and execution.pending_clarification:
                    raise NeedsClarification(execution.pending_clarification)
                structured = result.get("structured_response") if isinstance(result, dict) else None
                if structured is None:
                    structured = StockAgentResponse(
                        scenario=slots.get("scenario") or "equity_research",
                        summary="已获取部分证据，按有效数据展示。",
                        evidence_ids=[row["id"] for row in execution.rows] if execution else [],
                        warnings=["missing_structured_response"], evidence_quality="partial", failed=False,
                    )
                parsed = structured if isinstance(structured, StockAgentResponse) else StockAgentResponse.model_validate(structured)
                if slots.get("scenario") and parsed.scenario != slots["scenario"]:
                    parsed = parsed.model_copy(update={"scenario": slots["scenario"]})
                structured = parsed
                assembled = await asyncio.to_thread(assemble_stock_result, structured, context, evidence_store)
                parsed = structured if isinstance(structured, StockAgentResponse) else StockAgentResponse.model_validate(structured)
                if assembled.result is not None and assembled.result.evidence_quality != "failed":
                    assembled.evidence_ids = list(dict.fromkeys(parsed.evidence_ids))
                    assembled.facts = [fact.model_dump(mode="json") for fact in await asyncio.to_thread(
                        facts_from_evidence, evidence_store, context, assembled.evidence_ids)]
                if execution and execution.warnings:
                    assembled.warnings = list(dict.fromkeys([*assembled.warnings, *execution.warnings]))
                    if assembled.status == "ok":
                        assembled.status = "degraded"
                slots = state.get("domain_slots") or {}
                if slots.get("limitations"):
                    assembled.warnings.append("unsupported_limitations: 未验证附加限制")
                    if assembled.status == "ok":
                        assembled.status = "degraded"
                if slots.get("timeframe") and slots.get("scenario") != "equity_research" and market_tools is None:
                    assembled.warnings.append("unsupported_timeframe: 当前市场概览与选股仅使用最新快照")
                    if assembled.status == "ok":
                        assembled.status = "degraded"
                return assembled
        except Exception:
            execution = current_execution()
            if execution is not None and execution.pending_clarification:
                raise NeedsClarification(execution.pending_clarification) from None
            raise
        finally:
            reset_stock_execution(token)
            reset_execution(evidence_token)
            try:
                await asyncio.to_thread(evidence_store.cleanup_expired)
            except Exception:  # noqa: BLE001 - 惰性 TTL 清理失败不覆盖本轮结果
                pass

    return run


def build_specialists(
    model: Any,
    *,
    stock_agent: Any = None,
    evidence_store: Any = None,
    stock_warning: str = "",
    market_tools: list | None = None,
) -> dict:
    """四个 Agent；Stock Agent 已由运行时用动态 MCP tools 编译。"""
    fallback = stock_warning or "股票行情暂不可用，未编造行情数据"
    stock_runner = stock_agent_runner(stock_agent, evidence_store, warning=fallback, market_tools=market_tools)
    product_agent = create_agent(
        model=model,
        tools=_PRODUCT_TOOLS,
        system_prompt=PRODUCT_SYSTEM_PROMPT + _EVIDENCE_INSTRUCTIONS,
        name="product_agent",
        response_format=AgentResponse,
    )
    portfolio_agent = create_agent(
        model=model,
        tools=_PORTFOLIO_TOOLS,
        system_prompt=PORTFOLIO_SYSTEM_PROMPT + _EVIDENCE_INSTRUCTIONS,
        name="portfolio_agent",
        response_format=AgentResponse,
    )
    chat_agent = create_agent(
        model=model,
        tools=[],
        system_prompt=CHAT_SYSTEM_PROMPT,
        name="chat_agent",
    )
    return {
        "stock_agent": stock_runner,
        "product_agent": _runner(
            product_agent, "product_agent", "postgresql", "商品查询暂不可用", ProductResult, evidence_store=evidence_store
        ),
        "portfolio_agent": _runner(
            portfolio_agent, "portfolio_agent", "postgresql", "持仓查询暂不可用", PortfolioResult, evidence_store=evidence_store
        ),
        "chat_agent": _runner(chat_agent, "chat_agent", "", "问答暂不可用", ChatResult, evidence_store=evidence_store),
    }


__all__ = ["build_specialists", "stock_agent_runner"]
