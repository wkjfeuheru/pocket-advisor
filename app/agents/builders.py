"""构建四个专业 Agent 的主图适配器。"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langgraph.config import get_config

from app.agents.graphs.results import RESULT_TYPES, StockWorkerResult, failed_result
from app.agents.stock.assembler import assemble_stock_result
from app.agents.stock.contracts import StockAgentResponse
from app.agents.stock.evidence_store import EvidenceContext
from app.agents.stock.tools import bind_stock_execution, reset_stock_execution
from app.agents.graphs.task_agent import parse_recommendations
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


def _content_of(result: Any) -> str:
    messages = result.get("messages") if isinstance(result, dict) else getattr(result, "messages", None)
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    return content if isinstance(content, str) else str(content or "")


def _runner(agent: Any, name: str, source: str, warning: str):
    result_type = RESULT_TYPES[name]

    async def run(task: str, _state: dict) -> Any:
        if agent is None:
            return failed_result(name, warning, status="degraded", source=source)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": task}]},
            get_config(),
        )
        content = _content_of(result)
        fields: dict[str, Any] = {"summary": content, "source": source}
        if "recommended_product_codes" in result_type.model_fields:
            fields["recommended_product_codes"] = parse_recommendations(content)
        return result_type(**fields)

    return run


def stock_agent_runner(stock_agent: Any, evidence_store: Any, *, warning: str = ""):
    def degraded(message: str) -> StockWorkerResult:
        return StockWorkerResult(
            status="degraded",
            summary="股票实时证据暂不可用，仅能提供通用定性说明。",
            warnings=[message],
        )

    async def run(task: str, state: dict) -> StockWorkerResult:
        if stock_agent is None or evidence_store is None:
            return degraded(warning or "stock_agent_unavailable")
        import asyncio

        config = get_config()
        configurable = dict(config.get("configurable") or {})
        request_id = str(configurable.get("request_id") or "")
        session_id = str(configurable.get("thread_id") or "")
        customer_id = str(configurable.get("customer_id") or "")
        if not request_id or not session_id or not customer_id:
            return failed_result("stock_agent", "股票证据上下文缺失", source="fuyao")
        context = EvidenceContext(request_id=request_id, session_id=session_id, customer_id=customer_id)
        token = bind_stock_execution(context, profile=state.get("profile_snapshot") or {})
        try:
            async with asyncio.timeout(60):
                result = await stock_agent.ainvoke(
                    {"messages": state.get("messages") or [{"role": "user", "content": task}]},
                    config,
                )
                structured = result.get("structured_response") if isinstance(result, dict) else None
                if structured is None:
                    structured = StockAgentResponse(
                        scenario="equity_research",
                        summary="",
                        warnings=["missing_structured_response"],
                        evidence_quality="failed",
                        failed=True,
                    )
                return await asyncio.to_thread(assemble_stock_result, structured, context, evidence_store)
        finally:
            reset_stock_execution(token)
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
) -> dict:
    """四个 Agent；Stock Agent 已由运行时用动态 MCP tools 编译。"""
    fallback = stock_warning or "股票行情暂不可用，未编造行情数据"
    stock_runner = stock_agent_runner(stock_agent, evidence_store, warning=fallback)
    product_agent = create_agent(
        model=model,
        tools=_PRODUCT_TOOLS,
        system_prompt=PRODUCT_SYSTEM_PROMPT,
        name="product_agent",
    )
    portfolio_agent = create_agent(
        model=model,
        tools=_PORTFOLIO_TOOLS,
        system_prompt=PORTFOLIO_SYSTEM_PROMPT,
        name="portfolio_agent",
    )
    chat_agent = create_agent(
        model=model,
        tools=[],
        system_prompt=CHAT_SYSTEM_PROMPT,
        name="chat_agent",
    )
    return {
        "stock_agent": stock_runner,
        "product_agent": _runner(product_agent, "product_agent", "postgresql", "商品查询暂不可用"),
        "portfolio_agent": _runner(portfolio_agent, "portfolio_agent", "postgresql", "持仓查询暂不可用"),
        "chat_agent": _runner(chat_agent, "chat_agent", "", "问答暂不可用"),
    }


__all__ = ["build_specialists", "stock_agent_runner"]
