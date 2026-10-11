"""东方财富补充数据源：只读、可降级、字段口径正确。"""

from __future__ import annotations

import asyncio

import httpx

from app.mcp.eastmoney import EastmoneyClient, operation_catalog

_BREADTH_BODY = {
    "rc": 0,
    "data": {"total": 3, "diff": [
        {"f12": "000001", "f14": "上证指数", "f104": 1341, "f105": 956, "f106": 60, "f124": 1791533520},
        {"f12": "399001", "f14": "深证成指", "f104": 1698, "f105": 1166, "f106": 66, "f124": 1791533469},
        {"f12": "899050", "f14": "北证50", "f104": 308, "f105": 37, "f106": 4, "f124": 1791531345},
    ]},
}
_SECTOR_GAINERS = {
    "rc": 0,
    "data": {"total": 496, "diff": [
        {"f12": "BK1296", "f14": "视频媒体", "f3": 19.98, "f124": 1791531570},
        {"f12": "BK1298", "f14": "文字媒体", "f3": 15.0, "f124": 1791531570},
    ]},
}
_SECTOR_LOSERS = {
    "rc": 0,
    "data": {"total": 496, "diff": [
        {"f12": "BK1340", "f14": "印制电路板", "f3": -4.06, "f124": 1791531570},
        {"f12": "BK0459", "f14": "元件", "f3": -3.67, "f124": 1791531570},
    ]},
}


def _transport(*, fail: bool = False, seen: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if fail:
            return httpx.Response(500, text="boom")
        path = request.url.path
        if path.endswith("/ulist.np/get"):
            return httpx.Response(200, json=_BREADTH_BODY)
        if path.endswith("/clist/get"):
            # 用原始 URL 判定板块：httpx 的 QueryParams 会把 ``+`` 解码成空格，不能拿它比对。
            raw = str(request.url)
            is_concept = "t:3" in raw
            gainers, losers = _SECTOR_GAINERS, _SECTOR_LOSERS
            if is_concept:
                gainers = {"rc": 0, "data": {"total": 506, "diff": [
                    {"f14": "AI语料", "f3": 6.07, "f124": 1791531570},
                    {"f14": "短剧互动游戏", "f3": 5.98, "f124": 1791531570}]}}
                losers = {"rc": 0, "data": {"total": 506, "diff": [
                    {"f14": "煤炭概念", "f3": -2.4, "f124": 1791531570},
                    {"f14": "白酒概念", "f3": -1.8, "f124": 1791531570}]}}
            return httpx.Response(200, json=gainers if "po=1" in raw else losers)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_market_breadth_sums_index_constituent_counts():
    client = EastmoneyClient(transport=_transport())
    payload = asyncio.run(client.call("market_breadth"))
    assert payload["ok"] is True
    data = payload["data"]
    assert data["up"] == 1341 + 1698 + 308
    assert data["down"] == 956 + 1166 + 37
    assert data["unchanged"] == 60 + 66 + 4
    assert data["source"] == "eastmoney"
    assert data["as_of"].startswith("2026-10-09")


def test_sector_overview_pairs_gainers_with_losers_for_both_boards():
    client = EastmoneyClient(transport=_transport())
    payload = asyncio.run(client.call("sector_overview", top=2))
    assert payload["ok"] is True
    data = payload["data"]
    assert [row["name"] for row in data["industry"]] == ["视频媒体", "文字媒体", "印制电路板", "元件"]
    assert [row["name"] for row in data["concept"]] == ["AI语料", "短剧互动游戏", "煤炭概念", "白酒概念"]
    assert data["industry"][0]["change_pct"] == 19.98
    assert data["industry"][-1]["change_pct"] == -3.67
    assert data["source"] == "eastmoney"
    assert data["as_of"].startswith("2026-10-09")


def test_incomplete_breadth_rows_are_refused_not_guessed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rc": 0, "data": {"total": 1, "diff": [
            {"f12": "000001", "f104": 10, "f105": None, "f106": 3, "f124": 1791533520}]}})

    client = EastmoneyClient(transport=httpx.MockTransport(handler))
    payload = asyncio.run(client.call("market_breadth"))
    assert payload["ok"] is False
    assert payload["data"] is None


def test_unknown_operation_is_refused():
    client = EastmoneyClient(transport=_transport())
    payload = asyncio.run(client.call("not_a_dataset"))
    assert payload["ok"] is False
    assert payload["code"] == "unavailable"


def test_upstream_failure_degrades_without_raising():
    client = EastmoneyClient(transport=_transport(fail=True))
    payload = asyncio.run(client.call("market_breadth"))
    assert payload["ok"] is False
    assert payload["data"] is None


def test_transient_disconnect_is_retried_once():
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise httpx.ConnectError("dropped", request=request)
        return httpx.Response(200, json=_BREADTH_BODY)

    client = EastmoneyClient(transport=httpx.MockTransport(handler))
    payload = asyncio.run(client.call("market_breadth"))
    assert payload["ok"] is True
    assert state["attempts"] == 2


def test_persistent_disconnect_degrades_after_retries():
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["attempts"] += 1
        raise httpx.ConnectError("dropped", request=request)

    client = EastmoneyClient(transport=httpx.MockTransport(handler))
    payload = asyncio.run(client.call("market_breadth"))
    assert payload["ok"] is False
    assert state["attempts"] == 2


def test_operation_catalog_lists_registered_datasets():
    catalog = operation_catalog()
    assert set(catalog) == {"market_breadth", "sector_overview"}
    assert "涨跌家数" in catalog["market_breadth"]


def test_sector_query_keeps_literal_plus_separator():
    """东财的 ``fs`` 依赖字面 ``+`` 作为分隔符，请求 URL 不能被编码成 %2B/%20。"""
    seen: list[str] = []
    client = EastmoneyClient(transport=_transport(seen=seen))
    asyncio.run(client.call("sector_overview", top=3))
    assert any("fs=m:90+t:2" in url for url in seen)
    assert any("fs=m:90+t:3" in url for url in seen)
