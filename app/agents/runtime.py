"""投顾运行时：模型、PgSaver 与编译后的图。初始化失败只标记降级。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import anyio
from psycopg.rows import dict_row

from app.agents.builders import build_specialists
from app.agents.graphs.task_agent import build_advisor_graph
from app.agents.stock.agent import build_stock_agent
from app.agents.stock.middleware import build_evidence_middleware
from app.agents.stock.tools import build_stock_local_tools
from app.agents.model import build_chat_model
from app.utils.sanitize import sanitize_error
from app.core.config import postgres_dsn
from app.core.settings_models import load_advisor_settings
from app.crud.product_crud import get_product_library
from app.crud.stock_feature_crud import get_stock_feature_store
from app.crud.agent_evidence_crud import get_agent_evidence_store
from app.mcp.mcp_client import load_stock_mcp_tools
from app.mcp.tushare_client import load_tushare_client

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    """Agent 子系统状态。``reason`` 已脱敏，可直接返回给健康检查。"""

    status: str = "degraded"
    reason: str = "not_initialized"
    graph: Any = None
    checkpointer: Any = None
    pool: Any = None
    library: Any = None
    portfolio: Any = None
    evidence_store: Any = None


_runtime = AgentRuntime()


def get_agent_runtime() -> AgentRuntime:
    return _runtime


async def init_agents() -> AgentRuntime:
    """启动投顾图。失败时记录脱敏原因并保持业务 API 可用。不调度快照刷新。"""
    global _runtime
    await close_agents()
    pool = None
    try:
        settings = load_advisor_settings()
        model = build_chat_model(settings)
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        pool = AsyncConnectionPool(
            conninfo=postgres_dsn(),
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        mcp_tools, stock_warning = await load_stock_mcp_tools(settings.fuyao_api_key, strict=True)
        tushare, tushare_warning = load_tushare_client(getattr(settings, "tushare_token", "") or "")
        from app.services.portfolio_service import get_portfolio_service

        library = get_product_library()
        portfolio = get_portfolio_service()
        warehouse = get_stock_feature_store()
        evidence_store = None
        stock_agent = None
        try:
            evidence_store = get_agent_evidence_store()
            local_tools = build_stock_local_tools(store=evidence_store, tushare=tushare, warehouse=warehouse)
            evidence_middleware = build_evidence_middleware(
                evidence_store,
                {str(getattr(tool, "name", "")) for tool in mcp_tools},
            )
            stock_agent = build_stock_agent(
                model,
                mcp_tools,
                local_tools,
                extra_middleware=[evidence_middleware],
                checkpointer=checkpointer,
            )
        except Exception as exc:  # noqa: BLE001 - 只降级股票分支
            logger.error("stock_agent_degraded reason=%s", sanitize_error(exc))
            stock_warning = "stock_agent_unavailable"

        def risk_of(code: str) -> int | None:
            from app.agents.graphs.task_agent import risk_rank

            product = library.query_by_code(code)
            basic = (product or {}).get("basic_info") or {}
            return risk_rank(str(basic.get("risk_level") or ""))

        def _allocation_runner(*, customer_id: str, state: dict, interrupt):
            """持仓配置分支的真实实现：生成方案 → 挂起 → 采纳。

            服务在调用时惰性获取（而不是启动时），因此商品库/净值表晚一点就绪
            也不会让整个投顾运行时降级。

            ``request_id`` / ``session_id`` 由 AgentService 在轮次记录里持有，
            图节点拿不到，因此这里不传——它们只用于把方案关联回具体轮次，
            缺失不影响生成与采纳。
            """
            from app.agents.portfolio.branch import run_allocation_task
            from app.services.portfolio_advisor.advisor_service import get_advisor_service

            return run_allocation_task(
                customer_id=customer_id,
                service=get_advisor_service(),
                interrupt=interrupt,
            )

        graph = build_advisor_graph(
            model=model,
            specialists=build_specialists(
                model,
                stock_agent=stock_agent,
                evidence_store=evidence_store,
                stock_warning=stock_warning or tushare_warning,
            ),
            checkpointer=checkpointer,
            risk_of=risk_of,
            context_limit=settings.context_message_limit,
            allocation_runner=_allocation_runner,
        )
        await anyio.to_thread.run_sync(_run_startup_cleanup, settings.turn_timeout_seconds, evidence_store)
        _runtime = AgentRuntime(
            status="ready",
            reason="",
            graph=graph,
            checkpointer=checkpointer,
            pool=pool,
            library=library,
            portfolio=portfolio,
            evidence_store=evidence_store,
        )
        if stock_warning:
            logger.warning("stock_tools_degraded code=%s", stock_warning)
        if tushare_warning:
            logger.warning("tushare_degraded code=%s", tushare_warning)
        return _runtime
    except Exception as exc:  # noqa: BLE001 - 投顾失败不能阻止业务进程
        logger.error("agent_runtime_degraded reason=%s", sanitize_error(exc))
        if pool is not None:
            await _close_pool(pool)
        _runtime = AgentRuntime(status="degraded", reason=sanitize_error(exc))
        return _runtime


async def close_agents() -> None:
    global _runtime
    pool = _runtime.pool
    _runtime = AgentRuntime()
    await _close_pool(pool)


async def _close_pool(pool: Any) -> None:
    if pool is None:
        return
    try:
        await pool.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_pool_close_failed reason=%s", sanitize_error(exc))


def _mark_stale(timeout_seconds: int) -> None:
    from app.crud.agent_session_crud import get_advisor_store

    store = get_advisor_store()
    with store.transaction():
        store.mark_stale_running(timeout_seconds)


def _run_startup_cleanup(timeout_seconds: int, evidence_store: Any) -> None:
    _mark_stale(timeout_seconds)
    if evidence_store is None:
        return
    try:
        evidence_store.cleanup_expired()
    except Exception as exc:  # noqa: BLE001 - TTL 清理不影响其他 Agent 启动
        logger.warning("stock_evidence_cleanup_failed reason=%s", sanitize_error(exc))


def set_agent_runtime(runtime: AgentRuntime) -> None:
    """测试注入运行时。"""
    global _runtime
    _runtime = runtime


__all__ = [
    "AgentRuntime",
    "close_agents",
    "get_agent_runtime",
    "init_agents",
    "set_agent_runtime",
]
