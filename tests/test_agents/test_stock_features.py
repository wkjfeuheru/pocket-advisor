"""股票特征快照：原子发布、三日容忍、过期拒绝。"""

from __future__ import annotations

import os
from datetime import date

import pytest

from app.crud.stock_feature_crud import MemoryStockFeatureStore, SnapshotStaleError
from app.db.schema import SCHEMA_APPLY_ORDER, _load_sql


def test_migration_defines_snapshot_tables_without_checkpoint():
    assert SCHEMA_APPLY_ORDER.index("004_stock_features.sql") < SCHEMA_APPLY_ORDER.index("005_agent_evidence.sql")
    sql = _load_sql("004_stock_features.sql")
    assert "finance.stock_feature_snapshots" in sql
    assert "finance.stock_features" in sql
    assert "finance.checkpoint" not in sql
    assert "building" in sql and "ready" in sql and "failed" in sql


def test_ready_snapshot_is_atomic_and_failed_is_ignored():
    store = MemoryStockFeatureStore()
    building = store.begin_snapshot(trade_date=date(2026, 9, 26), source_as_of="2026-09-26T15:00:00+08:00")
    store.add_features(building["id"], [{"thscode": "600519.SH", "name": "贵州茅台", "factors": {"growth": 1}}])
    store.fail_snapshot(building["id"], "upstream")
    assert store.latest_ready(as_of=date(2026, 9, 26)) is None
    ready = store.begin_snapshot(trade_date=date(2026, 9, 26), source_as_of="2026-09-26T15:05:00+08:00")
    store.add_features(ready["id"], [{"thscode": "600519.SH", "name": "贵州茅台", "factors": {"growth": 2}}])
    published = store.publish_snapshot(ready["id"])
    assert published["status"] == "ready"
    latest = store.latest_ready(as_of=date(2026, 9, 26))
    assert latest["id"] == ready["id"]
    assert store.features(latest["id"])[0]["factors"]["growth"] == 2


def test_repeat_refresh_on_same_day_is_idempotent():
    store = MemoryStockFeatureStore()
    first = store.refresh(date(2026, 9, 26), rows=[{"thscode": "AAA.SH", "name": "A", "factors": {}}])
    second = store.refresh(date(2026, 9, 26), rows=[{"thscode": "AAA.SH", "name": "A", "factors": {"x": 1}}])
    assert second["status"] == "ready"
    latest = store.latest_ready(as_of=date(2026, 9, 26))
    assert latest["status"] == "ready"
    assert latest["id"] == second["id"]
    assert first["id"] != second["id"]
    assert len(store.ready_snapshots()) <= 30


def test_snapshot_stale_window():
    store = MemoryStockFeatureStore()
    store.refresh(date(2026, 9, 24), rows=[{"thscode": "AAA.SH", "name": "A", "factors": {}}])
    warning = store.latest_ready(as_of=date(2026, 9, 27))
    assert warning["stale_warning"] == "stock_snapshot_stale_warning"
    try:
        store.latest_ready(as_of=date(2026, 9, 28), required=True)
        raise AssertionError("expected stale error")
    except SnapshotStaleError as exc:
        assert exc.code == "stock_snapshot_stale"
        assert exc.retryable is True
    assert store.latest_ready(as_of=date(2026, 9, 28), required=False) is None


def test_refresh_cli_writes_ready_snapshot_without_web_init():
    import inspect

    from app.agents import runtime
    from app.crud.stock_feature_crud import MemoryStockFeatureStore
    from scripts.refresh_stock_features import main

    store = MemoryStockFeatureStore()

    class FakeSource:
        def fetch_rows(self, as_of):
            return [{"thscode": "AAA.SH", "name": "A", "factors": {"growth": 1}}]

    code = main(["--as-of", "2026-09-26"], store=store, source=FakeSource())
    assert code == 0
    latest = store.latest_ready(as_of=date(2026, 9, 26))
    assert latest is not None
    assert latest["status"] == "ready"
    assert store.features(latest["id"])[0]["thscode"] == "AAA.SH"
    assert "refresh_stock_features" not in inspect.getsource(runtime.init_agents)


