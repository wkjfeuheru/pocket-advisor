"""Tushare Pro：新闻与公司事件。正文不进入返回结构。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

import anyio

from app.mcp.envelope import classify_upstream_error, wrap_envelope
from app.utils.sanitize import sanitize_error

logger = logging.getLogger(__name__)

_TAG_RULES = (
    ("earnings_preview", ("业绩预告", "业绩快报", "年报", "半年报")),
    ("unlock", ("解禁",)),
    ("pledge", ("质押",)),
    ("holder_change", ("增持", "减持", "股东增减持")),
)

# 结构化事件没有新闻标题，也没有上游 source 字段。用数据集本身作为可追溯来源，
# 并在缺标题时用事件标签生成可读标题，避免这些事件被证据门禁整块删掉。
_DATASET_SOURCES = {
    "news": "tushare:news",
    "earnings_preview": "tushare:forecast",
    "unlock": "tushare:share_float",
    "pledge": "tushare:pledge_stat",
    "holder_change": "tushare:stk_holdertrade",
}
_DATASET_TITLES = {
    "earnings_preview": "业绩预告",
    "unlock": "限售解禁",
    "pledge": "股权质押",
    "holder_change": "股东增减持",
}
_TIME_KEYS = ("datetime", "ann_date", "pub_time", "end_date", "float_date", "trade_date")


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if hasattr(payload, "to_dict"):
        try:
            return list(payload.to_dict("records"))
        except TypeError:
            return list(payload.to_dict())
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _tags(title: str) -> tuple[list[str], float]:
    found = [name for name, words in _TAG_RULES if any(word in title for word in words)]
    if found:
        return found, 0.8
    return ["news"], 0.5


def _mentions(row: dict[str, Any], thscode: str) -> bool:
    blob = " ".join(str(row.get(key) or "") for key in ("channels", "ts_code", "code", "title", "ann_title"))
    root = thscode.split(".")[0]
    return thscode in blob or (root and root in blob)


def _published_text(row: dict[str, Any]) -> str:
    for key in _TIME_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _published_on(row: dict[str, Any]) -> date | None:
    digits = "".join(ch for ch in _published_text(row) if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _public_event(
    row: dict[str, Any],
    thscode: str,
    tag: str | None = None,
    source: str = "",
) -> dict[str, Any] | None:
    if not _mentions(row, thscode):
        return None
    title = str(row.get("title") or row.get("ann_title") or "")
    tags, confidence = _tags(title)
    if tag and tag not in tags:
        tags = [tag, *tags]
    if not title and tag:
        title = _DATASET_TITLES.get(tag, tag)
    related = str(row.get("channels") or row.get("ts_code") or thscode)
    symbols = [part.strip() for part in related.replace(",", " ").split() if part.strip()]
    return {
        "title": title,
        "source": str(row.get("source") or row.get("src") or source or ""),
        "url": str(row.get("url") or row.get("ann_url") or ""),
        "published_at": _published_text(row),
        "related_symbols": symbols,
        "event_tags": tags,
        "confidence": confidence,
    }


_STRUCTURED = (
    ("forecast", "earnings_preview"),
    ("share_float", "unlock"),
    ("pledge_stat", "pledge"),
    ("stk_holdertrade", "holder_change"),
)


def _load_events(api: Any, thscode: str, start: date, end: date) -> list[dict[str, Any]]:
    if api is None:
        raise RuntimeError("tushare unconfigured")
    start_text = start.strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")
    events: list[dict[str, Any]] = []

    def _collect(rows: Any, tag: str | None = None) -> None:
        source = _DATASET_SOURCES.get(tag or "news", "")
        for row in _rows(rows):
            published = _published_on(row)
            if published is not None and (published < start or published > end):
                continue
            item = _public_event(row, thscode, tag, source)
            if item is not None:
                events.append(item)

    if hasattr(api, "news"):
        _collect(api.news(src="sina", start_date=start_text, end_date=end_text))
    for method, tag in _STRUCTURED:
        if not hasattr(api, method):
            continue
        loaded = getattr(api, method)(ts_code=thscode, start_date=start_text, end_date=end_text)
        _collect(loaded, tag)
    return events


def _tushare_code(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "tushare_forbidden"
    text = sanitize_error(exc).lower()
    if "privilege" in text or "forbidden" in text or "权限" in text:
        return "tushare_forbidden"
    mapped = classify_upstream_error(exc)
    if mapped == "timeout":
        return "tushare_timeout"
    if mapped == "rate_limited":
        return "tushare_rate_limited"
    return "tushare_unavailable"


class TushareClient:
    def __init__(self, token: str = "", pro: Any = None) -> None:
        self.token = (token or "").strip()
        self._pro = pro

    def _api(self) -> Any:
        if self._pro is not None:
            return self._pro
        if not self.token:
            return None
        import tushare as ts

        return ts.pro_api(self.token)

    async def list_events(self, thscode: str, *, days: int = 30, as_of: date | None = None) -> dict[str, Any]:
        if not self.token:
            return wrap_envelope(RuntimeError("tushare unconfigured"), code="tushare_unconfigured")
        end = as_of or date.today()
        start = end - timedelta(days=days)
        try:
            events = await anyio.to_thread.run_sync(partial(_load_events, self._api(), thscode, start, end))
            return wrap_envelope(events)
        except Exception as exc:  # noqa: BLE001 - 当轮降级
            logger.warning("tushare_unavailable reason=%s", sanitize_error(exc))
            return wrap_envelope(exc, code=_tushare_code(exc))


def load_tushare_client(token: str) -> tuple[TushareClient, str]:
    cleaned = (token or "").strip()
    if not cleaned:
        return TushareClient(token=""), "tushare_unconfigured"
    return TushareClient(token=cleaned), ""


__all__ = ["TushareClient", "load_tushare_client"]
