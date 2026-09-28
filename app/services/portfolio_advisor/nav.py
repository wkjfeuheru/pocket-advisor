"""日频净值服务：写入、覆盖语义、查询与合成数据生成。

真数据优先覆盖合成：合成数据只用于在拿到真实历史之前把链路跑通，一旦某天有了
真数据，它必须压过那天的合成值；反之合成值**永远不能**覆盖真数据。这条规则是
「验收前清除合成、结果不受污染」能够成立的前提，所以由服务层强制执行，而不是
依赖调用方自觉。

用窄 Protocol + 内存实现做单元测试（与仓库既有"手写 fake + monkeypatch"惯例一致），
真正落到 PostgreSQL 的 ``ON CONFLICT`` 语义另由集成测试覆盖。
"""

from __future__ import annotations

import csv
import io
import math
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Protocol, Sequence

#: 合法数据来源。``real`` 压过 ``synthetic``。
REAL = "real"
SYNTHETIC = "synthetic"
DATA_SOURCES = (REAL, SYNTHETIC)

#: 合成净值序列的默认交易日步长（自然日）。生成时跳过周末。
_WEEKEND = (5, 6)


class NavError(ValueError):
    """净值写入或查询不合法。``code`` 便于上层区分处理。"""

    def __init__(self, message: str, *, code: str = "invalid_nav") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NavRow:
    """一行日频净值。``trade_date`` 是净值生效日。"""

    product_code: str
    trade_date: date
    nav: float
    data_source: str = REAL


class NavStore(Protocol):
    """净值持久化的窄接口。

    只暴露本模块需要的四个操作，便于用内存实现做单元测试；
    PostgreSQL 实现负责把覆盖语义落到 ``ON CONFLICT DO UPDATE ... WHERE`` 上。
    """

    def upsert_many(self, rows: Sequence[NavRow]) -> int:
        """按 (product_code, trade_date) 幂等写入，返回实际写入行数。"""
        ...

    def history(
        self, product_code: str, start: date, end: date
    ) -> list[tuple[date, float]]:
        """返回区间内的 ``(trade_date, nav)``，按日期升序。"""
        ...

    def latest(self, product_code: str) -> tuple[date, float] | None:
        """返回最新一条 ``(trade_date, nav)``，没有则 ``None``。"""
        ...

    def delete_synthetic(self, product_code: str | None = None) -> int:
        """删除合成行。真数据绝不被删除，返回删除行数。"""
        ...


def _validate(row: NavRow) -> None:
    if not row.product_code:
        raise NavError("product_code 不能为空", code="missing_product")
    if row.data_source not in DATA_SOURCES:
        raise NavError(f"未知数据来源: {row.data_source}", code="bad_source")
    if row.nav is None or not math.isfinite(float(row.nav)) or float(row.nav) <= 0:
        raise NavError(f"净值必须为正的有限数: {row.nav!r}", code="bad_nav")


class NavService:
    """净值写入与查询的用例层。"""

    def __init__(self, store: NavStore) -> None:
        self._store = store

    def upsert(self, rows: Iterable[NavRow]) -> int:
        """校验并写入若干行。同批内同一 (code, date) 取最后一条。"""
        deduped: dict[tuple[str, date], NavRow] = {}
        for row in rows:
            _validate(row)
            deduped[(row.product_code, row.trade_date)] = row
        if not deduped:
            return 0
        return self._store.upsert_many(list(deduped.values()))

    def history(self, product_code: str, start: date, end: date) -> list[tuple[date, float]]:
        if end < start:
            raise NavError("结束日期早于开始日期", code="bad_range")
        return self._store.history(product_code, start, end)

    def latest(self, product_code: str) -> tuple[date, float] | None:
        return self._store.latest(product_code)

    def as_of(self, product_code: str) -> date | None:
        """该产品最新净值的生效日，用于页面标注 ``nav_as_of``。"""
        found = self._store.latest(product_code)
        return found[0] if found else None

    def clear_synthetic(self, product_code: str | None = None) -> int:
        """清除合成数据。真数据不受影响。"""
        return self._store.delete_synthetic(product_code)


def trading_days(start: date, end: date) -> list[date]:
    """区间内的交易日（跳过周六周日）。不含节假日——法定节假日不在本函数职责内。"""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() not in _WEEKEND:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def normalize_risk_level(value: object, *, default: str = "R3") -> str:
    """把任意风险等级写法归一成 ``R1``–``R5``。

    真实数据里这个字段并不规整：``finance.products.risk_level`` 存的是带中文说明的
    字符串（如 ``"R3 中风险"``），迁移种子与 API 又可能给纯 ``"R3"``。
    早期实现直接拿原串去查波动档位表，结果**所有产品都落到默认档**，
    合成序列形状完全一样——看起来"生成成功"，实际风险分档失效。

    取首个匹配到的 ``R1``–``R5`` 片段；都匹配不到时返回 ``default``。
    """
    text = str(value or "").strip().upper()
    if not text:
        return default
    match = re.search(r"R[1-5]", text)
    return match.group(0) if match else default


