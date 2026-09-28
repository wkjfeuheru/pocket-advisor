"""产品数据 SQL 种子生成器。

把结构化产品数据（JSON）转换为幂等的产品 SQL。生成结果默认应写到
``migrations/002_product_seed.sql``，该文件不进入自动迁移；演示账在
``migrations/002_seed.sql``。

用法::

    python -m scripts.generate_product_seed \\
        --input scripts/data/products.sample.json \\
        --output migrations/002_product_seed.sql
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

_PRODUCT_FIELDS: tuple[tuple[str, str], ...] = (
    ("code", "text"),
    ("name", "text"),
    ("type", "text"),
    ("establish_date", "text"),
    ("scale", "number"),
    ("manager", "text"),
    ("company", "text"),
    ("management_fee", "number"),
    ("custody_fee", "number"),
    ("subscription_fee", "number"),
    ("redemption_fee", "text"),
    ("risk_level", "text"),
    ("recommended_holding_period", "text"),
    ("investment_target", "text"),
    ("investment_strategy", "text"),
)
_HOLDING_FIELDS: tuple[tuple[str, str], ...] = (
    ("product_code", "text"), ("stock_name", "text"), ("stock_code", "text"),
    ("weight", "number"), ("rank", "number"), ("report_date", "text"),
)
_PERFORMANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("product_code", "text"), ("nav", "number"), ("return_1m", "number"),
    ("return_3m", "number"), ("return_6m", "number"), ("return_1y", "number"),
    ("return_3y", "number"), ("max_drawdown", "number"), ("volatility", "number"),
    ("sharpe_ratio", "number"), ("update_date", "text"),
)


class SeedError(ValueError):
    """输入数据不合法；调用方应报错退出而不产出脚本。"""


def sql_literal(value: Any, kind: str) -> str:
    """把单个值转成安全的 SQL 字面量。"""
    if kind == "number":
        if value is None or value == "":
            return "NULL"
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SeedError(f"数值字段类型非法: {value!r}")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise SeedError(f"数值字段无法解析: {value!r}") from exc
        if not math.isfinite(numeric):
            return "NULL"
        return repr(numeric)
    if kind == "text":
        if value is None:
            return "''"
        if not isinstance(value, str):
            value = str(value)
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    raise SeedError(f"未知字段类型: {kind}")


def _row(values: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> str:
    return "(" + ", ".join(sql_literal(values.get(name), kind) for name, kind in fields) + ")"


def _columns(fields: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(name for name, _ in fields)


def _validate_product(item: dict[str, Any]) -> None:
    if not isinstance(item, dict):
        raise SeedError(f"产品记录必须是对象: {item!r}")
    for required in ("code", "name"):
        if not str(item.get(required, "") or "").strip():
            raise SeedError(f"产品缺少必填字段 {required}: {item!r}")


def _validate_child(item: dict[str, Any], parent_codes: set[str], kind: str) -> None:
    if not isinstance(item, dict):
        raise SeedError(f"{kind} 记录必须是对象: {item!r}")
    code = str(item.get("product_code", "") or "").strip()
    if not code:
        raise SeedError(f"{kind} 记录缺少 product_code: {item!r}")
    if code not in parent_codes:
        raise SeedError(f"{kind} 引用了不存在的产品代码: {code}")


def build_sql(payload: dict[str, Any]) -> str:
    """由结构化数据构建完整的幂等产品 SQL。"""
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise SeedError("products 必须是非空列表")
    for item in products:
        _validate_product(item)
    codes = [str(item["code"]).strip() for item in products]
    if len(codes) != len(set(codes)):
        raise SeedError("产品代码存在重复")
    code_set = set(codes)

    holdings = payload.get("holdings", [])
    performance = payload.get("performance", [])
    if not isinstance(holdings, list):
        raise SeedError("holdings 必须是列表")
    if not isinstance(performance, list):
        raise SeedError("performance 必须是列表")
    for item in holdings:
        _validate_child(item, code_set, "holdings")
    for item in performance:
        _validate_child(item, code_set, "performance")

    statements: list[str] = [
        "-- 由 scripts/generate_product_seed.py 生成，请勿手工编辑。",
        "-- 幂等：重复执行会更新同代码产品/持仓/业绩，不产生重复行。",
        "-- 本文件不在自动迁移清单中；演示账见 migrations/002_seed.sql。",
        "",
    ]

    product_values = ",\n    ".join(_row(item, _PRODUCT_FIELDS) for item in products)
    product_updates = ", ".join(
        f"{name} = EXCLUDED.{name}" for name, _ in _PRODUCT_FIELDS if name != "code"
    )
    statements.append(
        "INSERT INTO finance.products (" + _columns(_PRODUCT_FIELDS) + ") VALUES\n"
        f"    {product_values}\n"
        f"ON CONFLICT (code) DO UPDATE SET {product_updates};\n"
    )

    if holdings:
        holding_values = ",\n    ".join(_row(item, _HOLDING_FIELDS) for item in holdings)
        statements.append(
            "DELETE FROM finance.product_holdings WHERE product_code IN ("
            + ", ".join(sql_literal(code, "text") for code in codes) + ");\n"
        )
        statements.append(
            "INSERT INTO finance.product_holdings (" + _columns(_HOLDING_FIELDS) + ") VALUES\n"
            f"    {holding_values};\n"
        )

    if performance:
        performance_values = ",\n    ".join(_row(item, _PERFORMANCE_FIELDS) for item in performance)
        statements.append(
            "DELETE FROM finance.product_performance WHERE product_code IN ("
            + ", ".join(sql_literal(code, "text") for code in codes) + ");\n"
        )
        statements.append(
            "INSERT INTO finance.product_performance (" + _columns(_PERFORMANCE_FIELDS) + ") VALUES\n"
            f"    {performance_values};\n"
        )

    return "\n".join(statements)


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(f"无法读取输入文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SeedError("输入文件顶层必须是 JSON 对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成产品数据 SQL 种子脚本")
    parser.add_argument("--input", required=True, help="产品数据 JSON 路径")
    parser.add_argument(
        "--output",
        required=True,
        help="输出 SQL 路径（建议 migrations/002_product_seed.sql，不要覆盖 002_seed.sql）",
    )
    args = parser.parse_args(argv)

    try:
        payload = _load(Path(args.input))
        sql = build_sql(payload)
    except SeedError as exc:
        print(f"[product-seed] 输入校验失败：{exc}", file=sys.stderr)
        return 2

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sql, encoding="utf-8", newline="\n")
    print(f"[product-seed] 已生成 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
