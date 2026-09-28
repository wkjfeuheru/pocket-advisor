"""净值同步作业与调度注册。

**核心逻辑不依赖 APScheduler**：``run_nav_sync_job`` 是纯函数式的（给定源、日历、
日期就返回结果），因此可以在没有调度器的情况下完整测试。APScheduler 只在
``register_nav_job`` 里用到，且是延迟导入——没装它也不会影响其余功能。

作业时点：每个交易日 18:00（``Asia/Shanghai``）。用 ``zoneinfo``（标准库 + ``tzdata``），
**不引入 pytz**。非交易日直接跳过，避免写入周末的重复净值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo

from app.services.portfolio_advisor.nav import NavService
from app.services.portfolio_advisor.nav_sync import (
    NavSource,
    NavSyncResult,
    sync_daily_nav,
)

logger = logging.getLogger(__name__)

#: A 股收盘后、净值披露通常已就绪的时点。
SHANGHAI = ZoneInfo("Asia/Shanghai")
JOB_HOUR = 18
JOB_MINUTE = 0

#: 周末。法定节假日不在本模块职责内——接入真实源时应换成交易日历。
_WEEKEND = (5, 6)


class TradingCalendar(Protocol):
    """判断某日是否交易日。默认实现只跳过周末。"""

    def is_trading_day(self, day: date) -> bool: ...


class WeekdayCalendar:
    """最小实现：只按周末判断。

    真实交易日历（含法定节假日）应实现同一协议后注入；Fuyao 已有 ``trade_calendar``
    工具可作为后续数据源。
    """

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() not in _WEEKEND


@dataclass
class NavJobResult:
    """一次作业的结果。``status`` 便于健康检查直接暴露。"""

    status: str
    as_of: date
    written: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run_nav_sync_job(
    *,
    service: NavService,
    source: NavSource | None,
    product_codes: Sequence[str],
    as_of: date,
    calendar: TradingCalendar | None = None,
) -> NavJobResult:
    """执行一次净值同步。

    非交易日返回 ``skipped``；没有净值源返回 ``no_source``。两种情况都不抛错——
    调度器需要把状态记录下来，而不是让整轮任务崩掉。
    """
    cal = calendar or WeekdayCalendar()
    if not cal.is_trading_day(as_of):
        return NavJobResult(status="skipped", as_of=as_of, skipped_reason="non_trading_day")

    codes = [str(code) for code in product_codes if str(code)]
    if not codes:
        return NavJobResult(status="skipped", as_of=as_of, skipped_reason="no_products")

    if source is None:
        logger.warning("净值同步未执行：未配置净值源（as_of=%s）", as_of)
        return NavJobResult(status="no_source", as_of=as_of, skipped_reason="no_source")

    try:
        sync: NavSyncResult = sync_daily_nav(
            service, source, product_codes=codes, as_of=as_of
        )
    except Exception as exc:  # noqa: BLE001 - 作业不得因单次故障中断调度
        logger.exception("净值同步失败: %s", exc)
        return NavJobResult(status="failed", as_of=as_of)

    if sync.status != "ok":
        logger.warning(
            "净值同步 %s：写入 %d、失败 %d（%s）",
            sync.status,
            sync.written,
            len(sync.failed),
            ", ".join(item["product_code"] for item in sync.failed[:5]),
        )
    else:
        logger.info("净值同步完成：写入 %d 行（as_of=%s）", sync.written, as_of)

    return NavJobResult(
        status=sync.status,
        as_of=as_of,
        written=sync.written,
        failed=list(sync.failed),
    )


def enabled_product_codes(library: Any) -> list[str]:
    """从商品池取需要同步净值的标的（排除无行情的理财产品伪代码）。"""
    try:
        pool = library.list_products_for_nav()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取商品池失败，本轮不同步: %s", exc)
        return []
    return sorted(
        str(item["code"])
        for item in pool
        if item.get("code") and str(item.get("type")) != "wealth"
    )


def register_nav_job(
    scheduler: Any,
    *,
    service: NavService,
    source: NavSource | None,
    product_codes: Sequence[str],
    calendar: TradingCalendar | None = None,
    hour: int = JOB_HOUR,
    minute: int = JOB_MINUTE,
) -> Any:
    """把每日净值同步注册进给定的 APScheduler 调度器。

    ``scheduler`` 由调用方创建与启动（便于测试注入假调度器，也便于部署时决定
    是否启用）。返回被注册的 job。
    """
    async def _job() -> None:
        run_nav_sync_job(
            service=service,
            source=source,
            product_codes=product_codes,
            as_of=datetime.now(SHANGHAI).date(),
            calendar=calendar,
        )

    return scheduler.add_job(
        _job,
        trigger="cron",
        hour=hour,
        minute=minute,
        timezone=SHANGHAI,
        id="portfolio_nav_sync",
        replace_existing=True,
    )


__all__ = [
    "JOB_HOUR",
    "JOB_MINUTE",
    "SHANGHAI",
    "NavJobResult",
    "TradingCalendar",
    "WeekdayCalendar",
    "enabled_product_codes",
    "register_nav_job",
    "run_nav_sync_job",
]
