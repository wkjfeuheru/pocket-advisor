"""Fuyao 只读操作客户端。白名单以内的工具才能调用，且不把工具对象交给模型。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.tools.guard import is_forbidden_tool
from app.mcp.envelope import wrap_envelope
from app.mcp.fuyao_dto import normalize_payload
from app.mcp.mcp_client import load_stock_mcp_tools
from app.utils.sanitize import sanitize_error

logger = logging.getLogger(__name__)

FUYAO_OPERATIONS: tuple[str, ...] = (
    "search_security",
    "list_industries",
    "list_indices",
    "quote",
    "kline",
    "financials",
    "valuation",
    "market_breadth",
    "sector_quote",
    "trade_calendar",
    "company_profile",
    "index_members",
)

# 每个内部操作均以 Fuyao 2026-09-27 实际 ``tools/list`` 目录为首选。
# 末尾的旧名称仅用于兼容测试替身和旧网关，不能再依赖猜测式命名。
_TOOL_NAMES = {
    "search_security": (
        "get_meta_tickers_search", "search_security", "search_stock", "meta_search",
    ),
    "list_industries": (
        "get_meta_tickers_list", "list_industries", "industry_list", "meta_industry",
    ),
    "list_indices": (
        "get_a_share_index_catalog_ths_index_list", "list_indices", "index_list", "meta_index",
    ),
    "quote": (
        "get_a_share_prices_snapshot", "get_a_share_index_prices_snapshot",
        "quote", "get_quote", "realtime_quote",
    ),
    "kline": (
        "get_a_share_prices_historical", "kline", "get_kline", "daily_kline",
    ),
    "financials": (
        "get_a_share_financials_indicators", "financials", "get_financials", "finance_indicator",
    ),
    "valuation": (
        "get_a_share_valuations_snapshot", "valuation", "get_valuation",
    ),
    "market_breadth": ("market_breadth", "market_activity", "market_overview"),
    "sector_quote": ("sector_quote", "industry_quote", "concept_quote"),
    "trade_calendar": (
        "get_a_share_calendar_trading_days", "trade_calendar", "trading_calendar",
    ),
    "company_profile": ("company_profile", "stock_profile"),
    "index_members": (
        "get_a_share_index_constituents_ths_stock_list",
        "index_members", "index_weight", "index_member",
    ),
}

Operation = Callable[..., Awaitable[Any]]


def operation_for_tool_name(tool_name: str) -> str:
    """把带 MCP server 前缀的动态工具名映射为内部只读操作。"""
    name = str(tool_name or "")
    for prefix in ("fuyao_a_share_index_", "fuyao_a_share_", "fuyao_meta_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for operation, aliases in _TOOL_NAMES.items():
        if name in aliases:
            return operation
    return ""


def select_whitelisted_tools(tools: list[Any]) -> dict[str, Any]:
    """只保留白名单操作。禁止工具和未点名工具一律丢掉。"""
    by_name = {getattr(tool, "name", ""): tool for tool in tools}
    selected: dict[str, Any] = {}
    for operation, tool_names in _TOOL_NAMES.items():
        if operation not in FUYAO_OPERATIONS or is_forbidden_tool(operation):
            continue
        for tool_name in tool_names:
            tool = by_name.get(tool_name)
            if tool is None or is_forbidden_tool(tool_name):
                continue
            selected[operation] = tool
            break
    return selected


def whitelist_diagnostics(tools: list[Any]) -> dict[str, Any]:
    """发现到的工具名与未能映射的内部操作。

    工具名/操作名都是 schema 元数据，不是凭据，可以进日志和健康检查。
    MCP 会动态发现工具，但"上游工具名 → 内部业务操作"必须显式映射；
    这张对照表一旦对不上，就会发现成功却调用全失败。
    """
    discovered = sorted({str(getattr(tool, "name", "") or "") for tool in tools})
    selected = select_whitelisted_tools(tools)
    return {
        "discovered": [name for name in discovered if name],
        "mapped": sorted(selected),
        "unmapped": sorted(name for name in FUYAO_OPERATIONS if name not in selected),
    }


def _invoke_tool(tool: Any, kwargs: dict[str, Any]) -> Any:
    if hasattr(tool, "ainvoke"):
        return tool.ainvoke(kwargs)
    if hasattr(tool, "invoke"):
        return tool.invoke(kwargs)
    if callable(tool):
        return tool(**kwargs)
    raise TypeError(f"unsupported tool {type(tool).__name__}")


def _tool_arguments(tool_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """将内部参数转换成已实测 Fuyao 工具 schema。

    只对明确识别出的真实工具名转换；旧网关和测试替身继续使用其原有参数，
    避免在兼容分支里猜测 schema。
    """
    arguments = dict(kwargs)
    if tool_name in {
        "get_a_share_prices_snapshot",
        "get_a_share_index_prices_snapshot",
        "get_a_share_valuations_snapshot",
    }:
        value = arguments.pop("thscode", arguments.get("thscodes", None))
        if value is not None:
            if isinstance(value, (list, tuple, set)):
                arguments["thscodes"] = ",".join(str(item) for item in value)
            else:
                arguments["thscodes"] = str(value)
    elif tool_name == "get_meta_tickers_search":
        value = arguments.pop("query", arguments.get("q", None))
        if value is not None:
            arguments["q"] = str(value)
    elif tool_name == "get_meta_tickers_list":
        arguments.setdefault("asset_type", "a-share-index")
    return arguments


class FuyaoClient:
    """按内部操作名调用。返回信封，永不抛出密钥。"""

    def __init__(self, operations: dict[str, Operation] | None = None,
                 *, diagnostics: dict[str, Any] | None = None) -> None:
        self._operations = dict(operations or {})
        self.diagnostics = dict(diagnostics or {})

    @property
    def operations(self) -> list[str]:
        return sorted(self._operations)

    @property
    def unmapped(self) -> list[str]:
        """上游没有提供、因而无法调用的内部操作。"""
        return list(self.diagnostics.get("unmapped") or [])

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        operation = self._operations.get(name)
        if operation is None:
            return wrap_envelope(RuntimeError(f"{name} unavailable"), code="unavailable")
        try:
            loaded = operation(**kwargs)
            if hasattr(loaded, "__await__"):
                loaded = await loaded
            envelope = wrap_envelope(loaded)
            if envelope["ok"]:
                envelope["data"] = normalize_payload(name, envelope["data"])
                data = envelope["data"]
                # 内部 quote 契约是一只标的；Fuyao 的 snapshot API 即使传入一只
                # 也会返回列表。这里收窄为单行，避免把 MCP 批量外壳泄漏到业务图。
                if name == "quote" and isinstance(data, list):
                    envelope["data"] = data[0] if len(data) == 1 else None
                    data = envelope["data"]
                if isinstance(data, dict) and data.get("as_of"):
                    envelope["as_of"] = data["as_of"]
            return envelope
        except Exception as exc:  # noqa: BLE001 - 上游失败收成信封
            logger.warning("fuyao_call_failed op=%s reason=%s", name, sanitize_error(exc))
            return wrap_envelope(exc)


async def load_fuyao_client(api_key: str) -> tuple[FuyaoClient | None, str]:
    """未配置、上游失败或工具名对不上时返回警告码，不把工具交给模型。

    工具名对不上必须当成受控失败上报：否则会拿到一个"发现成功但每次调用都失败"
    的客户端，真实原因（映射缺失）会被误读成上游故障。
    """
    tools, warning = await load_stock_mcp_tools(api_key)
    if warning:
        return None, warning
    loaded_tools = list(tools or [])
    diagnostics = whitelist_diagnostics(loaded_tools)
    selected = select_whitelisted_tools(loaded_tools)
    if not selected:
        logger.warning(
            "fuyao_tools_unmapped discovered=%s expected=%s",
            diagnostics["discovered"],
            diagnostics["unmapped"],
        )
        return None, "fuyao_tools_unmapped"
    if diagnostics["unmapped"]:
        logger.warning(
            "fuyao_tools_partial unmapped=%s discovered=%s",
            diagnostics["unmapped"],
            diagnostics["discovered"],
        )

    async def _bound(tool: Any, **kwargs: Any) -> Any:
        loaded = _invoke_tool(tool, kwargs)
        if hasattr(loaded, "__await__"):
            loaded = await loaded
        return loaded

    def _make_op(tool: Any) -> Operation:
        tool_name = str(getattr(tool, "name", "") or "")

        async def _call(**kwargs: Any) -> Any:
            return await _bound(tool, **_tool_arguments(tool_name, kwargs))

        return _call

    operations = {name: _make_op(tool) for name, tool in selected.items()}
    client = FuyaoClient(operations, diagnostics=diagnostics)
    return client, "fuyao_tools_partial" if diagnostics["unmapped"] else ""


__all__ = [
    "FUYAO_OPERATIONS",
    "FuyaoClient",
    "load_fuyao_client",
    "operation_for_tool_name",
    "select_whitelisted_tools",
    "whitelist_diagnostics",
]
