"""通用任务执行上下文与逐字段事实核验，身份仅由受信任运行时提供。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.stock.evidence_store import EvidenceAccessError, EvidenceContext


class NeedsClarification(Exception):
    """工作代理请求澄清，主图负责统一挂起。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("reason") or "needs_clarification"))
        self.payload = dict(payload)


class FactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    evidence_id: str
    field_path: str
    subject: str
    metric: str
    value: Any
    unit: str = ""
    as_of: str


def verify_fact(store: Any, context: EvidenceContext, reference: FactReference | dict) -> FactReference:
    """核对字段、所属实体、指标、单位及时间，不能凭相同数字跨实体引用。"""
    fact = FactReference.model_validate(reference)
    row = store.get(fact.evidence_id, context)
    value = row["payload"]
    ancestors: list[dict] = []
    path = fact.field_path.split(".")
    try:
        for part in path:
            if isinstance(value, dict):
                ancestors.append(value)
                value = value[part]
            elif isinstance(value, list):
                value = value[int(part)]
            else:
                raise KeyError(part)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise EvidenceAccessError("invalid_fact_path") from exc
    if isinstance(value, (dict, list)) or isinstance(value, bool) != isinstance(fact.value, bool) or value != fact.value:
        raise EvidenceAccessError("fact_value_mismatch")
    entity = next((str(parent[key]) for parent in reversed(ancestors)
                   for key in ("subject", "code", "product_code", "thscode", "ts_code", "symbol", "customer_id")
                   if parent.get(key) is not None), "")
    metric = str(ancestors[-1].get("metric") or path[-1]) if ancestors else path[-1]
    unit = next((str(parent.get("unit") or (parent.get("units") or {}).get(metric))
                 for parent in reversed(ancestors)
                 if parent.get("unit") or isinstance(parent.get("units"), dict) and parent["units"].get(metric)), "")
    as_of = next((str(parent.get("as_of") or parent.get("update_date"))
                  for parent in reversed(ancestors) if parent.get("as_of") or parent.get("update_date")),
                 str(row.get("as_of") or ""))
    if not fact.subject or fact.subject != entity or fact.metric != metric:
        raise EvidenceAccessError("fact_association_mismatch")
    if fact.unit != unit or not fact.as_of or fact.as_of != as_of:
        raise EvidenceAccessError("fact_metadata_mismatch")
    return fact


def facts_from_evidence(store: Any, context: EvidenceContext, evidence_ids: list[str], *, limit: int = 100) -> list[FactReference]:
    """从真实字段生成事实；不读取模型声称的数值，不展示原始长序列。"""
    facts: list[FactReference] = []
    for evidence_id in dict.fromkeys(evidence_ids):
        row = store.get(evidence_id, context)
        if not row.get("source") or not row.get("as_of") or row.get("evidence_type") in {"kline", "calendar", "security_meta"}:
            continue

        def visit(value, path, subject="", as_of="", units=None, unit=""):
            if len(facts) >= limit:
                return
            if isinstance(value, dict):
                subject = next((str(value[key]) for key in ("subject", "code", "product_code", "thscode", "ts_code", "symbol", "customer_id")
                                if value.get(key) is not None), subject)
                as_of = str(value.get("as_of") or value.get("update_date") or as_of)
                units = value.get("units") if isinstance(value.get("units"), dict) else units
                unit = str(value.get("unit") or unit)
                for key, item in value.items():
                    visit(item, [*path, str(key)], subject, as_of, units, unit)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, [*path, str(index)], subject, as_of, units, unit)
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and subject and path:
                fact = FactReference(id=f"{evidence_id}:{'.'.join(path)}", evidence_id=evidence_id,
                                     field_path=".".join(path), subject=subject, metric=path[-1], value=value,
                                     unit=unit or str((units or {}).get(path[-1]) or ""), as_of=as_of)
                try:
                    facts.append(verify_fact(store, context, fact))
                except EvidenceAccessError:
                    pass

        visit(row["payload"], [], as_of=str(row["as_of"]))
    return facts


