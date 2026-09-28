"""刷新股票特征快照。只由运维 CLI 调用，Web 进程不得调度。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from typing import Any, Protocol


class FeatureSourceUnavailable(RuntimeError):
    """生产特征源不可用。绝不返回占位或补零因子。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FeatureSource(Protocol):
    def fetch_rows(self, as_of: date) -> list[dict[str, Any]]:
        ...


class EmptyFeatureSource:
    """测试与未接上游时的占位；不编造因子。"""

    def fetch_rows(self, as_of: date) -> list[dict[str, Any]]:
        return []


def parse_as_of(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class FuyaoFeatureSource:
    """生产默认源：把 Fuyao 只读客户端折算成可追溯的横截面因子行。

    只接受带 ``thscode``、``source``、``as_of`` 的行；没有数据时抛出
    :class:`FeatureSourceUnavailable`，由刷新命令记失败，绝不发布空快照。
    """

    def __init__(self, client: Any = None) -> None:
        self.client = client

    def fetch_rows(self, as_of: date) -> list[dict[str, Any]]:
        if self.client is None or not hasattr(self.client, "feature_rows"):
            raise FeatureSourceUnavailable("feature_source_unconfigured")
        loaded = self.client.feature_rows(as_of)
        if hasattr(loaded, "__await__"):
            loaded = asyncio.run(loaded)
        rows = []
        for row in loaded or []:
            if row.get("source") and row.get("as_of") and row.get("thscode"):
                rows.append(dict(row))
        return rows


def _fetch_rows(source: FeatureSource, as_of: date) -> list[dict[str, Any]]:
    loaded = source.fetch_rows(as_of)
    if hasattr(loaded, "__await__"):
        loaded = asyncio.run(loaded)
    return list(loaded or [])


def run_refresh(*, as_of: date, store: Any, source: FeatureSource | None = None) -> dict[str, Any]:
    stamp = f"{as_of.isoformat()}T15:00:00+08:00"
    try:
        rows = _fetch_rows(source or FuyaoFeatureSource(), as_of)
    except FeatureSourceUnavailable as exc:
        building = store.begin_snapshot(as_of, stamp)
        return store.fail_snapshot(building["id"], exc.code)
    if not rows:
        building = store.begin_snapshot(as_of, stamp)
        return store.fail_snapshot(building["id"], "empty_feature_source")
    return store.refresh(as_of, rows=rows, source_as_of=stamp)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.refresh_stock_features")
    parser.add_argument("--as-of", required=True, help="交易日 YYYY-MM-DD")
    return parser


def main(argv: list[str] | None = None, *, store: Any = None, source: FeatureSource | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = parse_as_of(args.as_of)
    if store is None:
        from app.crud.stock_feature_crud import get_stock_feature_store

        store = get_stock_feature_store()
    snapshot = run_refresh(as_of=as_of, store=store, source=source)
    if snapshot.get("status") != "ready":
        print(
            f"snapshot_failed id={snapshot.get('id')} code={snapshot.get('error_code')}",
            file=sys.stderr,
        )
        return 1
    print(f"snapshot_ready id={snapshot['id']} trade_date={as_of.isoformat()} rows={snapshot.get('row_count', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