def test_empty_refresh_does_not_publish_ready_snapshot():
    from scripts.refresh_stock_features import EmptyFeatureSource, main

    store = MemoryStockFeatureStore()
    code = main(["--as-of", "2026-09-26"], store=store, source=EmptyFeatureSource())
    assert code != 0
    assert store.latest_ready(as_of=date(2026, 9, 26)) is None
    assert all(item["status"] != "ready" for item in store._snapshots.values())


def test_runtime_and_cli_use_postgres_feature_store():
    import inspect

    from app.agents import runtime
    from app.crud.stock_feature_crud import PostgresStockFeatureStore, get_stock_feature_store
    from scripts import refresh_stock_features

    assert isinstance(get_stock_feature_store(), PostgresStockFeatureStore)
    assert "MemoryStockFeatureStore()" not in inspect.getsource(runtime.init_agents)
    assert "get_stock_feature_store" in inspect.getsource(runtime.init_agents)
    assert "get_stock_feature_store" in inspect.getsource(refresh_stock_features.main)


def test_postgres_publish_prunes_inside_one_transaction(monkeypatch):
    from app.crud import stock_feature_crud
    from app.crud.stock_feature_crud import PostgresStockFeatureStore

    shared: list[object] = []
    opens = {"count": 0}

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class FakeSession:
        def add(self, obj):
            if obj not in shared:
                shared.append(obj)

        def flush(self):
            return None

        def delete(self, obj):
            if obj in shared:
                shared.remove(obj)

        def get(self, model, key):
            for item in shared:
                if isinstance(item, model) and str(item.id) == str(key):
                    return item
            return None

        def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            return _Result([item for item in shared if isinstance(item, entity)])

    class Scope:
        def __enter__(self):
            opens["count"] += 1
            return FakeSession()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(stock_feature_crud, "session_scope", lambda: Scope())
    monkeypatch.setattr(stock_feature_crud, "ensure_schema", lambda: None)
    store = PostgresStockFeatureStore()
    before = opens["count"]
    for offset in range(31):
        day = date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + offset)
        published = store.refresh(
            day,
            rows=[{
                "thscode": f"{offset:06d}.SH",
                "name": "A",
                "source": "fuyao",
                "as_of": f"{day.isoformat()}T15:00:00+08:00",
                "factors": {"growth": 1},
            }],
        )
        assert published["status"] == "ready"
    assert opens["count"] - before == 31
    ready = [item for item in shared if getattr(item, "status", "") == "ready"]
    assert len(ready) == 30
    latest = store.latest_ready(as_of=date(2026, 1, 31))
    assert latest["status"] == "ready"
    assert store.universe_rows(latest["id"])[0]["thscode"]


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="需要 RUN_POSTGRES_INTEGRATION=1")
def test_second_store_reads_snapshot_published_by_the_first():
    from app.crud.stock_feature_crud import get_stock_feature_store

    writer = get_stock_feature_store()
    published = writer.refresh(
        date(2026, 9, 26),
        rows=[{
            "thscode": "600519.SH",
            "name": "贵州茅台",
            "source": "fuyao",
            "as_of": "2026-09-26T15:00:00+08:00",
            "factors": {"growth": 1},
        }],
    )
    reader = get_stock_feature_store()
    latest = reader.latest_ready(as_of=date(2026, 9, 26))
    assert latest["id"] == published["id"]
    assert reader.universe_rows(latest["id"])[0]["thscode"] == "600519.SH"


