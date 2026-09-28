"""产品数据 SQL 种子生成器测试。

重点验证：转义安全（引号加倍、拒绝拼接）、必填校验（不产出半截脚本）、
外键顺序与幂等语义。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_product_seed import SeedError, build_sql, sql_literal

REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(products=None, holdings=None, performance=None):
    return {
        "products": products if products is not None else [
            {"code": "P001", "name": "示例基金", "risk_level": "R2", "recommended_holding_period": "long"},
        ],
        "holdings": holdings if holdings is not None else [],
        "performance": performance if performance is not None else [],
    }


# ── 转义 ─────────────────────────────────────────────────────────────────────

def test_text_literal_doubles_single_quotes():
    assert sql_literal("O'Brien", "text") == "'O''Brien'"
    assert sql_literal("'; DROP TABLE finance.products; --", "text") == (
        "'''; DROP TABLE finance.products; --'"
    )


def test_number_literal_rejects_non_finite_and_accepts_values():
    assert sql_literal(1.5, "number") == "1.5"
    assert sql_literal("2", "number") == "2.0"
    assert sql_literal(None, "number") == "NULL"
    assert sql_literal("", "number") == "NULL"
    assert sql_literal(float("nan"), "number") == "NULL"
    assert sql_literal(float("inf"), "number") == "NULL"


def test_number_literal_rejects_garbage():
    with pytest.raises(SeedError):
        sql_literal("not-a-number", "number")
    with pytest.raises(SeedError):
        sql_literal(True, "number")


def test_injection_string_is_neutralized_in_generated_sql():
    payload = _payload(products=[
        {"code": "P001", "name": "'; DROP TABLE finance.products; --"},
    ])

    sql = build_sql(payload)

    # 原始注入片段不能以未转义形式出现；引号必须被加倍。
    assert "'''; DROP TABLE finance.products; --'" in sql
    assert "';\nDROP TABLE" not in sql


# ── 必填校验：不产出半截脚本 ──────────────────────────────────────────────────

def test_missing_required_field_raises_before_any_sql():
    for bad in ({"code": "", "name": "x"}, {"code": "P001", "name": ""}, {"name": "x"}):
        with pytest.raises(SeedError):
            build_sql(_payload(products=[bad]))


def test_empty_products_raises():
    with pytest.raises(SeedError):
        build_sql({"products": []})
    with pytest.raises(SeedError):
        build_sql({})


def test_duplicate_product_codes_raise():
    with pytest.raises(SeedError):
        build_sql(_payload(products=[
            {"code": "P001", "name": "A"}, {"code": "P001", "name": "B"},
        ]))


def test_child_referencing_unknown_product_raises():
    with pytest.raises(SeedError):
        build_sql(_payload(
            products=[{"code": "P001", "name": "A"}],
            holdings=[{"product_code": "P999", "stock_name": "x"}],
        ))
    with pytest.raises(SeedError):
        build_sql(_payload(
            products=[{"code": "P001", "name": "A"}],
            performance=[{"product_code": "P999"}],
        ))


# ── 结构：外键顺序与幂等 ─────────────────────────────────────────────────────

def test_generated_sql_orders_products_before_children_and_is_idempotent():
    sql = build_sql(_payload(
        products=[{"code": "P001", "name": "A"}],
        holdings=[{"product_code": "P001", "stock_name": "x", "weight": 1.0, "rank": 1}],
        performance=[{"product_code": "P001", "return_1y": 0.1}],
    ))

    assert sql.index("INSERT INTO finance.products") < sql.index("INSERT INTO finance.product_holdings")
    assert sql.index("INSERT INTO finance.product_holdings") < sql.index("INSERT INTO finance.product_performance")
    assert "ON CONFLICT (code) DO UPDATE SET" in sql
    # 明细表按产品先清后写，保证幂等而不留旧快照。
    assert sql.count("DELETE FROM finance.product_holdings") == 1
    assert sql.count("DELETE FROM finance.product_performance") == 1


# ── CLI：失败不产出文件 ───────────────────────────────────────────────────────

def test_cli_rejects_invalid_input_without_writing_output(work_dir):
    bad_input = work_dir / "bad.json"
    bad_input.write_text(json.dumps({"products": [{"code": "", "name": ""}]}), encoding="utf-8")
    output = work_dir / "out.sql"

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.generate_product_seed",
         "--input", str(bad_input), "--output", str(output)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 2
    assert not output.exists(), "校验失败时不得产出脚本"
    assert "输入校验失败" in completed.stderr


def test_cli_generates_from_sample_data(work_dir):
    output = work_dir / "seed.sql"

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.generate_product_seed",
         "--input", str(REPO_ROOT / "scripts" / "data" / "products.sample.json"),
         "--output", str(output)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 0, completed.stderr
    sql = output.read_text(encoding="utf-8")
    assert "INSERT INTO finance.products" in sql
    assert "510300" in sql
