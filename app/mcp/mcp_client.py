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
            tools, rejected = filter_mcp_tools(discovered, allowed_server_prefixes=prefixes)
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
