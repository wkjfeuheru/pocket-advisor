"""大盘只读数据流水线：按已确认范围查询，不让模型追逐不存在的工具。"""
from __future__ import annotations
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.stock.middleware import persist_tool_output
from app.agents.stock.market import build_market_overview
from app.agents.stock.contracts import StockAgentResponse
from app.mcp.fuyao import operation_for_tool_name

DEFAULT_INDICES = ("000001.SH", "399001.SZ", "399006.SZ", "000300.SH")


async def _fetch_auxiliary(client, name: str) -> dict | None:
    """用补充数据源抓取一项聚合口径。

    主数据源（Fuyao）不提供涨跌家数与板块行情，这里由东财补齐；
    未注入客户端或上游失败时返回 None，保留原来的降级提示，绝不编造。
    """
    if client is None:
        return None
    envelope = await client.call(name)
    data = envelope.get("data") if envelope.get("ok") else None
    return dict(data) if isinstance(data, dict) else None


async def collect_market_evidence(store, context, tools, slots, *, eastmoney=None) -> StockAgentResponse:
    """已有指数足以构成部分市场概览；缺失广度和板块只限制相应结论。"""
    snapshot = next((t for t in tools if "index_prices_snapshot" in t.name), None)
    calendar = next((t for t in tools if operation_for_tool_name(t.name) == "trade_calendar"), None)
    search = next((t for t in tools if operation_for_tool_name(t.name) == "search_security"), None)
    historical = next((t for t in tools if "index_prices_historical" in t.name), None)
    codes = list(dict.fromkeys([*(slots.get("symbols") or []), *(slots.get("index_scope") or [])])) or list(DEFAULT_INDICES)
    rows = []

    async def fetch(tool, args):
        if tool is None:
            return None
        try:
            async with asyncio.timeout(15):
                output = await tool.ainvoke(args)
            preview = await asyncio.to_thread(persist_tool_output, store, context, tool_name=tool.name,
                tool_call_id=f"market:{tool.name}:{len(rows)}", arguments=args, result=output)
            row = await asyncio.to_thread(store.get, preview["evidence_id"], context)
            rows.append(row)
            return row
        except Exception as exc:
            from app.agents.graphs.task_agent import is_degradable
            if not is_degradable(exc):
                raise
            return None

    quote_row, calendar_row = await asyncio.gather(fetch(snapshot, {"thscodes": ",".join(codes)}), fetch(calendar, {}))

    def data(row):
        payload = row["payload"] if row else {}
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    quotes = data(quote_row)
    quotes = quotes if isinstance(quotes, list) else [quotes] if isinstance(quotes, dict) else []
    quotes = [item for item in quotes if item.get("thscode") in codes and isinstance(item.get("last"), (int, float))]
    names = {}
    if quotes and search is not None:
        metadata = await asyncio.gather(*(fetch(search, {"q": item["thscode"], "asset_type": "a-share-index"}) for item in quotes))
        for row in metadata:
            entries = data(row)
            entries = entries if isinstance(entries, list) else [entries] if isinstance(entries, dict) else []
            for item in entries:
                if item.get("thscode") in codes and item.get("name"):
                    names[item["thscode"]] = item["name"]
    for quote in quotes:
        # ticker 是数据源代码，不冒充已确认的名称。
        quote["name"] = names.get(quote["thscode"], quote["thscode"])
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    days = data(calendar_row)
    days = days if isinstance(days, list) else [days] if isinstance(days, dict) else []
    trading_dates = sorted({str(item.get("trade_date") or "") for item in days if item.get("trade_date") and str(item["trade_date"]) <= now.date().isoformat()})
    last_day = trading_dates[-1] if trading_dates else ""
    clock = now.strftime("%H:%M")
    session = "open" if last_day == now.date().isoformat() and ("09:30" <= clock <= "11:30" or "13:00" <= clock <= "15:00") else "closed"
    # 广度与板块由补充数据源补齐；取不到就保持 None，让概览层如实降级。
    breadth = await _fetch_auxiliary(eastmoney, "market_breadth")
    sectors = await _fetch_auxiliary(eastmoney, "sector_overview")
    result = build_market_overview(indices=quotes, breadth=breadth, sectors=sectors, now=now, session=session)
    result["last_calendar_trade_date"] = last_day
    result["snapshot_time"] = result["as_of"]
    if slots.get("timeframe"):
        period = slots["timeframe"]
        start = datetime.fromisoformat(period["start"]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        end = datetime.fromisoformat(period["end"]).replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo("Asia/Shanghai"))
        histories = await asyncio.gather(*(fetch(historical, {"thscode": code, "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000), "interval": "1d"}) for code in codes)) if historical else []
        trends = []
        for code, row in zip(codes, histories):
            entries = data(row)
            entries = entries if isinstance(entries, list) else []
            # 上游有时忽略请求区间；历史样本必须实际落在本次区间内才能计算。
            valid = sorted([item for item in entries if isinstance(item, dict) and str(item.get("thscode") or code).upper() == code
                and period["start"] <= str(item.get("as_of") or "")[:10] <= period["end"]
                and isinstance(item.get("close"), (int, float))], key=lambda item: item["as_of"])
            if len(valid) >= 2 and valid[0]["close"]:
                trends.append({"thscode": code, "name": names.get(code, code), "first_close": valid[0]["close"],
                    "last_close": valid[-1]["close"], "change_pct": (valid[-1]["close"] / valid[0]["close"] - 1) * 100,
                    "start_date": valid[0]["as_of"], "as_of": valid[-1]["as_of"], "source": "fuyao",
                    "units": {"first_close": "点", "last_close": "点", "change_pct": "%"},
                    "input_evidence_ids": [row["id"]], "coverage": "available_sessions"})
        result["historical_indices"] = trends
        if len(trends) != len(codes):
            result["warnings"].append("历史行情未返回所要求区间内足够的数据，暂不能判断该区间的走势。")
        if trends and not result["indices"]:
            result["failed"] = False
            result["as_of"] = max(item["as_of"] for item in trends)
            result["snapshot_time"] = ""
            result["warnings"] = [w for w in result["warnings"] if w != "fuyao_unavailable"]
            result["warnings"].append("最新指数快照暂不可用，以下仅展示所请求区间内的历史数据。")
    aggregate = await asyncio.to_thread(store.put, context, tool_call_id="market:aggregate", tool_name="aggregate_market_snapshot",
        evidence_type="market_overview", payload=result, source=",".join(result["sources"]), as_of=result["as_of"],
        arguments={"evidence_ids": [r["id"] for r in rows]})
    moves = [float(item["change_pct"]) for item in result["indices"] if isinstance(item.get("change_pct"), (int, float))]
    summary = "已核验的主要指数整体上涨。" if moves and all(v > 0 for v in moves) else "已核验的主要指数整体下跌。" if moves and all(v < 0 for v in moves) else "已核验的主要指数表现分化。" if moves else "指数行情暂不可用。"
    summary += "这是最新快照的表现，不能直接推断近期连续走势。"
    # 只对确实缺失的口径声明限制，避免有了数据还挂着"数据不足"的降级话术。
    if result["breadth"].get("up") is None:
        summary += "广度数据不足，暂不评价全市场情绪。"
    if not result["sectors"].get("industry"):
        summary += "板块数据不足，暂不评价板块轮动。"
    if result.get("historical_indices"):
        summary += "历史区间仅根据返回的有效交易日收盘序列分析，不与最新快照直接拼接。"
    return StockAgentResponse(scenario="market_overview", summary=summary, evidence_ids=[aggregate["id"]],
        warnings=result["warnings"], evidence_quality="failed" if result["failed"] else "partial", failed=result["failed"])
