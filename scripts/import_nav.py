"""从 CSV 导入真实日频净值。

真实历史的入口。未知产品会被记入问题清单并跳过——**必须把问题清单打出来**，
否则"导入成功"会掩盖没导进去的产品（``product_nav_history.product_code`` 有外键，
静默跳过等于谎报成功）。

CSV 格式（表头必需，列名固定）::

    product_code,trade_date,nav
    110011,2026-01-05,1.2345
    510300,2026-01-05,3.9876

用法::

    python -m scripts.import_nav nav.csv
    python -m scripts.import_nav nav.csv --lenient
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_nav",
        description="把 CSV 里的日频净值写入 finance.product_nav_history（真数据，覆盖合成）。",
    )
    parser.add_argument("path", help="CSV 文件路径")
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="有问题行时仍返回退出码 0；默认有任何问题行即返回 2，便于 CI 拦住脏数据",
    )
    return parser


def load_known_products() -> set[str]:
    """商品库里全部在售产品代码（含无风险等级的）。"""
    from app.crud.product_crud import get_product_library

    return {item["code"] for item in get_product_library().list_products_for_nav()}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"无法读取 {path}: {exc}", file=sys.stderr)
        return 1

    from app.crud.portfolio_advisor_crud import get_nav_store
    from app.services.portfolio_advisor.nav import NavError, NavService, import_nav_csv

    service = NavService(get_nav_store())
    try:
        result = import_nav_csv(service, content, known_products=load_known_products())
    except NavError as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 1

    print(f"写入 {result.written} 行，跳过 {result.skipped} 行")
    for note in result.issues:
        print(f"  ! {note}", file=sys.stderr)

    if result.issues and not args.lenient:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
