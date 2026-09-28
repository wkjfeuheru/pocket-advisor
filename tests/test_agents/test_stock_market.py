"""市场行情总览：过期行情作废，上游失败不编造。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.stock.market import QUOTE_STALE_SECONDS, build_market_overview

AS_OF = datetime(2026, 9, 27, 14, 0, tzinfo=timezone(timedelta(hours=8)))


def _index(thscode: str, name: str, change_pct: float) -> dict:
    return {
        "thscode": thscode,
        "name": name,
        "last": 3000.0,
        "change_pct": change_pct,
        "source": "fuyao",
        "as_of": AS_OF.isoformat(),
    }


def test_market_overview_includes_breadth_and_sector_leaders():
    result = build_market_overview(
        indices=[_index("000001.SH", "上证综指", 0.8), _index("000300.SH", "沪深300", 1.1)],
        breadth={"up": 2800, "down": 1400, "unchanged": 200, "turnover": 1.2e12, "limit_up": 40, "limit_down": 8, "continue_board": 12, "broken_board": 6, "source": "fuyao", "as_of": AS_OF.isoformat()},
        sectors={
            "industry": [{"name": "银行", "change_pct": 2.1}, {"name": "房地产", "change_pct": -1.8}],
            "concept": [{"name": "人工智能", "change_pct": 3.2}, {"name": "煤炭", "change_pct": -2.4}],
            "source": "fuyao",
            "as_of": AS_OF.isoformat(),
        },
        now=AS_OF,
        session="open",
    )
    assert result["failed"] is False
    assert {item["thscode"] for item in result["indices"]} >= {"000001.SH", "000300.SH"}
    assert result["breadth"]["limit_up"] == 40
    assert result["breadth"]["continue_board"] == 12
    assert result["sectors"]["industry"][0]["name"] == "银行"
    assert result["as_of"] == AS_OF.isoformat()
    assert "fuyao" in result["sources"]


def test_intraday_quote_older_than_five_minutes_is_dropped():
    stale = AS_OF - timedelta(seconds=QUOTE_STALE_SECONDS + 1)
    result = build_market_overview(
        indices=[{**_index("000001.SH", "上证综指", 0.8), "as_of": stale.isoformat()}],
        breadth={"up": 1, "down": 1, "source": "fuyao", "as_of": AS_OF.isoformat()},
        sectors={"industry": [], "concept": [], "source": "fuyao", "as_of": AS_OF.isoformat()},
        now=AS_OF,
        session="open",
    )
    assert result["indices"] == []
    assert any("过期" in item or "stale" in item.lower() for item in result["warnings"])


def test_closed_session_keeps_last_close_even_if_older_than_five_minutes():
    close_as_of = AS_OF - timedelta(hours=5)
    result = build_market_overview(
        indices=[{**_index("000001.SH", "上证综指", -0.2), "as_of": close_as_of.isoformat()}],
        breadth={"up": 1000, "down": 2000, "source": "fuyao", "as_of": close_as_of.isoformat()},
        sectors={"industry": [], "concept": [], "source": "fuyao", "as_of": close_as_of.isoformat()},
        now=AS_OF,
        session="closed",
    )
    assert result["indices"][0]["change_pct"] == -0.2
    assert result["failed"] is False


def test_trade_calendar_selects_intraday_or_close_freshness():
    from app.agents.stock.market import session_from_calendar

    assert session_from_calendar({"session": "open", "trade_date": "2026-09-27"}) == "open"
    assert session_from_calendar({"session": "closed", "trade_date": "2026-09-26"}) == "closed"
    assert session_from_calendar(None) == "closed"


def test_fuyao_failure_does_not_invent_moves():
    result = build_market_overview(
        indices=None,
        breadth=None,
        sectors=None,
        now=AS_OF,
        session="open",
        upstream_error="fuyao_unavailable",
    )
    assert result["failed"] is True
    assert result["indices"] == []
    assert "fuyao_unavailable" in result["warnings"]
    assert "change_pct" not in str(result.get("indices"))
