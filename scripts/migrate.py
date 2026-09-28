"""统一、可在容器启动时执行的 schema apply。

``migrations/*.sql`` 已是幂等 DDL 事实源。本模块是部署入口，失败显式退出。

用法::

    python -m scripts.migrate
    python -m scripts.migrate --seed
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate",
        description="幂等应用 migrations/ 结构脚本；可选演示种子数据。",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="同时应用 migrations/002_seed.sql（产品、演示账号与模拟持仓）",
    )
    return parser


def apply_from_config(*, include_seed: bool) -> None:
    """用应用配置连接数据库并应用 schema。失败抛出，不吞异常。"""
    from app.core.config import get_postgres_connection_factory
    from app.db.schema import apply_postgres_schema

    connection = get_postgres_connection_factory()()
    try:
        apply_postgres_schema(connection, include_seed=include_seed)
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        apply_from_config(include_seed=args.seed)
    except Exception as exc:
        print(f"schema apply 失败: {exc}", file=sys.stderr)
        return 1
    print("schema apply 完成" + ("（含演示种子）" if args.seed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
