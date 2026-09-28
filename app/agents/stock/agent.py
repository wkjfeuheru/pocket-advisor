"""标准 LangChain ReAct Stock Agent 的构建入口。"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, ToolRetryMiddleware

from app.agents.stock.contracts import StockAgentResponse
from app.agents.prompts.task_agent_prompts import STOCK_SYSTEM_PROMPT


def _transient_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in text for marker in ("timeout", "rate limit", "rate_limited", "temporarily unavailable")
    )


def build_stock_agent(
    model: Any,
    mcp_tools: list[Any],
    local_tools: list[Any],
    *,
    extra_middleware: list[Any] | None = None,
    checkpointer: Any = None,
) -> Any:
    tools = [*mcp_tools, *local_tools]
    middleware: list[Any] = [
        ToolRetryMiddleware(max_retries=1, retry_on=_transient_error, on_failure="error"),
        ToolCallLimitMiddleware(run_limit=12, exit_behavior="error"),
    ]
    middleware.extend(
        ToolCallLimitMiddleware(tool_name=tool.name, run_limit=3, exit_behavior="error")
        for tool in tools
    )
    middleware.extend(extra_middleware or [])
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=STOCK_SYSTEM_PROMPT,
        middleware=middleware,
        response_format=StockAgentResponse,
        checkpointer=checkpointer,
        name="stock_agent",
    )


__all__ = ["build_stock_agent"]