@dataclass
class Execution:
    store: Any
    context: EvidenceContext
    rows: list[dict[str, Any]] = field(default_factory=list)
    pending_clarification: dict[str, Any] | None = None
    domain_slots: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    product_codes: set[str] = field(default_factory=set)
    agent: str = ""


_EXECUTION: ContextVar[Execution | None] = ContextVar("worker_evidence_execution", default=None)


def bind_execution(store: Any, context: EvidenceContext, *, domain_slots: dict | None = None, profile: dict | None = None, agent: str = "") -> Token:
    return _EXECUTION.set(Execution(store=store, context=context, domain_slots=dict(domain_slots or {}), profile=dict(profile or {}), agent=agent))


def reset_execution(token: Token) -> None:
    _EXECUTION.reset(token)


def current_execution() -> Execution | None:
    return _EXECUTION.get()


def product_access_allowed(product_code: str) -> bool:
    execution = current_execution()
    if execution is None:
        return True
    slots = execution.domain_slots
    fixed = {str(code) for code in [*(slots.get("codes") or []), *(slots.get("symbols") or [])]}
    if fixed:
        return product_code in fixed
    if execution.agent == "portfolio_agent":
        return product_code in execution.product_codes
    if slots.get("operation") in {"query", "diagnose"} and execution.product_codes:
        return product_code in execution.product_codes
    return True


def validate_tool_arguments(tool_name: str, arguments: dict) -> str:
    """MCP 和本地股票工具都不能覆盖主图已经确认的范围。"""
    execution = current_execution()
    if execution is None:
        return ""
    from app.mcp.fuyao import operation_for_tool_name
    operation = operation_for_tool_name(tool_name)
    slots = execution.domain_slots
    if operation in {"search_security", "list_indices", "list_industries", "trade_calendar"}:
        return ""
    allowed = {str(item).upper() for item in [*(slots.get("symbols") or []), *(slots.get("index_scope") or [])]}
    raw = next((arguments[key] for key in ("thscode", "thscodes", "ts_code", "symbol", "symbols", "code", "codes")
                if arguments.get(key)), None)
    if raw is not None:
        requested = {str(item).strip().upper() for item in raw} if isinstance(raw, list) else set(str(raw).upper().replace(" ", "").split(","))
        if allowed and not requested <= allowed:
            return "symbol_outside_confirmed_slots"
    elif allowed and operation in {"quote", "kline", "financials", "valuation", "company_profile", "index_members"}:
        return "missing_scoped_symbol"
    timeframe = slots.get("timeframe")
    if timeframe and operation == "kline":
        def day(value):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            if str(value or "").isdigit() and len(str(value)) in {10, 13}:
                return datetime.fromtimestamp(int(value) / (1000 if len(str(value)) == 13 else 1), ZoneInfo("Asia/Shanghai")).date().isoformat()
            text = str(value or "")[:10]
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text
        start = next((arguments[key] for key in ("start_date", "begin_date", "start_time", "start") if arguments.get(key)), None)
        end = next((arguments[key] for key in ("end_date", "end_time", "end") if arguments.get(key)), None)
        if start is None or end is None or day(start) < timeframe["start"] or day(end) > timeframe["end"] or day(start) > day(end):
            return "timeframe_outside_confirmed_slots"
    return ""


def capture_evidence(payload: dict[str, Any], *, tool_name: str, arguments: Any = None) -> dict[str, Any]:
    execution = current_execution()
    if execution is None:
        return payload
    if execution.store is None:
        return {**payload, "warning": payload.get("warning") or "evidence_store_unavailable"}
    row = execution.store.put(execution.context, tool_call_id=f"local:{tool_name}:{len(execution.rows)}",
                              tool_name=tool_name, evidence_type="readonly_tool", payload=payload,
                              source=str(payload.get("source") or ""), as_of=str(payload.get("as_of") or ""),
                              arguments=arguments)
    execution.rows.append(row)
    if isinstance(payload.get("data"), list) and tool_name in {"list_products", "list_positions"}:
        execution.product_codes.update(str(item.get("code") or item.get("product_code"))
                                       for item in payload["data"] if isinstance(item, dict) and (item.get("code") or item.get("product_code")))
    return {**payload, "evidence_id": row["id"]}
