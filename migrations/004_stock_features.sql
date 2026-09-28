-- 股票特征快照。选股只读 ready 快照，不在 Web 进程里刷新。
--
-- 幂等：表和索引使用 IF NOT EXISTS。不删除业务表。
-- 不创建 LangGraph 基础设施表。

CREATE TABLE IF NOT EXISTS finance.stock_feature_snapshots (
    id uuid PRIMARY KEY,
    trade_date date NOT NULL,
    schema_version varchar(32) NOT NULL DEFAULT 'v1',
    rules_version varchar(32) NOT NULL DEFAULT 'v1',
    source_as_of timestamptz,
    status varchar(16) NOT NULL
        CHECK (status IN ('building', 'ready', 'failed')),
    row_count integer NOT NULL DEFAULT 0,
    error_code varchar(64) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stock_feature_snapshots_trade_status
    ON finance.stock_feature_snapshots (trade_date DESC, status);

CREATE TABLE IF NOT EXISTS finance.stock_features (
    id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL
        REFERENCES finance.stock_feature_snapshots(id) ON DELETE CASCADE,
    thscode varchar(32) NOT NULL,
    name varchar(128) NOT NULL DEFAULT '',
    exchange varchar(16) NOT NULL DEFAULT '',
    industry varchar(64) NOT NULL DEFAULT '',
    trade_status varchar(32) NOT NULL DEFAULT '',
    listed_days integer NOT NULL DEFAULT 0,
    avg_turnover_20 numeric(20, 2),
    is_st boolean NOT NULL DEFAULT false,
    suspended boolean NOT NULL DEFAULT false,
    filter_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    factors jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_features_snapshot_thscode
    ON finance.stock_features (snapshot_id, thscode);