def test_feature_row_normalization_keeps_non_column_fields_in_factors():
    from app.crud.stock_feature_crud import normalize_feature_row

    packed = normalize_feature_row({
        "thscode": "600519.SH",
        "name": "贵州茅台",
        "listed_days": "400",
        "is_st": None,
        "growth": 0.3,
        "source": "fuyao",
        "as_of": "2026-09-26T15:00:00+08:00",
        "filter_flags": {"one_word_limit": True},
        "factors": {"valuation": 0.4},
    })
    assert packed["thscode"] == "600519.SH"
    assert packed["listed_days"] == 400
    assert packed["is_st"] is False
    assert packed["filter_flags"] == {"one_word_limit": True}
    assert packed["factors"]["valuation"] == 0.4
    assert packed["factors"]["growth"] == 0.3
    assert packed["factors"]["source"] == "fuyao"
    assert "growth" not in {key: value for key, value in packed.items() if key != "factors"}


def test_memory_store_round_trips_non_column_factor_fields():
    store = MemoryStockFeatureStore()
    published = store.refresh(
        date(2026, 9, 26),
        rows=[{
            "thscode": "600519.SH", "name": "贵州茅台", "industry": "白酒",
            "listed_days": 4000, "net_assets": 2.0e11, "avg_turnover_20": 3.0e9,
            "earnings_quality": 0.9, "source": "fuyao",
            "as_of": "2026-09-26T15:00:00+08:00", "factors": {"growth": 0.3},
        }],
        source_as_of="2026-09-26T15:00:00+08:00",
    )
    row = store.universe_rows(published["id"])[0]
    assert row["earnings_quality"] == 0.9
    assert row["growth"] == 0.3
    assert row["net_assets"] == 2.0e11
    assert row["source"] == "fuyao"
    assert row["industry"] == "白酒"


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="需要 RUN_POSTGRES_INTEGRATION=1")
def test_memory_and_postgres_stores_agree_on_universe_rows():
    from app.crud.stock_feature_crud import get_stock_feature_store

    rows = [{
        "thscode": "600519.SH", "name": "贵州茅台", "industry": "白酒",
        "listed_days": 4000, "net_assets": 2.0e11, "avg_turnover_20": 3.0e9,
        "earnings_quality": 0.9, "cashflow_quality": 0.85, "is_st": False,
        "suspended": False, "source": "fuyao",
        "as_of": "2026-09-26T15:00:00+08:00", "factors": {"growth": 0.3},
    }]
    keys = ("thscode", "industry", "listed_days", "is_st", "suspended",
            "earnings_quality", "cashflow_quality", "growth", "net_assets", "source")

    memory_store = MemoryStockFeatureStore()
    memory_published = memory_store.refresh(
        date(2026, 9, 26), rows=rows, source_as_of="2026-09-26T15:00:00+08:00",
    )
    postgres_store = get_stock_feature_store()
    postgres_published = postgres_store.refresh(
        date(2026, 9, 26), rows=rows, source_as_of="2026-09-26T15:00:00+08:00",
    )
    left = memory_store.universe_rows(memory_published["id"])[0]
    right = postgres_store.universe_rows(postgres_published["id"])[0]
    assert {key: left.get(key) for key in keys} == {key: right.get(key) for key in keys}


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="需要 RUN_POSTGRES_INTEGRATION=1")
def test_postgres_publish_keeps_non_column_factor_fields():
    from app.crud.stock_feature_crud import get_stock_feature_store

    store = get_stock_feature_store()
    published = store.refresh(
        date(2026, 9, 26),
        rows=[{
            "thscode": "600519.SH", "name": "贵州茅台", "industry": "白酒",
            "listed_days": 4000, "avg_turnover_20": 3.0e9, "earnings_quality": 0.9,
            "source": "fuyao", "as_of": "2026-09-26T15:00:00+08:00",
        }],
        source_as_of="2026-09-26T15:00:00+08:00",
    )
    row = store.universe_rows(published["id"])[0]
    assert row["earnings_quality"] == 0.9
    assert row["industry"] == "白酒"
    assert row["source"] == "fuyao"
