"""东方财富公开行情客户端：主数据源（Fuyao/Tushare）之外的补充取数通道。

设计对齐 ``FuyaoClient``：

- 以"内部操作名"为唯一入口，操作注册表即白名单，未注册操作 fail-closed；
- 每个操作把上游 JSON 归一化成内部契约字段，再统一收进信封（成功/失败/截止时间）；
- 全部只读、不抛异常、不泄漏请求细节；上游失败时返回 ``ok=False`` 的信封，由调用方降级。

数据源为东方财富公开行情接口，无授权钥匙，因此调用方需自设超时与降级；
字段口径以 ``_OPERATIONS`` 中的注释为准，扩展新数据集只需注册一个操作函数。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.mcp.envelope import Envelope, wrap_envelope
from app.utils.sanitize import sanitize_error

logger = logging.getLogger(__name__)

SOURCE = "eastmoney"
_TIMEOUT_SECONDS = 10.0
# 东财实时行情接口会随机丢弃连接（实测同一批请求时成时败），因此重试并按域名轮换。
_RETRIES = 3
_BACKOFF_BASE = 0.3
# 成功结果短期缓存，避免同一会话多轮重复打上游、降低被限流概率。
_CACHE_TTL_SECONDS = 120.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 补齐浏览器常规请求头（Referer 指向东财行情站），降低被反爬拦截的概率。
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_API_PREFIX = "/api/qt"
# 实时行情有多个镜像域名，逐个回退；实测它们可能同时抖动，因此仅作提高成功率的补充。
_QUOTE_HOSTS = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com",
                "https://82.push2.eastmoney.com")
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# 指数 secid 覆盖整市场：上证综指=沪市、深证成指=深市、北证50=北交所。
# 东财对这三个指数返回 f104/f105/f106（上涨/下跌/平盘家数），相加即为全市场交易中股票口径。
BREADTH_SECIDS: tuple[str, ...] = ("1.000001", "0.399001", "0.899050")
# 板块分类：行业板块 / 概念板块。
_SECTOR_BOARDS: tuple[tuple[str, str], ...] = (("industry", "m:90+t:2"), ("concept", "m:90+t:3"))

Operation = Callable[[httpx.AsyncClient, dict[str, Any]], Awaitable[dict[str, Any]]]
_OPERATIONS: dict[str, tuple[str, Operation]] = {}


def _operation(name: str, summary: str) -> Callable[[Operation], Operation]:
    """把一个取数函数登记进操作注册表，同时登记它的用途说明。"""

    def register(func: Operation) -> Operation:
        _OPERATIONS[name] = (summary, func)
        return func

    return register


def _now_iso() -> str:
    return datetime.now(_SHANGHAI).isoformat()


def _number(value: Any) -> float | None:
    """东财字段可能是数字或字符串；不能解析为数值时返回 None，绝不猜数。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _iso_from_epoch(value: Any) -> str:
    """东财的 f124 是秒级时间戳；转为本地 ISO 字符串，失败返回空串。"""
    number = _number(value)
    if number is None or number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number, tz=_SHANGHAI).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _latest_epoch(rows: list[dict], field: str = "f124") -> str:
    """取整批数据里最新的更新时间戳，作为该数据集的 ``as_of``。"""
    stamps = [text for text in (_iso_from_epoch(row.get(field)) for row in rows) if text]
    return max(stamps, default="")


async def _fetch_data(client: httpx.AsyncClient, path: str) -> Any:
    """按域名轮换请求东财并收敛成 ``data`` 字段。

    每次尝试换一个镜像域名（``_QUOTE_HOSTS``）；连接被上游丢弃（``httpx.TransportError``）
    时按指数退避后继续，耗尽 ``_RETRIES`` 次才上报失败。业务错误码不重试，
    避免把确定性失败拖成长尾。``path`` 不含域名，便于替换主机。
    """
    failure: Exception | None = None
    for attempt in range(_RETRIES):
        host = _QUOTE_HOSTS[attempt % len(_QUOTE_HOSTS)]
        try:
            response = await client.get(host + path)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("rc") not in (0, None):
                raise RuntimeError("eastmoney_bad_response")
            return body.get("data")
        except httpx.TransportError as exc:
            failure = exc
            logger.warning("eastmoney_transport_error host=%s attempt=%s reason=%s",
                           host, attempt, sanitize_error(exc))
            if attempt + 1 < _RETRIES:
                await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.1))
    raise failure or RuntimeError("eastmoney_unavailable")


def _rows(data: Any) -> list[dict]:
    """东财的 ``diff`` 有时是列表、有时是以索引为键的字典，这里统一成行列表。"""
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, list):
        return [item for item in diff if isinstance(item, dict)]
    if isinstance(diff, dict):
        return [item for item in diff.values() if isinstance(item, dict)]
    return []


