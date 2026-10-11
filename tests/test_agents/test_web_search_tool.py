"""web_search：东财补充数据工具落库、降级与数据集清单。"""

from __future__ import annotations

import asyncio

from app.agents.stock.evidence_store import EvidenceContext, MemoryAgentEvidenceStore
from app.agents.stock.tools import bind_stock_execution, build_stock_local_tools, reset_stock_execution


class _FakeEastmoney:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, **params):
        self.calls.append((name, params))
        if not self.ok:
            return {"ok": False, "code": "unavailable", "data": None, "warning": "boom", "as_of": ""}
        return {"ok": True, "code": "", "data": {"up": 3347, "down": 2159, "unchanged": 130,
            "source": "eastmoney", "as_of": "2026-10-09T15:00:00+08:00"},
            "as_of": "2026-10-09T15:00:00+08:00", "warning": ""}


def _run_tool(store, ctx, client, arguments):
    tool = next(item for item in build_stock_local_tools(store=store, eastmoney=client)
                if item.name == "web_search")

    async def call():
        token = bind_stock_execution(ctx)
        try:
            return await tool.ainvoke(arguments)
        finally:
            reset_stock_execution(token)

    return tool, asyncio.run(call())


def test_web_search_persists_auxiliary_evidence():
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    client = _FakeEastmoney()
    tool, preview = _run_tool(store, ctx, client, {"dataset": "market_breadth", "top": 5})
    row = store.get(preview["evidence_id"], ctx)
    assert row["evidence_type"] == "market_auxiliary"
    assert row["source"] == "eastmoney"
    assert row["as_of"] == "2026-10-09T15:00:00+08:00"
    assert row["payload"]["data"]["up"] == 3347
    assert client.calls == [("market_breadth", {"top": 5})]
    # 数据集清单自动同步进工具描述。
    assert "market_breadth" in tool.description and "sector_overview" in tool.description


def test_web_search_degrades_without_inventing_data():
    store = MemoryAgentEvidenceStore()
    ctx = EvidenceContext("r", "s", "c")
    _, preview = _run_tool(store, ctx, _FakeEastmoney(ok=False), {"dataset": "sector_overview"})
    row = store.get(preview["evidence_id"], ctx)
    assert row["payload"]["failed"] is True
    assert row["payload"]["data"] is None
    assert row["payload"]["warning"] == "upstream_data_unavailable"
    from app.agents.public_warnings import public_warnings
    assert public_warnings([row["payload"]["warning"]]) == ["数据源暂未返回有效数据。"]
