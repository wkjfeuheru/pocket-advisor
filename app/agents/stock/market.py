"""市场行情总览。盘中超过 5 分钟的行情视为过期；上游失败不编造涨跌。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

QUOTE_STALE_SECONDS = 300


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return stamp


def session_from_calendar(payload: dict | None) -> str:
    """交易日历决定盘中或收盘。缺少日历时按收盘处理，避免把旧收盘价当成过期盘中价删掉。"""
    if not isinstance(payload, dict):
        return "closed"
    session = payload.get("session")
    if session is True or str(session).lower() in {"open", "1", "true"}:
        return "open"
    return "closed"


def _fresh(item: dict, *, now: datetime, session: str) -> bool:
    stamp = _parse_time(item.get("as_of"))
    if stamp is None:
        return False
    if session != "open":
        return True
    return abs((now - stamp).total_seconds()) <= QUOTE_STALE_SECONDS


def _leaders(rows: list[dict], *, top: int = 5) -> dict[str, list[dict]]:
    """涨幅前 ``top`` 与跌幅前 ``top``。板块样本不足时同一个板块不重复出现。"""
    ranked = sorted(rows, key=lambda row: float(row.get("change_pct") or 0), reverse=True)
    gainers = ranked[:top]
    taken = {id(row) for row in gainers}
    losers = [row for row in reversed(ranked) if id(row) not in taken][:top]
    return {"gainers": gainers, "losers": losers}


def build_market_overview(
    *,
    indices: list[dict] | None,
    breadth: dict | None,
    sectors: dict | None,
    now: datetime,
    session: str = "open",
    upstream_error: str = "",
) -> dict[str, Any]:
    if not indices:
        warning = upstream_error or "fuyao_unavailable"
        return {
            "failed": True,
            "indices": [],
            "breadth": {},
            "sectors": {"industry": [], "concept": []},
            "as_of": now.isoformat(),
            "sources": ["fuyao"],
            "warnings": [warning],
        }
    kept = []
    warnings: list[str] = []
    if breadth is None:
        warnings.append("市场涨跌家数暂不可用，以下分析仅覆盖已核验的指数行情。")
    if sectors is None:
        warnings.append("板块行情暂不可用，暂不判断板块轮动。")
    breadth = breadth or {}
    sectors = sectors or {}
    for item in indices:
        if _fresh(item, now=now, session=session):
            kept.append({
                "thscode": item.get("thscode"),
                "name": item.get("name"),
                "last": item.get("last"),
                "change_pct": item.get("change_pct"),
                "turnover": item.get("turnover"),
                "units": {"last": "点", "change_pct": "%", "turnover": "元"},
                "source": item.get("source") or "fuyao",
                "as_of": item.get("as_of"),
            })
        else:
            warnings.append("盘中行情过期，已删除超过 5 分钟的指数报价")
    industry = _leaders(list((sectors or {}).get("industry") or []))
    concept = _leaders(list((sectors or {}).get("concept") or []))
    as_of = str(breadth.get("as_of") or (kept[0]["as_of"] if kept else now.isoformat()))
    return {
        "failed": not bool(kept),
        "indices": kept,
        "breadth": {
            "up": breadth.get("up"),
            "down": breadth.get("down"),
            "unchanged": breadth.get("unchanged"),
            "turnover": breadth.get("turnover"),
            "limit_up": breadth.get("limit_up"),
            "limit_down": breadth.get("limit_down"),
            "continue_board": breadth.get("continue_board"),
            "broken_board": breadth.get("broken_board"),
            "source": breadth.get("source") or "fuyao",
            "as_of": breadth.get("as_of"),
        },
        "sectors": {
            "industry": industry["gainers"] + industry["losers"],
            "concept": concept["gainers"] + concept["losers"],
            "source": sectors.get("source") or "fuyao",
            "as_of": sectors.get("as_of"),
        },
        "as_of": as_of,
        "sources": ["fuyao"],
        "warnings": list(dict.fromkeys(warnings)),
    }


__all__ = ["QUOTE_STALE_SECONDS", "build_market_overview", "session_from_calendar"]
