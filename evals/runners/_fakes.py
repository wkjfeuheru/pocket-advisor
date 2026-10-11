"""评测共享的离线替身：脚本化模型、假专业分支、内存存储与假行情。

语义对齐 ``tests/test_agents`` 的既有替身，但独立于 tests 包，
保证 ``python -m evals.runners.*`` 不依赖 pytest。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.agents.graphs.state import (
    ChatResult,
    IntentResult,
    PortfolioResult,
    ProductResult,
    RewrittenQuery,
    StockResult,
    AnswerPlan,
    PlanPoint,
    PlanSection,
    SupervisorDecision,
)
from tests.test_agents.memory_store import MemoryAdvisorStore  # 复用语义对齐的存储替身


class ScriptedModel:
    """按脚本返回结构化意图/改写结果与聚合文本，用于离线驱动主图。"""

    def __init__(
        self,
        intent: IntentResult,
        answers: list[str],
        rewritten: RewrittenQuery | None = None,
        *,
        classify_error: Exception | None = None,
        rewrite_error: Exception | None = None,
        slot_values: dict | None = None,
    ) -> None:
        self.intent = intent
        self.answers = list(answers)
        self.rewritten = rewritten
        self.classify_error = classify_error
        self.rewrite_error = rewrite_error
        self.slot_values = slot_values or {}
        self.structured_method = None
        self.compliance_inputs: list = []
        self.rewrite_inputs: list = []
        self.classify_inputs: list = []
        #: 骨架阶段确定的成文内容：可见阶段（astream）据此产出，避免重复消费脚本答案。
        self.current_prose = ""

    def with_structured_output(self, schema, *, method=None, **kwargs):
        self.structured_method = method
        parent = self

        class _Structured:
            async def ainvoke(self, messages, config=None):
                from app.agents.slots import SlotExtraction, SlotPatch, _fallback_patch
                if schema is SlotExtraction:
                    import json
                    data = json.loads(messages[-1]["content"])
                    domain = next(name for name in ("stock_agent", "product_agent", "portfolio_agent") if name in messages[0]["content"])
                    if domain in parent.slot_values:
                        return SlotExtraction(domain=domain, patch=SlotPatch(set_values=parent.slot_values[domain]))
                    return _fallback_patch(domain, data["query"], "eval")
                if schema is SupervisorDecision:
                    return SupervisorDecision()
                if schema is AnswerPlan:
                    parent.compliance_inputs.append(list(messages))
                    text = parent.answers.pop(0) if parent.answers else "没有更多材料。"
                    from app.agents.graphs.task_agent import parse_recommendations
                    prose = text.split("推荐代码:")[0].strip()
                    parent.current_prose = prose
                    sections = [PlanSection(points=[PlanPoint(point=prose)])] if prose else []
                    return AnswerPlan(sections=sections, recommendations=parse_recommendations(text))
                if schema is RewrittenQuery:
                    parent.rewrite_inputs.append(list(messages))
                    if parent.rewrite_error is not None:
                        raise parent.rewrite_error
                    return parent.rewritten
                parent.classify_inputs.append(list(messages))
                if parent.classify_error is not None:
                    raise parent.classify_error
                return parent.intent

        return _Structured()

    async def astream(self, messages, config=None):
        self.compliance_inputs.append(messages)
        # 有骨架时按骨架成文；未走骨架（例如 legacy 路径）则回退到脚本答案。
        text = self.current_prose or (self.answers.pop(0) if self.answers else "没有更多材料。")
        yield SimpleNamespace(content=text)


def fake_specialist(name: str, *, content: str = "", codes: list[str] | None = None,
                    error: Exception | None = None, warning: str = "",
                    failed: bool = False, candidates: list[dict] | None = None,
                    stock_result: Any = None, delay: float = 0.0):
    """按 agent 名产出对应类型的结构化结果，语义对齐 tests 的 ``_specialist``。"""

    async def run(task: str, state: dict):
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        status = "failed" if failed else "ok"
        summary = content or f"{name}:{task}"
        warnings = [warning] if warning else []
        common = {"status": status, "summary": summary, "warnings": warnings,
                  "source": "fake", "as_of": "2026-09-28T00:00:00Z"}
        if name == "stock_agent":
            from app.agents.stock.contracts import CandidateStock

            return StockResult(
                **common,
                result=stock_result,
                candidates=[CandidateStock.model_validate(item) for item in (candidates or [])],
            )
        if name == "product_agent":
            return ProductResult(**common, recommended_codes=list(codes or []))
        if name == "portfolio_agent":
            return PortfolioResult(**common, recommended_codes=list(codes or []))
        return ChatResult(**common)

    return run


def full_specialists(**overrides: Any) -> dict:
    specialists = {
        "stock_agent": fake_specialist("stock_agent"),
        "product_agent": fake_specialist("product_agent"),
        "portfolio_agent": fake_specialist("portfolio_agent"),
        "chat_agent": fake_specialist("chat_agent"),
    }
    specialists.update(overrides)
    return specialists


def build_eval_graph(model: ScriptedModel, specialists: dict | None = None, **kwargs: Any):
    """离线编译主图：内存 checkpointer + 无风险约束（除非显式传入）。"""
    from app.agents.graphs.task_agent import build_advisor_graph

    return build_advisor_graph(
        model=model,
        specialists=specialists or full_specialists(),
        checkpointer=kwargs.pop("checkpointer", MemorySaver()),
        risk_of=kwargs.pop("risk_of", None) or (lambda _code: None),
        **kwargs,
    )


def interrupt_value(snapshot) -> dict:
    """从 aget_state 快照里取 interrupt 载荷。"""
    direct = getattr(snapshot, "interrupts", None) or ()
    if direct:
        return getattr(direct[0], "value", direct[0])
    for task in getattr(snapshot, "tasks", ()) or ():
        found = getattr(task, "interrupts", None) or ()
        if found:
            return getattr(found[0], "value", found[0])
    raise AssertionError("expected interrupt")


# ---------------------------------------------------------------------------
# 股票分支替身：假行情操作 + 内存证据库 + 内存快照仓库
# ---------------------------------------------------------------------------

_FAKE_INDEX_ROWS = [
    {"thscode": "000001.SH", "name": "上证指数", "last": 3200.5, "price": 3200.5,
     "price_change_ratio_pct": 0.8, "change_pct": 0.8},
    {"thscode": "399001.SZ", "name": "深证成指", "last": 10450.2, "price": 10450.2,
     "price_change_ratio_pct": -0.3, "change_pct": -0.3},
]


class FakeFuyaoOperations:
    """按 Fuyao 内部操作名提供确定性数据，替代 MCP 网关。"""

    async def search_security(self, **_kwargs):
        return {"ok": True, "data": [
            {"thscode": "600519.SH", "name": "贵州茅台", "asset_type": "stock"},
            {"thscode": "000858.SZ", "name": "五粮液", "asset_type": "stock"},
        ]}

    async def list_indices(self, **_kwargs):
        return {"ok": True, "data": [{"thscode": "000001.SH", "name": "上证指数"}]}

    async def list_industries(self, **_kwargs):
        return {"ok": True, "data": [{"industry": "白酒"}]}

    async def quote(self, thscode: str = "", **_kwargs):
        row = next((item for item in _FAKE_INDEX_ROWS if item["thscode"] == thscode), None)
        if row is None:
            row = {"thscode": thscode, "last": 100.0, "price": 100.0,
                   "price_change_ratio_pct": 1.2, "change_pct": 1.2}
        return {"ok": True, "data": row}

    async def kline(self, thscode: str = "", **_kwargs):
        return {"ok": True, "data": [
            {"close": 100 + index, "high": 101 + index, "low": 99 + index,
             "volume": 1000 + index, "as_of": f"2026-09-{index + 1:02d}"}
            for index in range(20)
        ]}

    async def financials(self, **_kwargs):
        return {"ok": True, "data": {"roe": 0.25, "gross_margin": 0.9,
                                     "cashflow_quality": 0.8, "revenue_trend": "up"}}

    async def valuation(self, **_kwargs):
        return {"ok": True, "data": {"pe": 22.5, "pb": 8.1, "industry_percentile": 0.4}}

    async def market_breadth(self, **_kwargs):
        return {"ok": True, "data": {"up": 2800, "down": 2100}}

    async def sector_quote(self, **_kwargs):
        return {"ok": True, "data": {"industry": [{"industry": "白酒", "change_pct": 1.5}]}}

    async def trade_calendar(self, **_kwargs):
        return {"ok": True, "data": {"session": "open", "is_open": True,
                                     "as_of": "2026-09-28"}}

    async def company_profile(self, **_kwargs):
        return {"ok": True, "data": {"thscode": "600519.SH", "name": "贵州茅台"}}

    async def index_members(self, **_kwargs):
        return {"ok": True, "data": {"thscode": "000300.SH",
                                     "members": ["000001.SZ", "600000.SH"]}}
