"""标准 LangChain ReAct Stock Agent 的构建入口。"""

from __future__ import annotations

import json
import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
    wrap_tool_call,
)
from langchain_core.messages import ToolMessage

from app.agents.stock.contracts import StockAgentResponse
from app.agents.stock.tools import optional_stock_execution
from app.agents.prompts.task_agent_prompts import STOCK_SYSTEM_PROMPT

_TOOL_REPEAT_LIMIT = 3


def _transient_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in text for marker in ("timeout", "rate limit", "rate_limited", "temporarily unavailable")
    )


def _tool_repeat_guard() -> Any:
    """单个工具的重复调用上限。

    用一次 ``wrap_tool_call`` 计数器实现：每个
    ``ToolCallLimitMiddleware(tool_name=...)`` 都会在每轮模型调用时执行一次
    ``after_model`` 节点，工具一多就会把图步数预算（recursion_limit）烧光，
    反而让正常的多工具研究无法完成。
    """

    @wrap_tool_call(name="StockToolRepeatGuard")
    async def guard(request: Any, handler: Any) -> Any:
        execution = optional_stock_execution()
        if execution is None:
            return await handler(request)
        name = str(request.tool_call.get("name") or "")
        from app.agents.evidence import current_execution, NeedsClarification
        active = current_execution()
        if active and active.pending_clarification:
            raise NeedsClarification(active.pending_clarification)
        signature = name + json.dumps(request.tool_call.get("args") or {}, sort_keys=True, default=str)
        lock = execution.tool_locks.setdefault(signature, asyncio.Lock())
        async with lock:
            if signature in execution.tool_results:
                cached = execution.tool_results[signature]
                return cached.model_copy(update={"tool_call_id": str(request.tool_call.get("id") or "")})
            used = execution.tool_calls.get(signature, 0) + 1
            execution.tool_calls[signature] = used
            if used > _TOOL_REPEAT_LIMIT:
                return ToolMessage(content=json.dumps({"warning": "tool_repeat_limit", "tool": name, "data": None}),
                    tool_call_id=str(request.tool_call.get("id") or ""), name=name, status="error")
            result = await handler(request)
            if isinstance(result, ToolMessage) and result.status != "error":
                execution.tool_results[signature] = result
            return result

    return guard


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
        # 模型调用预算：模型偶发只顾自问自答而不产出结构化响应，用次数上限兜底并优雅收尾。
        ModelCallLimitMiddleware(run_limit=20, exit_behavior="end"),
        # 单轮工具预算：基本面研究通常需要 10+ 次只读查询；超出后按 `end` 优雅收尾，
        # 让已取得的证据仍能产出结果，而不是把整个任务判失败。
        ToolCallLimitMiddleware(run_limit=20, exit_behavior="end"),
        _tool_repeat_guard(),
    ]
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
