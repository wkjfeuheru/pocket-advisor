"""股票特征快照仓库。查询永不返回 failed；过期超过三日拒绝选股。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db.models.stock_feature import StockFeature, StockFeatureSnapshot
from app.db.session import ensure_schema, session_scope

STALE_DAYS = 3
KEEP_READY = 30
SCHEMA_VERSION = "v1"
RULES_VERSION = "v1"


class SnapshotStaleError(RuntimeError):
    """选股所需快照过期。"""

    def __init__(self, code: str = "stock_snapshot_stale", *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _copy(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


# 与 SQL 列一一对应的字段。其余顶层字段一律进 ``factors``，避免静默丢数据。
_COLUMNS = (
    "thscode",
    "name",
    "exchange",
    "industry",
    "trade_status",
    "listed_days",
    "avg_turnover_20",
    "is_st",
    "suspended",
)


def normalize_feature_row(row: dict[str, Any]) -> dict[str, Any]:
    """把一行因子收成与 SQL 契约一致的形状。

    ``universe_rows`` 会把 ``factors`` 与 ``filter_flags`` 再摊平回顶层，
    因此内存仓库与 PostgreSQL 仓库对同一输入产生同样的行。
    """
    payload = dict(row or {})
    factors = dict(payload.pop("factors", None) or {})
    flags = dict(payload.pop("filter_flags", None) or {})
    columns = {key: payload.pop(key) for key in _COLUMNS if key in payload}
    for key, value in payload.items():
        factors.setdefault(key, value)
    return {
        "thscode": str(columns.get("thscode") or ""),
        "name": str(columns.get("name") or ""),
        "exchange": str(columns.get("exchange") or ""),
        "industry": str(columns.get("industry") or ""),
        "trade_status": str(columns.get("trade_status") or ""),
        "listed_days": int(columns.get("listed_days") or 0),
        "avg_turnover_20": columns.get("avg_turnover_20"),
        "is_st": bool(columns.get("is_st")),
        "suspended": bool(columns.get("suspended")),
        "filter_flags": flags,
        "factors": factors,
    }


class MemoryStockFeatureStore:
    """测试与 CLI 注入用的内存仓库。行为与 SQL 契约一致。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._features: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0

    def begin_snapshot(
        self,
        trade_date: date,
        source_as_of: str,
        *,
        schema_version: str = SCHEMA_VERSION,
        rules_version: str = RULES_VERSION,
    ) -> dict[str, Any]:
        snapshot_id = str(uuid4())
        now = datetime.now(timezone.utc)
        self._seq += 1
        row = {
            "id": snapshot_id,
            "trade_date": trade_date,
            "schema_version": schema_version,
            "rules_version": rules_version,
            "source_as_of": source_as_of,
            "status": "building",
            "row_count": 0,
            "error_code": "",
            "created_at": now,
            "updated_at": now,
            "seq": self._seq,
        }
        self._snapshots[snapshot_id] = row
        self._features[snapshot_id] = []
        return _copy(row)

    def add_features(self, snapshot_id: str, rows: list[dict[str, Any]]) -> None:
        bucket = self._features.setdefault(snapshot_id, [])
        for row in rows:
            bucket.append(normalize_feature_row(row))
        snap = self._snapshots[snapshot_id]
        snap["row_count"] = len(bucket)
        snap["updated_at"] = datetime.now(timezone.utc)

    def fail_snapshot(self, snapshot_id: str, error_code: str) -> dict[str, Any]:
        snap = self._snapshots[snapshot_id]
        snap["status"] = "failed"
        snap["error_code"] = error_code
        snap["updated_at"] = datetime.now(timezone.utc)
        return _copy(snap)

    def publish_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        snap = self._snapshots[snapshot_id]
        snap["status"] = "ready"
        snap["error_code"] = ""
        snap["updated_at"] = datetime.now(timezone.utc)
        self._prune_ready()
        return _copy(snap)

    def refresh(
        self,
        trade_date: date,
        *,
        rows: list[dict[str, Any]],
        source_as_of: str | None = None,
    ) -> dict[str, Any]:
        stamp = source_as_of or f"{trade_date.isoformat()}T15:00:00+08:00"
        snap = self.begin_snapshot(trade_date, stamp)
        self.add_features(snap["id"], rows)
        return self.publish_snapshot(snap["id"])

    def latest_ready(self, as_of: date, *, required: bool = False) -> dict[str, Any] | None:
        ready = [item for item in self._snapshots.values() if item.get("status") == "ready"]
        if not ready:
            if required:
                raise SnapshotStaleError()
            return None
        latest = max(
            ready,
            key=lambda item: (item["trade_date"], item.get("seq", 0)),
        )
        delta = (as_of - latest["trade_date"]).days
        if delta > STALE_DAYS:
            if required:
                raise SnapshotStaleError()
            return None
        out = _copy(latest)
        if delta > 0:
            out["stale_warning"] = "stock_snapshot_stale_warning"
        return out

    def features(self, snapshot_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._features.get(snapshot_id, [])]

    def universe_rows(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = []
        for item in self.features(snapshot_id):
            packed = dict(item)
            factors = dict(packed.pop("factors", {}) or {})
            flags = dict(packed.pop("filter_flags", {}) or {})
            packed.update(flags)
            packed.update(factors)
            rows.append(packed)
        return rows

    def ready_snapshots(self) -> list[dict[str, Any]]:
        return [_copy(item) for item in self._snapshots.values() if item.get("status") == "ready"]

    def _prune_ready(self) -> None:
        ready = sorted(
            [item for item in self._snapshots.values() if item.get("status") == "ready"],
            key=lambda item: (item["trade_date"], str(item["id"])),
            reverse=True,
        )
        for extra in ready[KEEP_READY:]:
            snapshot_id = extra["id"]
            self._snapshots.pop(snapshot_id, None)
            self._features.pop(snapshot_id, None)


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _parse_stamp(value: str | datetime | None, trade_date: date) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value or f"{trade_date.isoformat()}T15:00:00+08:00").replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _snapshot_dict(row: StockFeatureSnapshot) -> dict[str, Any]:
    stamp = row.source_as_of
    return {
        "id": str(row.id),
        "trade_date": row.trade_date,
        "schema_version": row.schema_version,
        "rules_version": row.rules_version,
        "source_as_of": stamp.isoformat() if stamp is not None else "",
        "status": row.status,
        "row_count": row.row_count,
        "error_code": row.error_code,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _feature_dict(row: StockFeature) -> dict[str, Any]:
    return {
        "thscode": row.thscode,
        "name": row.name,
        "exchange": row.exchange,
        "industry": row.industry,
        "trade_status": row.trade_status,
        "listed_days": row.listed_days,
        "avg_turnover_20": float(row.avg_turnover_20) if row.avg_turnover_20 is not None else None,
        "is_st": row.is_st,
        "suspended": row.suspended,
        "filter_flags": dict(row.filter_flags or {}),
        "factors": dict(row.factors or {}),
    }


def _new_feature(snapshot_id: UUID, row: dict[str, Any]) -> StockFeature:
    packed = normalize_feature_row(row)
    turnover = packed["avg_turnover_20"]
    return StockFeature(
        id=uuid4(),
        snapshot_id=snapshot_id,
        thscode=packed["thscode"],
        name=packed["name"],
        exchange=packed["exchange"],
        industry=packed["industry"],
        trade_status=packed["trade_status"],
        listed_days=packed["listed_days"],
        avg_turnover_20=float(turnover) if turnover is not None else None,
        is_st=packed["is_st"],
        suspended=packed["suspended"],
        filter_flags=packed["filter_flags"],
        factors=packed["factors"],
    )


class PostgresStockFeatureStore:
    """PostgreSQL 快照仓库。发布与裁剪在同一个 session 事务里完成。"""

    def begin_snapshot(
        self,
        trade_date: date,
        source_as_of: str,
        *,
        schema_version: str = SCHEMA_VERSION,
        rules_version: str = RULES_VERSION,
    ) -> dict[str, Any]:
        ensure_schema()
        now = datetime.now(timezone.utc)
        row = StockFeatureSnapshot(
            id=uuid4(),
            trade_date=trade_date,
            schema_version=schema_version,
            rules_version=rules_version,
            source_as_of=_parse_stamp(source_as_of, trade_date),
            status="building",
            row_count=0,
            error_code="",
            created_at=now,
            updated_at=now,
        )
        with session_scope() as session:
            session.add(row)
            return _snapshot_dict(row)

    def add_features(self, snapshot_id: str, rows: list[dict[str, Any]]) -> None:
        ensure_schema()
        with session_scope() as session:
            snap = session.get(StockFeatureSnapshot, _as_uuid(snapshot_id))
            if snap is None:
                return
            for row in rows:
                session.add(_new_feature(snap.id, row))
            if hasattr(session, "flush"):
                session.flush()
            snap.row_count = len(self._features_for(session, snap.id))
            snap.updated_at = datetime.now(timezone.utc)

    def fail_snapshot(self, snapshot_id: str, error_code: str) -> dict[str, Any]:
        ensure_schema()
        with session_scope() as session:
            snap = session.get(StockFeatureSnapshot, _as_uuid(snapshot_id))
            snap.status = "failed"
            snap.error_code = error_code
            snap.updated_at = datetime.now(timezone.utc)
            return _snapshot_dict(snap)

    def publish_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        ensure_schema()
        with session_scope() as session:
            snap = session.get(StockFeatureSnapshot, _as_uuid(snapshot_id))
            snap.status = "ready"
            snap.error_code = ""
            snap.updated_at = datetime.now(timezone.utc)
            self._prune(session)
            return _snapshot_dict(snap)

    def refresh(
        self,
        trade_date: date,
        *,
        rows: list[dict[str, Any]],
        source_as_of: str | None = None,
    ) -> dict[str, Any]:
        ensure_schema()
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            snap = StockFeatureSnapshot(
                id=uuid4(),
                trade_date=trade_date,
                schema_version=SCHEMA_VERSION,
                rules_version=RULES_VERSION,
                source_as_of=_parse_stamp(source_as_of, trade_date),
                status="ready",
                row_count=len(rows),
                error_code="",
                created_at=now,
                updated_at=now,
            )
            session.add(snap)
            # 快照头必须先落库：feature 行有外键约束，而且 autoflush=False 时
            # 后续 SELECT 看不到未 flush 的新快照，裁剪会算错保留数量。
            session.flush()
            for row in rows:
                session.add(_new_feature(snap.id, row))
            session.flush()
            self._prune(session)
            return _snapshot_dict(snap)

    def latest_ready(self, as_of: date, *, required: bool = False) -> dict[str, Any] | None:
        ensure_schema()
        with session_scope() as session:
            ready = self._ready(session)
            if not ready:
                if required:
                    raise SnapshotStaleError()
                return None
            latest = max(ready, key=lambda item: (item.trade_date, item.created_at, str(item.id)))
            delta = (as_of - latest.trade_date).days
            if delta > STALE_DAYS:
                if required:
                    raise SnapshotStaleError()
                return None
            payload = _snapshot_dict(latest)
            if delta > 0:
                payload["stale_warning"] = "stock_snapshot_stale_warning"
            return payload

    def features(self, snapshot_id: str) -> list[dict[str, Any]]:
        ensure_schema()
        with session_scope() as session:
            return [_feature_dict(row) for row in self._features_for(session, _as_uuid(snapshot_id))]

    def universe_rows(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = []
        for item in self.features(snapshot_id):
            packed = dict(item)
            factors = dict(packed.pop("factors", {}) or {})
            flags = dict(packed.pop("filter_flags", {}) or {})
            packed.update(flags)
            packed.update(factors)
            rows.append(packed)
        return rows

    def ready_snapshots(self) -> list[dict[str, Any]]:
        ensure_schema()
        with session_scope() as session:
            return [_snapshot_dict(row) for row in self._ready(session)]

    def _ready(self, session: Any) -> list[StockFeatureSnapshot]:
        return [row for row in self._all(session, StockFeatureSnapshot) if row.status == "ready"]

    def _features_for(self, session: Any, snapshot_id: UUID) -> list[StockFeature]:
        return [
            row for row in self._all(session, StockFeature)
            if row.snapshot_id == snapshot_id
        ]

    def _all(self, session: Any, model: Any) -> list[Any]:
        return list(session.scalars(select(model)).all())

    def _prune(self, session: Any) -> None:
        ready = sorted(
            self._ready(session),
            key=lambda item: (item.trade_date, item.created_at, str(item.id)),
            reverse=True,
        )
        for extra in ready[KEEP_READY:]:
            for feature in self._features_for(session, extra.id):
                session.delete(feature)
            session.delete(extra)


def get_stock_feature_store() -> PostgresStockFeatureStore:
    """运行时与刷新命令共用的 PostgreSQL 仓库。"""
    return PostgresStockFeatureStore()


__all__ = [
    "KEEP_READY",
    "MemoryStockFeatureStore",
    "PostgresStockFeatureStore",
    "SCHEMA_VERSION",
    "STALE_DAYS",
    "SnapshotStaleError",
    "ensure_schema",
    "get_stock_feature_store",
    "normalize_feature_row",
    "session_scope",
]
