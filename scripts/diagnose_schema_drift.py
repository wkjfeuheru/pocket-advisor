"""库表漂移诊断：对比「DDL 声明的期望结构」与「线上实际结构」。

做法：把 ``migrations/`` 里的建表脚本应用到一个一次性探针库，作为权威期望结构，
再与线上库逐表逐列对比。种子脚本不参与对比。

用法::

    python -m scripts.diagnose_schema_drift
    python -m scripts.diagnose_schema_drift --keep-probe

退出码：发现缺失表/列或类型不一致时为 1，否则为 0。
线上库只执行只读查询；探针库是一次性的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

from app.core.config import postgres_dsn
from app.db.schema import BUSINESS_SCHEMA, SCHEMA_APPLY_ORDER

PROBE_DB = "advisor_schema_probe"
SQL_DIR = Path(__file__).resolve().parents[1] / "migrations"
SCHEMA_FILES = list(SCHEMA_APPLY_ORDER)
EXPECTED_LAZY_COLUMNS: dict[tuple[str, str], str] = {}


def _columns(conn, schema: str) -> dict[tuple[str, str], tuple[str, str, str, str]]:
    """读取列清单：{(表, 列): (类型, 是否可空, 默认值, 字符长度)}。"""
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT table_name, column_name, data_type, is_nullable,
                      COALESCE(column_default, ''), COALESCE(character_maximum_length, 0)
               FROM information_schema.columns
               WHERE table_schema = %s
               ORDER BY table_name, ordinal_position""",
            (schema,),
        )
        return {
            (row[0], row[1]): (row[2], row[3], row[4], str(row[5]))
            for row in cur.fetchall()
        }
    finally:
        cur.close()


def _tables(conn, schema: str) -> set[str]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        )
        return {row[0] for row in cur.fetchall()}
    finally:
        cur.close()


def build_probe(dsn: str) -> None:
    """把建表脚本应用到探针库，得到权威期望结构。"""
    conn = psycopg.connect(dsn)
    try:
        cur = conn.cursor()
        for filename in SCHEMA_FILES:
            text = (SQL_DIR / filename).read_text(encoding="utf-8")
            try:
                cur.execute(text)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - 逐个报告，不掩盖
                conn.rollback()
                print(f"  [跳过] {filename}: {type(exc).__name__}: {exc}")
        cur.close()
    finally:
        conn.close()


def _admin_dsn(dsn: str, database: str) -> str:
    """把连接串里的库名换成 ``database``。只处理 URL 形式。"""
    if "://" not in dsn:
        raise RuntimeError("诊断需要 URL 形式的 POSTGRES_DSN 或由分项参数拼出的 DSN")
    prefix, rest = dsn.split("://", 1)
    if "/" not in rest:
        raise RuntimeError(f"无法解析数据库名: {dsn}")
    head, _db = rest.rsplit("/", 1)
    query = ""
    if "?" in _db:
        _db, query = _db.split("?", 1)
        query = "?" + query
    return f"{prefix}://{head}/{database}{query}"


def _ensure_probe_db(admin_dsn: str, probe_dsn: str, *, keep: bool) -> None:
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (PROBE_DB,),
        )
        exists = cur.fetchone() is not None
        if exists and not keep:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (PROBE_DB,),
            )
            cur.execute(f'DROP DATABASE "{PROBE_DB}"')
        if not exists or not keep:
            cur.execute(f'CREATE DATABASE "{PROBE_DB}"')
        cur.close()
    finally:
        conn.close()
    build_probe(probe_dsn)


def _drop_probe(admin_dsn: str) -> None:
    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (PROBE_DB,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{PROBE_DB}"')
        cur.close()
    finally:
        conn.close()


def compare(live_dsn: str, probe_dsn: str) -> list[str]:
    """返回期望与线上不一致的说明。"""
    live = psycopg.connect(live_dsn)
    probe = psycopg.connect(probe_dsn)
    try:
        expected_tables = _tables(probe, BUSINESS_SCHEMA)
        actual_tables = _tables(live, BUSINESS_SCHEMA)
        expected_columns = _columns(probe, BUSINESS_SCHEMA)
        actual_columns = _columns(live, BUSINESS_SCHEMA)
    finally:
        live.close()
        probe.close()

    problems: list[str] = []
    for table in sorted(expected_tables - actual_tables):
        problems.append(f"缺少表 {BUSINESS_SCHEMA}.{table}")
    for key, expected in sorted(expected_columns.items()):
        table, column = key
        if (table, column) in EXPECTED_LAZY_COLUMNS:
            continue
        actual = actual_columns.get(key)
        if actual is None:
            problems.append(f"缺少列 {BUSINESS_SCHEMA}.{table}.{column}")
        elif actual[0] != expected[0]:
            problems.append(
                f"类型不一致 {BUSINESS_SCHEMA}.{table}.{column}: "
                f"期望 {expected[0]}，实际 {actual[0]}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比迁移声明的结构与线上库")
    parser.add_argument("--keep-probe", action="store_true", help="保留探针库")
    args = parser.parse_args(argv)
    live_dsn = postgres_dsn()
    admin_dsn = _admin_dsn(live_dsn, "postgres")
    probe_dsn = _admin_dsn(live_dsn, PROBE_DB)
    _ensure_probe_db(admin_dsn, probe_dsn, keep=args.keep_probe)
    try:
        problems = compare(live_dsn, probe_dsn)
    finally:
        if not args.keep_probe:
            _drop_probe(admin_dsn)
    if problems:
        print("schema drift:")
        for item in problems:
            print(f"  {item}")
        return 1
    print("schema drift: 无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
