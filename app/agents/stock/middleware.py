"""MCP tool 结果落库中间件：模型只看摘要和 evidence_id。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from app.agents.stock.evidence_store import compact_evidence_preview
from app.agents.stock.tools import current_stock_execution
from app.mcp.fuyao import operation_for_tool_name
from app.mcp.fuyao_dto import normalize_payload


def _evidence_type(tool_name: str) -> str:
    operation = operation_for_tool_name(tool_name)
    mapped = {
        "quote": "quote",
        "kline": "kline",
        "financials": "financials",
        "valuation": "financials",
        "trade_calendar": "calendar",
        "market_breadth": "market_auxiliary",
        "sector_quote": "market_auxiliary",
        "search_security": "security_meta",
        "list_indices": "security_meta",
        "list_industries": "security_meta",
        "company_profile": "security_meta",
        "index_members": "index_members",
    }
    if operation:
        return mapped.get(operation, "mcp_result")
    name = tool_name.lower()
    if any(part in name for part in ("history", "historical", "kline", "candlestick")):
        return "kline"
    if any(part in name for part in ("financial", "balance", "income", "cashflow", "valuation")):
        return "financials"
    if any(part in name for part in ("calendar", "trade_date")):
        return "calendar"
    if any(part in name for part in ("breadth", "special", "sector", "industry", "concept")):
        return "market_auxiliary"
    if any(part in name for part in ("meta", "ticker", "search", "profile")):
        return "security_meta"
    if any(part in name for part in ("price", "quote", "snapshot")):
        return "quote"
    return "mcp_result"


def _payload(value: Any) -> Any:
    artifact = getattr(value, "artifact", None)
    if artifact is not None:
        if hasattr(artifact, "model_dump"):
            artifact = artifact.model_dump()
        if isinstance(artifact, (dict, list)):
            if isinstance(artifact, dict) and "structured_content" in artifact:
                return artifact.get("structured_content")
            return artifact
    content = getattr(value, "content", value)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (TypeError, ValueError):
            return {"text": content}
    if isinstance(content, list):
        decoded = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                continue
            try:
                decoded.append(json.loads(block["text"]))
            except (TypeError, ValueError):
                continue
        if len(decoded) == 1:
            return decoded[0]
        if decoded:
            return decoded
    return content


def _source(tool_name: str) -> str:
    return tool_name.split("_", 1)[0] if "_" in tool_name else "mcp"


def _as_of(payload: Any) -> str:
    if isinstance(payload, dict):
        direct = str(payload.get("as_of") or payload.get("update_time") or payload.get("timestamp") or payload.get("published_at") or "")
        if direct:
            return direct
        return max((_as_of(item) for item in payload.values()), default="")
    if isinstance(payload, list):
        return max((_as_of(item) for item in payload), default="")
    return ""


def _single_symbol(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    value = arguments.get("thscode", arguments.get("thscodes", arguments.get("ts_code", "")))
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value if str(item)]
    else:
        values = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return values[0] if len(values) == 1 else ""


def _normalize(tool_name: str, raw: Any, arguments: Any) -> Any:
    operation = operation_for_tool_name(tool_name)
    normalized = normalize_payload(operation, raw) if operation else raw
    symbol = _single_symbol(arguments)
    if isinstance(normalized, list):
        payload: Any = {"data": normalized}
    elif isinstance(normalized, dict):
        payload = dict(normalized)
    else:
        payload = normalized
    if symbol and isinstance(payload, dict) and not payload.get("thscode"):
        payload["thscode"] = symbol
    return payload


def persist_tool_output(
    store: Any,
    context: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: Any,
    result: Any,
) -> dict[str, Any]:
    payload = _normalize(tool_name, _payload(result), arguments)
    row = store.put(
        context,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        evidence_type=_evidence_type(tool_name),
        payload=payload,
        source=_source(tool_name),
        as_of=_as_of(payload),
        arguments=arguments,
    )
    return compact_evidence_preview(row["payload"], row["id"])


def build_evidence_middleware(store: Any, mcp_tool_names: set[str]) -> Any:
    @wrap_tool_call(name="StockEvidenceMiddleware")
    async def capture(request: Any, handler: Any) -> Any:
        result = await handler(request)
        name = str(request.tool_call.get("name") or "")
        if name not in mcp_tool_names:
            return result
        execution = current_stock_execution()
        preview = await asyncio.to_thread(
            persist_tool_output,
            store,
            execution.context,
            tool_name=name,
            tool_call_id=str(request.tool_call.get("id") or ""),
            arguments=request.tool_call.get("args") or {},
            result=result,
        )
        return ToolMessage(
            content=json.dumps(preview, ensure_ascii=False, default=str),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=name,
            status=getattr(result, "status", "success"),
        )

    return capture


__all__ = ["build_evidence_middleware", "persist_tool_output"]
