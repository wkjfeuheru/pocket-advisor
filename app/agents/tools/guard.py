"""MCP 工具安全守卫。动态工具以 fail-closed 方式注册。"""

from __future__ import annotations

from collections import Counter
from typing import Any

_FORBIDDEN_PARTS = (
    "deposit",
    "recharge",
    "liquidate",
    "redeem",
    "withdraw",
    "transfer",
    "buy",
    "sell",
    "cancel_order",
    "update",
    "delete",
    "insert",
    "write",
    "shell",
    "command",
    "exec",
    "place_order",
    "create_order",
    "submit_order",
    "execute_sql",
    "run_sql",
    "raw_sql",
)
_FORBIDDEN_CN = ("充值", "下单", "赎回", "清仓", "买入", "卖出", "转账", "撤单", "写入", "删除")
_DANGEROUS_SCHEMA_PARTS = {
    "sql", "query_sql", "statement", "command", "cmd", "shell", "script",
    "order", "order_id", "side", "quantity", "amount", "account", "account_id",
    "customer_id", "password", "token", "api_key", "secret",
}


def is_forbidden_tool(name: str) -> bool:
    lowered = (name or "").lower()
    if any(part in lowered for part in _FORBIDDEN_PARTS):
        return True
    return any(word in (name or "") for word in _FORBIDDEN_CN)


def select_readonly_tools(tools: list[Any]) -> list[Any]:
    """丢掉写账户或执行 SQL 的工具，保留其余只读工具。"""
    return [tool for tool in tools if not is_forbidden_tool(getattr(tool, "name", ""))]


def _schema_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {str(key).lower() for key in value}
        for child in value.values():
            found.update(_schema_keys(child))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for child in value:
            found.update(_schema_keys(child))
        return found
    return set()


def _metadata(tool: Any) -> dict[str, Any]:
    value = getattr(tool, "metadata", None)
    return dict(value) if isinstance(value, dict) else {}


def _tool_schema(tool: Any) -> Any:
    args = getattr(tool, "args", None)
    if args:
        return args
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return schema
    if schema is not None and hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return {}


def _decision(tool: Any, allowed_server_prefixes: tuple[str, ...]) -> str:
    name = str(getattr(tool, "name", "") or "")
    if not name or is_forbidden_tool(name):
        return "dangerous_name"
    if allowed_server_prefixes and not any(name.startswith(prefix) for prefix in allowed_server_prefixes):
        return "untrusted_server"
    metadata = _metadata(tool)
    if metadata.get("destructiveHint") is True or metadata.get("destructive_hint") is True:
        return "destructive_annotation"
    readonly = metadata.get("readOnlyHint", metadata.get("read_only_hint"))
    if readonly is not True:
        return "missing_readonly_annotation"
    if _schema_keys(_tool_schema(tool)) & _DANGEROUS_SCHEMA_PARTS:
        return "dangerous_schema"
    return ""


def filter_mcp_tools(
    tools: list[Any],
    *,
    allowed_server_prefixes: tuple[str, ...],
) -> tuple[list[Any], dict[str, str]]:
    """对动态 MCP tools 做来源、注解、schema 和重名检查。"""
    counts = Counter(str(getattr(tool, "name", "") or "") for tool in tools)
    accepted: list[Any] = []
    rejected: dict[str, str] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if counts[name] > 1:
            rejected[name] = "duplicate_name"
            continue
        reason = _decision(tool, allowed_server_prefixes)
        if reason:
            rejected[name] = reason
        else:
            accepted.append(tool)
    return accepted, rejected


__all__ = ["filter_mcp_tools", "is_forbidden_tool", "select_readonly_tools"]
