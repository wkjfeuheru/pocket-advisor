"""日频净值同步：从净值源拉当日净值并落库。

调度器与外部数据源之间的接缝。**目前没有可用的外部净值源**（Fuyao 的基金端点未验证、
Tushare 无 ``fund_nav``），因此这里把源定义成一个协议，无源时作业明确报 ``no_source``
而不是静默"成功但什么都没写"——后者会让定时任务看起来在跑，实际净值永远是旧的。

三条行为约定：

- 同步回来的行是 **``real``**，因此按覆盖规则会压过合成数据（这正是要的）。
- **单个标的失败不影响其余**：源偶发超时不该让整批净值全丢。逐标的收集错误。
- **非交易日不跑**：用交易日历判断，避免写入周末的重复值。

═══════════════════════════════════════════════════════════════════════════
derivation —— 同步结果计数
═══════════════════════════════════════════════════════════════════════════
源返回：A=1.10、B=2.20，C 抛异常，D 返回 None（该日无净值）

    written = 2（A、B）
    failed  = 2（C 异常、D 无数据）

    A、B 落库为 real；若库中同日已有 synthetic，则被覆盖。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import NavRow, NavService
from app.services.portfolio_advisor.nav_sync import (
    NavSourceError,
    NavSyncResult,
    sync_daily_nav,
)

AS_OF = date(2026, 9, 28)  # 周一


class _FakeNavStore:
    def __init__(self):
        self.rows: dict[tuple[str, date], NavRow] = {}

    def upsert_many(self, rows):
        written = 0
        for row in rows:
            key = (row.product_code, row.trade_date)
            existing = self.rows.get(key)
            if (
                existing is not None
                and existing.data_source == "real"
                and row.data_source != "real"
            ):
                continue
            self.rows[key] = row
            written += 1
        return written

    def history(self, product_code, start, end):
        return sorted(
            (day, row.nav)
            for (code, day), row in self.rows.items()
            if code == product_code and start <= day <= end
        )

    def latest(self, product_code):
        found = sorted(
            (day, row.nav) for (code, day), row in self.rows.items() if code == product_code
        )
        return found[-1] if found else None

    def delete_synthetic(self, product_code=None):
        doomed = [
            k
            for k, r in self.rows.items()
            if r.data_source != "real" and (product_code is None or k[0] == product_code)
        ]
        for k in doomed:
            del self.rows[k]
        return len(doomed)


class _FakeSource:
    """按代码返回净值；值可以是数字、``None``（无数据）或异常。"""

    def __init__(self, values, *, as_of=AS_OF):
        self.values = values
        self._as_of = as_of
        self.calls: list[str] = []

    def latest_nav(self, product_code: str, *, as_of: date):
        self.calls.append(product_code)
        value = self.values.get(product_code)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return (self._as_of, float(value))


@pytest.fixture
def store():
    return _FakeNavStore()


@pytest.fixture
def service(store):
    return NavService(store)


def test_sync_writes_real_rows_for_every_product_that_returned_a_quote(service, store):
    source = _FakeSource({"A": 1.10, "B": 2.20})

    result = sync_daily_nav(service, source, product_codes=["A", "B"], as_of=AS_OF)

    assert result.written == 2
    assert result.failed == []
    assert service.history("A", AS_OF, AS_OF) == [(AS_OF, 1.10)]
    assert store.rows[("A", AS_OF)].data_source == "real"


def test_sync_overwrites_synthetic_values(service, store):
    """同步回来的必须压过合成值，否则真实数据永远盖不住演示数据。"""
    service.upsert([NavRow("A", AS_OF, 1.0, "synthetic")])

    sync_daily_nav(service, _FakeSource({"A": 1.10}), product_codes=["A"], as_of=AS_OF)

    assert service.history("A", AS_OF, AS_OF) == [(AS_OF, 1.10)]
    assert store.rows[("A", AS_OF)].data_source == "real"


def test_one_failing_product_does_not_lose_the_others(service):
    source = _FakeSource({"A": 1.10, "B": RuntimeError("upstream timeout"), "D": None})

    result = sync_daily_nav(
        service, source, product_codes=["A", "B", "D"], as_of=AS_OF
    )

    # A 仍然落库
    assert result.written == 1
    assert service.history("A", AS_OF, AS_OF) == [(AS_OF, 1.10)]
    # 失败的两个都进 failed，且带原因
    codes = {item["product_code"] for item in result.failed}
    assert codes == {"B", "D"}
    reasons = {item["product_code"]: item["reason"] for item in result.failed}
    assert reasons["D"] == "no_data"
    assert "timeout" in reasons["B"]


def test_a_quote_that_disagrees_on_the_date_is_still_stored_under_its_own_date(service):
    """源给的日期才是净值生效日，不能强行记成"今天"。"""
    other_day = date(2026, 9, 25)
    source = _FakeSource({"A": 1.10}, as_of=other_day)

    result = sync_daily_nav(service, source, product_codes=["A"], as_of=AS_OF)

    assert result.written == 1
    assert service.history("A", other_day, other_day) == [(other_day, 1.10)]
    assert service.history("A", AS_OF, AS_OF) == []


def test_an_empty_product_list_is_rejected(service):
    with pytest.raises(NavSourceError) as excinfo:
        sync_daily_nav(service, _FakeSource({}), product_codes=[], as_of=AS_OF)

    assert excinfo.value.code == "no_products"


def test_a_missing_source_is_reported_not_silently_skipped(service):
    """没有净值源时必须显式失败。

    静默"成功但什么都没写"会让定时任务看起来在跑，实际净值永远停在旧值——
    这类故障比直接报错难发现得多。
    """
    result = sync_daily_nav(service, None, product_codes=["A"], as_of=AS_OF)

    assert result.written == 0
    assert result.status == "no_source"
    assert result.failed == []


def test_the_result_status_distinguishes_success_partial_and_total_failure(service):
    ok = sync_daily_nav(service, _FakeSource({"A": 1.1}), product_codes=["A"], as_of=AS_OF)
    assert ok.status == "ok"

    partial = sync_daily_nav(
        service, _FakeSource({"A": 1.1, "B": RuntimeError("x")}), product_codes=["A", "B"], as_of=AS_OF
    )
    assert partial.status == "partial"

    failed = sync_daily_nav(
        service, _FakeSource({"B": RuntimeError("x")}), product_codes=["B"], as_of=AS_OF
    )
    assert failed.status == "failed"
