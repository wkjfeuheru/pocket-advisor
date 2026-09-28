"""对真实 PostgreSQL 幂等应用结构脚本，确认连接与 DDL 可用。

用法::

    python -m scripts.verify_portfolio_live
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对配置的数据库应用 001_schema.sql")
    parser.parse_args(argv)
    from app.core.config import get_postgres_connection_factory
    from app.db.schema import apply_postgres_schema

    connection = get_postgres_connection_factory()()
    try:
        apply_postgres_schema(connection, include_seed=False)
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f"verify_portfolio_live 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    print("verify_portfolio_live: schema apply 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