def generate_synthetic_nav(
    product_codes: Sequence[str],
    *,
    start: date,
    end: date,
    seed: int = 20260927,
    annual_drift: float = 0.04,
    annual_volatility_by_risk: Mapping[str, float] | None = None,
    risk_levels: Mapping[str, str] | None = None,
) -> list[NavRow]:
    """生成合成日频净值（几何布朗运动），``data_source='synthetic'``。

    波动按产品风险等级分档，风险越高波动越大。同一 ``seed`` 必然得到同一序列，
    因此合成链路可复现。``start`` 那天的净值固定为 1.0。
    """
    volatility_by_risk = {
        "R1": 0.02,
        "R2": 0.08,
        "R3": 0.15,
        "R4": 0.25,
        "R5": 0.35,
    }
    if annual_volatility_by_risk:
        volatility_by_risk.update(annual_volatility_by_risk)
    levels = dict(risk_levels or {})

    days = trading_days(start, end)
    if not days:
        raise NavError("区间内没有交易日", code="empty_range")

    # 每交易日步长：把年化漂移与波动折算到日频。
    dt = 1.0 / 252.0
    rows: list[NavRow] = []
    for index, product_code in enumerate(sorted(product_codes)):
        level = normalize_risk_level(levels.get(product_code))
        annual_vol = volatility_by_risk.get(level, volatility_by_risk["R3"])
        rng = random.Random(f"{seed}:{product_code}")
        nav = 1.0
        for day in days:
            rows.append(
                NavRow(
                    product_code=product_code,
                    trade_date=day,
                    nav=round(nav, 6),
                    data_source=SYNTHETIC,
                )
            )
            shock = rng.gauss(0.0, 1.0)
            nav *= math.exp(
                (annual_drift - 0.5 * annual_vol**2) * dt + annual_vol * math.sqrt(dt) * shock
            )
    return rows


@dataclass(frozen=True)
class NavImportResult:
    """一次导入的结果。``issues`` 是逐行问题描述，必须回显给用户。"""

    written: int
    skipped: int
    issues: list[str]
    rows: list[NavRow]


#: CSV 必需列。列名固定，不做模糊匹配——导入是运维操作，宁可报错也不要猜。
_REQUIRED_COLUMNS = ("product_code", "trade_date", "nav")


def parse_nav_csv(
    content: str, *, known_products: set[str] | None = None
) -> tuple[list[NavRow], list[str]]:
    """解析净值 CSV，返回 ``(有效行, 问题清单)``。

    逐行校验并收集问题而不是遇到第一个错就中断：运维一次导入几百行，
    只报一个错会让人反复试。至少表头必须是 ``product_code,trade_date,nav``。
    """
    reader = csv.DictReader(io.StringIO(content))
    header = [str(name).strip() for name in (reader.fieldnames or [])]
    missing = [name for name in _REQUIRED_COLUMNS if name not in header]
    if missing:
        raise NavError(
            f"CSV 缺少必需列: {missing}，实际表头为 {header}", code="bad_header"
        )

    rows: list[NavRow] = []
    issues: list[str] = []
    for index, raw in enumerate(reader, start=2):  # 表头占第 1 行
        record = {
            str(key).strip(): ("" if value is None else str(value).strip())
            for key, value in raw.items()
            if key is not None
        }
        if not any(record.values()):
            continue  # 空行直接跳过，不算问题

        code = record.get("product_code") or ""
        if not code:
            issues.append(f"第 {index} 行: 缺少 product_code")
            continue
        if known_products is not None and code not in known_products:
            issues.append(f"第 {index} 行 [{code}]: 产品不在商品库中")
            continue

        text = record.get("trade_date") or ""
        try:
            trade_date = date.fromisoformat(text)
        except ValueError:
            issues.append(
                f"第 {index} 行 [{code}]: 日期非法: {text!r}（需要 YYYY-MM-DD）"
            )
            continue

        raw_nav = record.get("nav") or ""
        try:
            nav = float(raw_nav)
        except ValueError:
            issues.append(f"第 {index} 行 [{code} {text}]: 净值非法: {raw_nav!r}")
            continue
        if not math.isfinite(nav) or nav <= 0:
            issues.append(f"第 {index} 行 [{code} {text}]: 净值必须为正的有限数: {raw_nav!r}")
            continue

        rows.append(NavRow(code, trade_date, nav, REAL))

    return rows, issues


def import_nav_csv(
    service: NavService, content: str, *, known_products: set[str] | None = None
) -> NavImportResult:
    """解析并写入净值 CSV。

    未知产品会被记入 ``issues`` 并跳过——它同时会出现在 ``skipped`` 里，
    调用方必须把 ``issues`` 回显出来，否则"导入成功"会掩盖没导进去的产品。
    """
    rows, issues = parse_nav_csv(content, known_products=known_products)
    written = service.upsert(rows) if rows else 0
    return NavImportResult(
        written=written,
        skipped=len(issues),
        issues=issues,
        rows=rows,
    )


def seed_synthetic(
    service: NavService,
    *,
    products: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    seed: int = 20260927,
) -> int:
    """给整个产品池批量播种合成日频净值，返回写入行数。

    ``products`` 是产品池字典序列，每项至少要有 ``code``；``risk_level`` 用来分档波动，
    缺失或为空时按 R3 处理（货架上确实有产品没有这个字段，不该因此报错）。

    播种走的是 ``NavService.upsert``，因此**合成数据不会覆盖已有的真数据**——
    这条不由本函数单独实现，而是复用统一的覆盖规则，避免两处判断不一致。
    """
    pool: list[Mapping[str, Any]] = [item for item in products if item.get("code")]
    if not pool:
        raise NavError("产品池为空，无法播种合成数据", code="empty_pool")

    codes = [str(item["code"]) for item in pool]
    risk_levels = {
        str(item["code"]): str(item.get("risk_level") or "R3") for item in pool
    }

    rows = generate_synthetic_nav(codes, start=start, end=end, seed=seed, risk_levels=risk_levels)
    return service.upsert(rows)


__all__ = [
    "DATA_SOURCES",
    "REAL",
    "SYNTHETIC",
    "NavError",
    "NavImportResult",
    "NavRow",
    "NavService",
    "NavStore",
    "generate_synthetic_nav",
    "import_nav_csv",
    "normalize_risk_level",
    "parse_nav_csv",
    "seed_synthetic",
    "trading_days",
]
