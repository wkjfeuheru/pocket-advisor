"""产品库数据访问的 ORM 行为契约测试。

用轻量假 Session 记录 ORM 操作，不依赖真实数据库：
- 按代码批量读取去重、保序、丢弃未知代码，且查询次数不随标的数增长；
- 读取端把 NUMERIC 起投金额转成 JSON 数值，未披露保持「未知」；
- 商品上架保存必须把 ``recommended_holding_period`` / ``min_investment`` 写进 ORM 行。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.crud.product_crud import PostgresProductLibrary
from app.db.models.product import Product, ProductHolding, ProductPerformance


class _FakeResult:
    """``session.scalars(...)`` 的返回值：同时支持 ``all()`` / ``first()`` / 迭代。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """按实体分派的最小会话替身：记录语句，按查询实体返回预置行。"""

    def __init__(
        self,
        *,
        products: list[Any] | None = None,
        performance: list[Any] | None = None,
        holdings: list[Any] | None = None,
    ) -> None:
        self._by_entity = {
            Product: list(products or []),
            ProductPerformance: list(performance or []),
            ProductHolding: list(holdings or []),
        }
        self.added: list[Any] = []
        self.statements: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def get(self, entity: Any, key: Any, **kwargs: Any) -> Any:
        for row in self._by_entity.get(entity, []):
            if getattr(row, "code", None) == key:
                return row
        return None

    def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        entity = _entity_of(statement)
        rows = self._by_entity.get(entity, [])
        return type("Result", (), {"all": lambda _self: list(rows)})()

    def scalars(self, statement: Any) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self._by_entity.get(_entity_of(statement), []))

    def flush(self) -> None:
        pass

    def refresh(self, obj: Any) -> None:
        pass


def _entity_of(statement: Any) -> Any:
    descriptions = getattr(statement, "column_descriptions", None) or []
    if descriptions:
        return descriptions[0].get("entity")
    return None


class _SessionScope:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc: Any) -> bool:
        return False


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    import app.crud.product_crud as product_module

    monkeypatch.setattr(product_module, "session_scope", lambda: _SessionScope(session))
    monkeypatch.setattr(product_module, "ensure_schema", lambda: None)


def _product_row(**overrides: Any) -> Product:
    """一条 ``finance.products`` 行；起投金额默认写成 Decimal，模拟 NUMERIC 读回。"""
    values: dict[str, Any] = {
        "code": "P001",
        "name": "示例理财",
        "type": "wealth",
        "min_investment": Decimal("10000.00"),
    }
    values.update(overrides)
    return Product(**values)


def _performance_row(code: str, row_id: int, **overrides: Any) -> ProductPerformance:
    values: dict[str, Any] = {
        "id": row_id,
        "product_code": code,
        "nav": 1.2,
        "return_1y": 0.126,
        "max_drawdown": 0.187,
        "update_date": "2026-09-25",
    }
    values.update(overrides)
    return ProductPerformance(**values)


def test_query_by_codes_deduplicates_codes_and_preserves_found_products(monkeypatch):
    session = _FakeSession(products=[
        _product_row(code="P001", name="甲"),
        _product_row(code="P002", name="乙"),
    ])
    _patch_session(monkeypatch, session)

    products = PostgresProductLibrary().query_by_codes(["P002", "P001", "P002", "P404", ""])

    assert [item["basic_info"]["code"] for item in products] == ["P002", "P001"]
    assert [item["basic_info"]["name"] for item in products] == ["乙", "甲"]


def test_query_by_codes_uses_a_constant_number_of_queries(monkeypatch):
    """批量读取的查询次数不随标的数增长——这是要拆掉的 N+1。"""
    single = _FakeSession(products=[_product_row(code="P001")])
    _patch_session(monkeypatch, single)
    PostgresProductLibrary().query_by_codes(["P001"])

    several = _FakeSession(products=[_product_row(code=f"P00{i}") for i in range(1, 6)])
    _patch_session(monkeypatch, several)
    PostgresProductLibrary().query_by_codes([f"P00{i}" for i in range(1, 6)])

    assert len(several.statements) == len(single.statements)


def test_query_by_codes_attaches_the_latest_performance_per_product(monkeypatch):
    session = _FakeSession(
        products=[_product_row(code="P001"), _product_row(code="P002")],
        performance=[
            _performance_row("P001", 1, return_1y=0.05),
            _performance_row("P001", 2, return_1y=0.126),
            _performance_row("P002", 3, return_1y=0.201),
        ],
    )
    _patch_session(monkeypatch, session)

    products = PostgresProductLibrary().query_by_codes(["P001", "P002"])
    returns = {item["basic_info"]["code"]: item["performance"]["return_1y"] for item in products}

    assert returns == {"P001": 0.126, "P002": 0.201}


def test_upsert_product_persists_recommended_holding_period(monkeypatch):
    session = _FakeSession()
    _patch_session(monkeypatch, session)

    saved = PostgresProductLibrary().upsert_product({
        "code": "P001",
        "name": "示例基金",
        "recommended_holding_period": "long",
    })

    assert saved is True
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, Product)
    assert row.code == "P001"
    assert row.name == "示例基金"
    assert row.recommended_holding_period == "long"


def test_upsert_product_persists_min_investment(monkeypatch):
    """起投金额必须落库：预算硬过滤依赖它，缺失时不能编造。"""
    session = _FakeSession()
    _patch_session(monkeypatch, session)

    saved = PostgresProductLibrary().upsert_product({
        "code": "P001",
        "name": "示例理财",
        "min_investment": 10000.0,
    })

    assert saved is True
    assert session.added[0].min_investment == 10000.0


def test_query_by_code_exposes_min_investment_as_a_json_number(monkeypatch):
    """读路径必须给出 JSON 数值：Decimal 会被工具序列化成字符串，前端无法比较。"""
    session = _FakeSession(products=[_product_row()])
    _patch_session(monkeypatch, session)

    product = PostgresProductLibrary().query_by_code("P001")

    value = product["basic_info"]["min_investment"]
    assert isinstance(value, float), f"起投金额应为 float，实际是 {type(value).__name__}"
    assert value == 10000.0


def test_query_by_code_keeps_undisclosed_min_investment_unknown(monkeypatch):
    """未披露必须保持 None（未知），不能变成 0——否则会被当成「零门槛」参与预算过滤。"""
    session = _FakeSession(products=[_product_row(min_investment=None)])
    _patch_session(monkeypatch, session)

    product = PostgresProductLibrary().query_by_code("P001")

    assert product["basic_info"]["min_investment"] is None


def test_migration_declares_min_investment_for_new_and_existing_databases():
    """老库不会因 ``CREATE TABLE IF NOT EXISTS`` 拿到新列，幂等 ALTER 必须同时存在。"""
    from app.db.schema import _load_sql

    sql = _load_sql("001_schema.sql")
    assert "min_investment numeric(18, 2)" in sql
    assert "ADD COLUMN IF NOT EXISTS min_investment numeric(18, 2)" in sql