@_operation("market_breadth", "全市场涨跌家数（沪/深/北交所交易中股票口径）")
async def _market_breadth(client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
    secids = params.get("secids") or BREADTH_SECIDS
    joined = ",".join(str(item) for item in secids)
    path = (f"{_API_PREFIX}/ulist.np/get?fltt=2&invt=2&fields=f12,f14,f104,f105,f106,f124"
            f"&secids={joined}")
    rows = _rows(await _fetch_data(client, path))
    up = [item for item in (_count(row.get("f104")) for row in rows) if item is not None]
    down = [item for item in (_count(row.get("f105")) for row in rows) if item is not None]
    flat = [item for item in (_count(row.get("f106")) for row in rows) if item is not None]
    if not rows or len(up) != len(rows) or len(down) != len(rows) or len(flat) != len(rows):
        raise RuntimeError("eastmoney_breadth_incomplete")
    return {
        "up": sum(up),
        "down": sum(down),
        "unchanged": sum(flat),
        "source": SOURCE,
        "as_of": _latest_epoch(rows) or _now_iso(),
    }


@_operation("sector_overview", "行业/概念板块涨跌榜（各取涨幅前 N 与跌幅前 N）")
async def _sector_overview(client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
    top = max(1, min(int(params.get("top") or 5), 20))
    result: dict[str, Any] = {}
    stamps: list[str] = []
    for board, fs in _SECTOR_BOARDS:
        # pz 上限 100，无法一次取全 400+ 个板块；改用"降序取涨幅榜 + 升序取跌幅榜"两次请求，
        # 各自只取 top 条，既准确又省流量。
        rows: list[dict] = []
        for order in ("1", "0"):
            path = (f"{_API_PREFIX}/clist/get?pn=1&pz={top}&po={order}&np=1&fltt=2&invt=2"
                    f"&fid=f3&fs={fs}&fields=f12,f14,f3,f124")
            rows.extend(_rows(await _fetch_data(client, path)))
        result[board] = _leaders(rows, top)
        stamps.extend(text for text in (_iso_from_epoch(row.get("f124")) for row in rows) if text)
    return {**result, "source": SOURCE, "as_of": max(stamps, default="") or _now_iso()}


def _leaders(rows: list[dict], top: int) -> list[dict]:
    """涨幅前 ``top`` 与跌幅前 ``top``，同一板块不重复出现，与内部板块榜单语义一致。"""
    ranked: list[dict] = []
    for row in rows:
        name = str(row.get("f14") or "").strip()
        change = _number(row.get("f3"))
        if not name or change is None:
            continue
        ranked.append({"name": name, "change_pct": change})
    # 涨幅榜与跌幅榜来自两次独立请求，拼接后必须重新排序才能得到"最大跌幅在前"。
    ranked.sort(key=lambda item: item["change_pct"], reverse=True)
    gainers = [item for item in ranked if item["change_pct"] > 0][:top]
    losers = [item for item in reversed(ranked) if item["change_pct"] < 0][:top]
    return [*gainers, *losers]


class EastmoneyClient:
    """按内部操作名调用东财公开接口。返回信封，永不抛出请求细节。

    复用单个 ``httpx.AsyncClient`` 以保持连接池，并对成功结果做短期缓存；
    缓存只存成功信封，失败不缓存，避免把一次抖动放大成持续降级。
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = _TIMEOUT_SECONDS,
                 cache_ttl: float = _CACHE_TTL_SECONDS) -> None:
        self._transport = transport
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[tuple, tuple[float, Envelope]] = {}

    @property
    def operations(self) -> list[str]:
        return sorted(_OPERATIONS)

    def _http(self) -> httpx.AsyncClient:
        """懒加载并复用底层客户端；``transport`` 供测试注入假传输。"""
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=self._timeout,
                                             headers=_HEADERS, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, name: str, **params: Any) -> Envelope:
        entry = _OPERATIONS.get(str(name))
        if entry is None:
            return wrap_envelope(RuntimeError(f"{name} unavailable"), code="unavailable")
        key = (str(name), tuple(sorted((str(k), str(v)) for k, v in params.items())))
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        _, run = entry
        try:
            envelope = wrap_envelope(await run(self._http(), params))
        except Exception as exc:  # noqa: BLE001 - 上游失败收成信封，绝不打断业务图
            logger.warning("eastmoney_call_failed op=%s reason=%s", name, sanitize_error(exc))
            return wrap_envelope(exc)
        if self._cache_ttl > 0:
            self._cache[key] = (now + self._cache_ttl, envelope)
        return envelope


def operation_catalog() -> dict[str, str]:
    """操作名 → 用途说明，供工具描述与自检使用。"""
    return {name: summary for name, (summary, _) in _OPERATIONS.items()}


_CLIENT: EastmoneyClient | None = None


def get_eastmoney_client() -> EastmoneyClient:
    """进程内单例：东财无需授权，直接按需创建即可。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = EastmoneyClient()
    return _CLIENT


__all__ = ["BREADTH_SECIDS", "EastmoneyClient", "SOURCE", "get_eastmoney_client", "operation_catalog"]
