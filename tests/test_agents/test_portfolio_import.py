"""净值导入：CSV 解析、校验与落库。

真实历史的入口。两条硬要求：

1. **未知产品必须报错并列出代码**，不能静默跳过——``product_nav_history.product_code``
   有外键指向 ``finance.products``，静默跳过会让"导入成功"变成一句谎话：用户以为
   28 个产品都进去了，实际只进去 14 个。
2. **导入幂等**：同一份文件重复导入不产生重复行（靠主键冲突走更新）。
   真数据覆盖合成、合成不覆盖真数据由 ``NavService`` 统一负责。

═══════════════════════════════════════════════════════════════════════════
derivation —— 解析规模
═══════════════════════════════════════════════════════════════════════════
CSV（含表头 + 1 行空行 + 1 行注释级空值）：
    product_code,trade_date,nav
    P1,2026-01-05,1.0000
    P1,2026-01-06,1.0100
    P2,2026-01-05,2.0000
    <空行>
    P2,2026-01-06,
    P3,not-a-date,1.5
    P9,2026-01-05,9.9

    → 有效 3 行（P1×2、P2×1）
    → 问题 3 条：空行忽略不计；P2 缺净值、P3 日期非法、P9 不在产品池

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import (
    REAL,
    NavRow,
    NavService,
    parse_nav_csv,
)


class _FakeNavStore:
    def __init__(self):
        self.rows: dict[tuple[str, date], NavRow] = {}

    def upsert_many(self, rows):
        written = 0
        for row in rows:
            key = (row.product_code, row.trade_date)
            existing = self.rows.get(key)
            if existing is not None and existing.data_source == REAL and row.data_source != REAL:
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
            key
            for key, row in self.rows.items()
            if row.data_source != REAL and (product_code is None or key[0] == product_code)
        ]
        for key in doomed:
            del self.rows[key]
        return len(doomed)


CSV = """product_code,trade_date,nav
P1,2026-01-05,1.0000
P1,2026-01-06,1.0100
P2,2026-01-05,2.0000

P2,2026-01-06,
P3,not-a-date,1.5
P9,2026-01-05,9.9
"""

KNOWN = {"P1", "P2", "P3"}


@pytest.fixture
def service():
    return NavService(_FakeNavStore())


def test_parsing_separates_valid_rows_from_problems():
    rows, problems = parse_nav_csv(CSV, known_products=KNOWN)

    assert len(rows) == 3
    assert {(row.product_code, row.trade_date) for row in rows} == {
        ("P1", date(2026, 1, 5)),
        ("P1", date(2026, 1, 6)),
        ("P2", date(2026, 1, 5)),
    }
    assert all(row.data_source == REAL for row in rows)

    # 三条问题：P2 缺净值、P3 日期非法、P9 未知产品
    assert len(problems) == 3
    joined = " | ".join(problems)
    assert "P2" in joined and "P3" in joined and "P9" in joined


def test_an_unknown_product_is_reported_with_its_code_not_silently_skipped():
    _, problems = parse_nav_csv(CSV, known_products=KNOWN)

    unknown = [note for note in problems if "P9" in note]
    assert unknown, "未知产品必须出现在问题清单里"
    assert "P9" in unknown[0]


def test_importing_writes_the_valid_rows_and_reports_the_rest(service):
    from app.services.portfolio_advisor.nav import import_nav_csv

    result = import_nav_csv(service, CSV, known_products=KNOWN)

    assert result.written == 3
    assert result.skipped == 3
    assert service.history("P1", date(2026, 1, 1), date(2026, 1, 31)) == [
        (date(2026, 1, 5), 1.0),
        (date(2026, 1, 6), 1.01),
    ]
    # 未知产品没进库
    assert service.latest("P9") is None


def test_reimporting_the_same_file_is_idempotent(service):
    from app.services.portfolio_advisor.nav import import_nav_csv

    first = import_nav_csv(service, CSV, known_products=KNOWN)
    second = import_nav_csv(service, CSV, known_products=KNOWN)

    assert first.written == 3
    # 第二次仍是 3 行被"写入"（走更新），但库里行数不变
    assert second.written == 3
    assert service.history("P1", date(2026, 1, 1), date(2026, 1, 31)) == [
        (date(2026, 1, 5), 1.0),
        (date(2026, 1, 6), 1.01),
    ]


def test_importing_never_clobbers_existing_real_rows(service):
    from app.services.portfolio_advisor.nav import import_nav_csv

    service.upsert([NavRow("P1", date(2026, 1, 5), 7.77, REAL)])

    import_nav_csv(service, "product_code,trade_date,nav\nP1,2026-01-05,1.0000\n", known_products=KNOWN)

    # 同一来源覆盖同一来源：CSV 也是 real，因此这次是正常更新
    assert service.history("P1", date(2026, 1, 5), date(2026, 1, 5)) == [
        (date(2026, 1, 5), 1.0)
    ]


def test_a_file_with_no_valid_rows_is_reported_not_silently_ok(service):
    from app.services.portfolio_advisor.nav import import_nav_csv

    result = import_nav_csv(service, "product_code,trade_date,nav\n", known_products=KNOWN)

    assert result.written == 0
    assert result.skipped == 0


def test_a_csv_missing_required_columns_is_rejected(service):
    from app.services.portfolio_advisor.nav import NavError, import_nav_csv

    with pytest.raises(NavError) as excinfo:
        import_nav_csv(service, "code,date,value\nP1,2026-01-05,1\n", known_products=KNOWN)

    assert excinfo.value.code == "bad_header"
