"""净值同步作业：交易日判断、状态上报与调度注册。

核心逻辑（``run_nav_sync_job``）**不依赖 APScheduler**，因此可以完整单测；
APScheduler 只在 ``register_nav_job`` 用到，注册用的是假调度器 + 内部作业函数调用，
所以即使环境里没装 APScheduler，这些测试也能跑。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import NavRow, NavService
from app.services.portfolio_advisor.nav_job import (
    NavJobResult,
    WeekdayCalendar,
    enabled_product_codes,
    register_nav_job,
    run_nav_sync_job,
)

MONDAY = date(2026, 9, 28)
SATURDAY = date(2026, 10, 3)


class _FakeStore:
    def __init__(self):
        self.rows = {}

    def upsert_many(self, rows):
        for row in rows:
            self.rows[(row.product_code, row.trade_date)] = row
        return len(rows)

    def history(self, code, start, end):
        return sorted(
            (d, r.nav) for (c, d), r in self.rows.items() if c == code and start <= d <= end
        )

    def latest(self, code):
        found = sorted((d, r.nav) for (c, d), r in self.rows.items() if c == code)
        return found[-1] if found else None

    def delete_synthetic(self, product_code=None):
        return 0


class _FakeSource:
    def __init__(self, values):
        self.values = values

    def latest_nav(self, product_code, *, as_of):
        value = self.values.get(product_code)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return (as_of, float(value))


@pytest.fixture
def service():
    return NavService(_FakeStore())


def test_the_job_writes_navs_on_a_trading_day(service):
    result = run_nav_sync_job(
        service=service,
        source=_FakeSource({"A": 1.1, "B": 2.2}),
        product_codes=["A", "B"],
        as_of=MONDAY,
    )

    assert result.status == "ok"
    assert result.written == 2
    assert result.as_of == MONDAY


def test_the_job_skips_weekends(service):
    result = run_nav_sync_job(
        service=service,
        source=_FakeSource({"A": 1.1}),
        product_codes=["A"],
        as_of=SATURDAY,
    )

    assert result.status == "skipped"
    assert result.skipped_reason == "non_trading_day"
    assert result.written == 0


def test_the_job_reports_no_source_instead_of_pretending_success(service):
    result = run_nav_sync_job(
        service=service,
        source=None,
        product_codes=["A"],
        as_of=MONDAY,
    )

    assert result.status == "no_source"
    assert result.written == 0


def test_the_job_reports_partial_failure_with_the_offending_codes(service):
    result = run_nav_sync_job(
        service=service,
        source=_FakeSource({"A": 1.1, "B": RuntimeError("boom")}),
        product_codes=["A", "B"],
        as_of=MONDAY,
    )

    assert result.status == "partial"
    assert result.written == 1
    assert {item["product_code"] for item in result.failed} == {"B"}


def test_a_sync_crash_does_not_propagate_out_of_the_job(service):
    """作业里的异常不能冒泡——否则调度器会把后续轮次一起带停。"""

    class _ExplodingSource:
        def latest_nav(self, product_code, *, as_of):
            raise RuntimeError("store unavailable")

    result = run_nav_sync_job(
        service=service,
        source=_ExplodingSource(),
        product_codes=["A"],
        as_of=MONDAY,
    )

    # 全部标的失败 → failed（而不是抛异常）
    assert result.status == "failed"


def test_no_products_means_skip_not_failure(service):
    result = run_nav_sync_job(
        service=service, source=_FakeSource({}), product_codes=[], as_of=MONDAY
    )

    assert result.status == "skipped"
    assert result.skipped_reason == "no_products"


def test_the_weekday_calendar_knows_weekends():
    cal = WeekdayCalendar()

    assert cal.is_trading_day(MONDAY) is True
    assert cal.is_trading_day(SATURDAY) is False


def test_enabled_product_codes_excludes_wealth_pseudo_codes():
    class _Library:
        def list_products_for_nav(self):
            return [
                {"code": "110011", "type": "fund"},
                {"code": "510300", "type": "etf"},
                {"code": "WM2026001", "type": "wealth"},
            ]

    assert enabled_product_codes(_Library()) == ["110011", "510300"]


def test_enabled_product_codes_survives_a_broken_library():
    class _Broken:
        def list_products_for_nav(self):
            raise RuntimeError("db down")

    assert enabled_product_codes(_Broken()) == []


class _FakeScheduler:
    """记录注册参数的假调度器；顺带让我们能直接调用被注册的作业函数。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.job = None

    def add_job(self, func, *, trigger, **kwargs):
        self.calls.append({"trigger": trigger, **kwargs})
        self.job = func
        return func


def test_registering_the_job_uses_a_daily_cron_trigger_at_18_shanghai(service):
    scheduler = _FakeScheduler()

    register_nav_job(
        scheduler,
        service=service,
        source=_FakeSource({"A": 1.1}),
        product_codes=["A"],
    )

    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["trigger"] == "cron"
    assert call["hour"] == 18
    assert call["minute"] == 0
    assert str(call["timezone"]) == "Asia/Shanghai"
    assert call["id"] == "portfolio_nav_sync"
    assert call["replace_existing"] is True
    assert scheduler.job is not None


def test_the_registered_job_is_runnable_and_writes_navs(service, monkeypatch):
    import asyncio
    from datetime import datetime
    from app.services.portfolio_advisor import nav_job
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 28, 18, 0, tzinfo=tz)
    monkeypatch.setattr(nav_job, "datetime", Clock)

    scheduler = _FakeScheduler()
    register_nav_job(
        scheduler,
        service=service,
        source=_FakeSource({"A": 1.1}),
        product_codes=["A"],
    )

    asyncio.run(scheduler.job())

    # 作业用的是"现在"，所以不断言具体日期，只断言确实写进去了
    assert service._store.rows, "注册的作业应当真的写入净值"
