"""商品与账户的只读工具。客户身份来自运行时 config，不来自模型参数。"""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

_library: ContextVar[Any] = ContextVar("advisor_product_library", default=None)
_portfolio: ContextVar[Any] = ContextVar("advisor_portfolio_service", default=None)


def bind_readonly_services(library: Any, portfolio: Any) -> tuple[Any, Any]:
    """在一次受信任的调用期间注入只读服务。返回重置令牌。"""
    return _library.set(library), _portfolio.set(portfolio)


def reset_readonly_services(tokens: tuple[Any, Any]) -> None:
    _library.reset(tokens[0])
    _portfolio.reset(tokens[1])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _unavailable(source: str, warning: str) -> str:
    return _dump({
        "source": source,
        "as_of": _now(),
        "data": None,
        "warning": warning,
    })


def _customer_id(config: RunnableConfig) -> str:
    customer_id = str((config.get("configurable") or {}).get("customer_id") or "").strip()
    if not customer_id:
        raise RuntimeError("缺少受信任的客户上下文")
    return customer_id.upper()


@tool
def list_products(product_type: str = "fund") -> str:
    """列出上架商品。product_type 取 fund（基金）、etf（ETF）或 wealth（理财产品）。"""
    library = _library.get()
    if library is None:
        return _unavailable("product_library", "商品库不可用，未编造商品数据")
    summaries = library.list_products(product_type or "fund")
    items = []
    for summary in summaries:
        code = str(summary.get("code") or "")
        detail = library.query_by_code(code) if code else None
        basic = (detail or {}).get("basic_info") or {}
        items.append({
            "code": code,
            "name": basic.get("name") or summary.get("name") or "",
            "risk_level": basic.get("risk_level") or "",
            "is_active": summary.get("is_active", True),
        })
    return _dump({"source": "postgresql", "as_of": _now(), "data": items, "warning": ""})


@tool
def get_product_detail(product_code: str) -> str:
    """按商品代码读取详情。代码不存在时返回警告，不编造商品。"""
    library = _library.get()
    if library is None:
        return _unavailable("product_library", "商品库不可用，未编造商品数据")
    detail = library.query_by_code(str(product_code or "").strip())
    if detail is None:
        return _unavailable("postgresql", "商品不存在或尚未披露，未编造详情")
    basic = detail.get("basic_info") or {}
    return _dump({
        "source": "postgresql",
        "as_of": str(basic.get("as_of") or _now()),
        "data": basic,
        "warning": "",
    })


@tool
def get_product_performance(product_code: str) -> str:
    """读取商品业绩与净值。没有业绩时明确警告。"""
    library = _library.get()
    if library is None:
        return _unavailable("product_library", "商品库不可用，未编造业绩")
    detail = library.query_by_code(str(product_code or "").strip())
    performance = (detail or {}).get("performance") or {}
    if not performance or performance.get("nav") is None and not performance.get("update_date"):
        return _unavailable("postgresql", "没有可用业绩，未编造收益或净值")
    return _dump({
        "source": str(performance.get("source") or "postgresql"),
        "as_of": str(performance.get("as_of") or performance.get("update_date") or ""),
        "data": performance,
        "warning": "" if performance.get("nav") is not None else "净值缺失，未编造净值",
    })


@tool
def get_product_holdings(product_code: str) -> str:
    """读取商品重仓。没有披露时返回空列表和警告。"""
    library = _library.get()
    if library is None or not hasattr(library, "list_holdings"):
        return _unavailable("product_library", "重仓不可用，未编造持仓")
    rows = library.list_holdings(str(product_code or "").strip()) or []
    as_of = str(rows[0].get("as_of") or "") if rows else ""
    warning = "" if rows else "没有披露重仓，未编造持仓"
    return _dump({"source": "postgresql", "as_of": as_of, "data": rows, "warning": warning})


@tool
def get_account_snapshot(config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """读取当前登录用户的模拟账户。不能指定其他客户。"""
    portfolio = _portfolio.get()
    if portfolio is None:
        return _unavailable("portfolio", "账户不可用，未编造资产")
    try:
        customer_id = _customer_id(config)
    except RuntimeError:
        return _unavailable("portfolio", "缺少受信任的客户上下文，未读取账户")
    account = portfolio.get_account(customer_id)
    payload = account.model_dump(mode="json") if hasattr(account, "model_dump") else account
    return _dump({
        "source": "postgresql",
        "as_of": str((payload or {}).get("updated_at") or _now()),
        "data": payload,
        "warning": "",
    })


@tool
def list_positions(config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """读取当前登录用户的模拟持仓。不能指定其他客户。"""
    portfolio = _portfolio.get()
    if portfolio is None:
        return _unavailable("portfolio", "持仓不可用，未编造仓位")
    try:
        customer_id = _customer_id(config)
    except RuntimeError:
        return _unavailable("portfolio", "缺少受信任的客户上下文，未读取持仓")
    rows = portfolio.list_positions(customer_id)
    data = [row.model_dump(mode="json") if hasattr(row, "model_dump") else row for row in rows]
    return _dump({"source": "postgresql", "as_of": _now(), "data": data, "warning": ""})


TOOLS = [
    list_products,
    get_product_detail,
    get_product_performance,
    get_product_holdings,
    get_account_snapshot,
    list_positions,
]

__all__ = [
    "TOOLS",
    "bind_readonly_services",
    "get_account_snapshot",
    "get_product_detail",
    "get_product_holdings",
    "get_product_performance",
    "list_positions",
    "list_products",
    "reset_readonly_services",
]
