"""给整个产品池播种合成日频净值（开发与演示用）。

真实历史到位前，先用合成数据把整条链路跑通（设计文档「数据管线」）。
合成数据带 ``data_source='synthetic'``，**永远不会覆盖已有真数据**，且可被
``scripts/seed_nav.py --clear`` 一键清除。

用法::

    python -m scripts.seed_nav --days 180
    python -m scripts.seed_nav --days 180 --seed 7
    python -m scripts.seed_nav --clear
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from app.services.portfolio_advisor.nav import (
    NavService,
    seed_synthetic,
    trading_days,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_nav",
        description="给产品池播种合成日频净值，或清除合成数据。",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="回溯的自然日数，默认 180（约 6 个月，足够演示与 3 个月回测）",
    )
    parser.add_argument("--seed", type=int, default=20260927, help="随机种子，同种子同结果")
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="区间结束日 YYYY-MM-DD，默认今天",
    )
    parser.add_argument("--clear", action="store_true", help="只清除合成数据，不播种")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from app.crud.portfolio_advisor_crud import get_nav_store
    from app.crud.product_crud import get_product_library

    service = NavService(get_nav_store())

    if args.clear:
        removed = service.clear_synthetic()
        print(f"已清除合成净值 {removed} 行（真数据不受影响）")
        return 0

    try:
        end = date.fromisoformat(args.end) if args.end else date.today()
    except ValueError:
        print(f"--end 日期非法: {args.end!r}（需要 YYYY-MM-DD）", file=sys.stderr)
        return 1

    if args.days <= 0:
        print("--days 必须为正", file=sys.stderr)
        return 1

    start = end - timedelta(days=args.days)
    pool = get_product_library().list_products_for_nav()
    if not pool:
        print("产品池为空，请先执行 python -m scripts.migrate --seed", file=sys.stderr)
        return 1

    written = seed_synthetic(service, products=pool, start=start, end=end, seed=args.seed)
    days = len(trading_days(start, end))
    print(
        f"合成净值播种完成: {len(pool)} 个产品 × {days} 个交易日 = 写入 {written} 行"
        f"（data_source=synthetic，区间 {start} ~ {end}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
