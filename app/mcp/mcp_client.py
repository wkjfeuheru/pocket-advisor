"""Fuyao 股票、指数和元数据 MCP。失败时返回空工具列表，不抛给业务 API。"""

from __future__ import annotations

import logging
from typing import Any

from app.utils.sanitize import sanitize_error
from app.agents.tools.guard import filter_mcp_tools, select_readonly_tools

logger = logging.getLogger(__name__)

_SERVERS = {
    "fuyao_a_share": "https://fuyao.aicubes.cn/mcp/a-share",
    "fuyao_a_share_index": "https://fuyao.aicubes.cn/mcp/a-share-index",
    "fuyao_meta": "https://fuyao.aicubes.cn/mcp/meta",
}


def _server_config(url: str, api_key: str) -> dict[str, Any]:
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": {"X-api-key": api_key},
    }


def _relax_mcp_output_schema_validation() -> None:
    """关闭 MCP SDK 对工具 ``structuredContent`` 的 outputSchema 校验。

    Fuyao 声明的 outputSchema 与其真实返回不一致（例如 ``...financials_indicators``
    的 ``value`` 会返回 ``null``），SDK 校验失败会直接把整个工具调用升级为异常，
    导致股票分支整体失败。本项目只消费 ``content`` 文本，并由 ``normalize_payload``、
    证据落库与事实核验自行校验，因此这里关闭这层校验；工具白名单与只读约束不受影响。
    """
    from mcp.client.session import ClientSession

    if getattr(ClientSession, "_finance_output_schema_relaxed", False):
        return

    async def _skip_output_schema_validation(self: Any, name: str, result: Any) -> None:
        return None

    ClientSession._validate_tool_result = _skip_output_schema_validation  # type: ignore[method-assign]
    ClientSession._finance_output_schema_relaxed = True  # type: ignore[attr-defined]
    logger.info("mcp_output_schema_validation_relaxed")


async def load_stock_mcp_tools(
    api_key: str,
    *,
    strict: bool = False,
) -> tuple[list[Any], str]:
    """加载行情工具。未配置或上游失败时返回警告码，不返回上游正文。"""
    if not (api_key or "").strip():
        return [], "fuyao_unconfigured"
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        _relax_mcp_output_schema_validation()
        connections = {
            name: _server_config(url, api_key.strip())
            for name, url in _SERVERS.items()
        }
        client = (
            MultiServerMCPClient(connections, tool_name_prefix=True)
            if strict
            else MultiServerMCPClient(connections)
        )
        loaded = client.get_tools()
        if hasattr(loaded, "__await__"):
            loaded = await loaded
        discovered = list(loaded or [])
        if strict:
            prefixes = tuple(f"{name}_" for name in _SERVERS)
            # Fuyao 上游不发布 readOnlyHint，改用显式的只读操作白名单作为信任来源；
            # 延迟导入避免与 app.mcp.fuyao 形成模块级循环依赖。
            from app.mcp.fuyao import operation_for_tool_name

            tools, rejected = filter_mcp_tools(
                discovered,
                allowed_server_prefixes=prefixes,
                trusted_readonly=lambda name: bool(operation_for_tool_name(name)),
            )
            logger.info("fuyao_tools_discovered names=%s", [getattr(tool, "name", "") for tool in discovered])
            logger.info("fuyao_tools_accepted names=%s", [getattr(tool, "name", "") for tool in tools])
            if rejected:
                logger.warning("fuyao_tools_rejected tools=%s", rejected)
        else:
            tools = select_readonly_tools(discovered)
            rejected = {}
        dropped = len(discovered) - len(tools)
        if dropped and not strict:
            logger.warning("fuyao_tools_dropped count=%s", dropped)
        return tools, ""
    except Exception as exc:  # noqa: BLE001 - 行情故障只降级股票分支
        logger.warning("fuyao_unavailable reason=%s", sanitize_error(exc))
        return [], "fuyao_unavailable"


__all__ = ["load_stock_mcp_tools"]
