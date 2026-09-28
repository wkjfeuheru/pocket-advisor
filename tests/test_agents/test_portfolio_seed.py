"""合成日频数据的生成与清除（整条验收一链路的数据底座）。

合成数据只有一个用途：在拿到真实历史之前把链路跑通。因此它必须
**可复现**（同 seed 同结果，便于对比两次运行）且**可一键清除**，并且绝不能
污染真数据——最后一条由 ``NavService`` 的覆盖规则保证，这里验证它在批量播种路径上
同样成立。

═══════════════════════════════════════════════════════════════════════════
derivation —— 播种规模
═══════════════════════════════════════════════════════════════════════════
池：3 个产品（两个 fund、一个 etf）。
区间：2026-01-05（周一）~ 2026-01-09（周五）= 5 个交易日。

    行数 = 3 产品 × 5 交易日 = 15

起点净值固定为 1.0，之后按风险等级演化：
    P1 R1（低波动）→ 波动最小
    P2 R3
    P3 R5（高波动）→ 波动最大

风险等级缺失时按 R3 处理，不抛错——货架上有些产品本来就没有风险等级字段。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.portfolio_advisor.nav import (
    REAL,
    SYNTHETIC,
    NavRow,
    NavService,
    seed_synthetic,
)


class _FakeNavStore:
    """覆盖语义与 PostgreSQL 实现对齐的内存存储。"""

    def __init__(self):
        self.rows: dict[tuple[str, date], NavRow] = {}

    def upsert_many(self, rows):
        written = 0
        for row in rows:
            key = (row.product_code, row.trade_date)
            existing = self.rows.get(key)
            # 合成值永远不能覆盖真数据。
            if existing is not None and existing.data_source == REAL and row.data_source == SYNTHETIC:
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
            if row.data_source == SYNTHETIC
            and (product_code is None or key[0] == product_code)
        ]
        for key in doomed:
            del self.rows[key]
        return len(doomed)


POOL = [
    {"code": "P1", "name": "低风险债基", "type": "fund", "risk_level": "R1"},
    {"code": "P2", "name": "混合基金", "type": "fund", "risk_level": "R3"},
    {"code": "P3", "name": "高波动ETF", "type": "etf", "risk_level": "R5"},
]

START = date(2026, 1, 5)
END = date(2026, 1, 9)


@pytest.fixture
def service():
    return NavService(_FakeNavStore())


def test_seeding_covers_every_product_and_trading_day(service):
    written = seed_synthetic(service, products=POOL, start=START, end=END, seed=42)

    assert written == 3 * 5
    for item in POOL:
        history = service.history(item["code"], START, END)
        assert len(history) == 5
        # 起点净值固定 1.0，且逐日有变化（不是一条持平直线）
        assert history[0][1] == pytest.approx(1.0)
        assert len({nav for _, nav in history}) > 1


def test_seeding_is_reproducible_for_the_same_seed(service):
    seed_synthetic(service, products=POOL, start=START, end=END, seed=7)
    first = service.history("P2", START, END)

    seed_synthetic(service, products=POOL, start=START, end=END, seed=7)
    assert service.history("P2", START, END) == first


def test_a_riskier_product_gets_a_wider_swing(service):
    seed_synthetic(service, products=POOL, start=START, end=END, seed=99)

    def spread(code):
        values = [nav for _, nav in service.history(code, START, END)]
        return max(values) - min(values)

    assert spread("P3") > spread("P2") > spread("P1")


def test_a_risk_level_with_a_chinese_suffix_still_tiers_the_volatility(service):
    """真实库里 risk_level 是 ``"R3 中风险"`` 这种带说明的串，不是纯 ``R1``–``R5``。

    早期实现按原串查波动档位表，全部落到默认档 → 所有产品合成序列形状一样，
    看起来"生成成功"但风险分档失效。这里用真实的字段形状钉住它。
    """
    pool = [
        {"code": "LOW", "name": "低风险", "type": "fund", "risk_level": "R1 低风险"},
        {"code": "HIGH", "name": "高风险", "type": "fund", "risk_level": "R5 高风险"},
    ]

    seed_synthetic(service, products=pool, start=START, end=END, seed=5)

    def spread(code):
        values = [nav for _, nav in service.history(code, START, END)]
        return max(values) - min(values)

    # 若两个都落到默认档，两者会完全相等
    assert spread("HIGH") > spread("LOW")


def test_normalize_risk_level_accepts_the_shapes_actually_seen_in_data():
    from app.services.portfolio_advisor.nav import normalize_risk_level

    assert normalize_risk_level("R1") == "R1"
    assert normalize_risk_level("r5") == "R5"
    assert normalize_risk_level("R3 中风险") == "R3"
    assert normalize_risk_level("R4 中高风险") == "R4"
    # 缺失或无法识别时回退到 R3，而不是抛错
    assert normalize_risk_level("") == "R3"
    assert normalize_risk_level(None) == "R3"
    assert normalize_risk_level("未披露") == "R3"


def test_a_missing_risk_level_falls_back_instead_of_raising(service):
    pool = [{"code": "PN", "name": "无风险等级", "type": "wealth", "risk_level": ""}]

    written = seed_synthetic(service, products=pool, start=START, end=END, seed=1)

    assert written == 5


def test_seeding_never_overwrites_real_nav(service, ):
    real_day = date(2026, 1, 6)
    service.upsert([NavRow("P2", real_day, 9.75, REAL)])

    seed_synthetic(service, products=POOL, start=START, end=END, seed=42)

    # 真数据必须原样保留，周围的日子仍然是合成值
    assert service.history("P2", real_day, real_day) == [(real_day, 9.75)]
    history = dict(service.history("P2", START, END))
    assert history[real_day] == 9.75


def test_clearing_removes_every_synthetic_row(service):
    seed_synthetic(service, products=POOL, start=START, end=END, seed=42)
    service.upsert([NavRow("P1", date(2026, 1, 5), 3.33, REAL)])

    removed = service.clear_synthetic()

    assert removed == 3 * 5 - 1  # 被真数据覆盖的那一天不算合成
    assert service.latest("P1") == (date(2026, 1, 5), 3.33)
    assert service.latest("P2") is None


def test_an_empty_pool_is_rejected():
    service = NavService(_FakeNavStore())

    with pytest.raises(Exception) as excinfo:
        seed_synthetic(service, products=[], start=START, end=END)

    assert "product" in str(excinfo.value).lower() or "池" in str(excinfo.value)
