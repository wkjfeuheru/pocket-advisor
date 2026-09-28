"""产品库起投金额的真实数据库往返。

假 Session 只能验证 ORM 属性赋值，抓不到「迁移没真正建列」。这里用真实
PostgreSQL 验证建表语句生效，且未披露保持「未知」而不是 0。

需要 ``RUN_POSTGRES_INTEGRATION=1`` 与可访问的 PostgreSQL。
"""

from __future__ import annotations

import os
import uuid

import pytest

if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip(
        "需要 RUN_POSTGRES_INTEGRATION=1 和可访问的真实 PostgreSQL",
        allow_module_level=True,
    )

from app.crud.product_crud import get_product_library  # noqa: E402


def _unique_code() -> str:
    return f"P{uuid.uuid4().hex[:10].upper()}"


def test_min_investment_survives_a_real_round_trip():
    library = get_product_library()
    code = _unique_code()
    try:
        saved = library.upsert_product({
            "code": code,
            "name": "起投金额往返",
            "type": "wealth",
            "min_investment": 50000,
        })
        assert saved is True

        product = library.query_by_code(code)
        assert product["basic_info"]["min_investment"] == 50000.0
    finally:
        library.delete_product(code)


def test_undisclosed_min_investment_reads_back_as_unknown():
    library = get_product_library()
    code = _unique_code()
    try:
        library.upsert_product({"code": code, "name": "未披露起投", "type": "wealth"})

        product = library.query_by_code(code)
        assert product["basic_info"]["min_investment"] is None
    finally:
        library.delete_product(code)
