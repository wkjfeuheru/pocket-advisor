-- 模拟交易业务库的最终结构。
--
-- 幂等：新库由 CREATE TABLE IF NOT EXISTS 一次建齐；已用旧补丁链建过的库
-- 再用 ADD COLUMN IF NOT EXISTS 补上后来才出现的列。不 DROP SCHEMA，
-- 因此容器每次启动执行本脚本不会清空数据。
--
-- 不建 Agent 会话表。文末只清理已移除的对话/编排遗留表。

CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.users (
    id bigserial PRIMARY KEY,
    customer_id varchar(64) UNIQUE,
    username varchar(64) NOT NULL UNIQUE,
    display_name varchar(128) NOT NULL,
    password_hash text NOT NULL,
    salt text NOT NULL,
    created_at varchar(64) NOT NULL,
    is_admin boolean NOT NULL DEFAULT false
);

ALTER TABLE finance.users
    ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS finance.sessions (
    token text PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    expires_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON finance.sessions (customer_id);

CREATE TABLE IF NOT EXISTS finance.products (
    code varchar(32) PRIMARY KEY,
    name varchar(128) NOT NULL,
    type varchar(16) NOT NULL DEFAULT 'fund',
    establish_date varchar(32) NOT NULL DEFAULT '',
    scale double precision,
    manager varchar(64) NOT NULL DEFAULT '',
    company varchar(128) NOT NULL DEFAULT '',
    management_fee double precision,
    custody_fee double precision,
    subscription_fee double precision,
    redemption_fee varchar(64) NOT NULL DEFAULT '',
    risk_level varchar(32) NOT NULL DEFAULT '',
    recommended_holding_period varchar(32) NOT NULL DEFAULT '',
    investment_target text NOT NULL DEFAULT '',
    investment_strategy text NOT NULL DEFAULT '',
    min_investment numeric(18, 2),
    is_active boolean NOT NULL DEFAULT true
);

ALTER TABLE finance.products
    ADD COLUMN IF NOT EXISTS recommended_holding_period varchar(32) NOT NULL DEFAULT '';

ALTER TABLE finance.products
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;

ALTER TABLE finance.products
    ADD COLUMN IF NOT EXISTS min_investment numeric(18, 2);

CREATE INDEX IF NOT EXISTS idx_products_type_active
    ON finance.products (type, is_active);

CREATE TABLE IF NOT EXISTS finance.product_holdings (
    id bigserial PRIMARY KEY,
    product_code varchar(32) NOT NULL
        REFERENCES finance.products(code) ON DELETE CASCADE,
    stock_name varchar(128) NOT NULL,
    stock_code varchar(16) NOT NULL DEFAULT '',
    weight double precision,
    rank integer,
    report_date varchar(32) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_holdings_product ON finance.product_holdings (product_code, rank);

CREATE TABLE IF NOT EXISTS finance.product_performance (
    id bigserial PRIMARY KEY,
    product_code varchar(32) NOT NULL
        REFERENCES finance.products(code) ON DELETE CASCADE,
    nav double precision,
    return_1m double precision,
    return_3m double precision,
    return_6m double precision,
    return_1y double precision,
    return_3y double precision,
    max_drawdown double precision,
    volatility double precision,
    sharpe_ratio double precision,
    update_date varchar(32) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_performance_product ON finance.product_performance (product_code, id DESC);

-- 账户、流水、成交、持仓。注销用户时由 customer_id 外键级联清理。

CREATE TABLE IF NOT EXISTS finance.accounts (
    customer_id varchar(64) PRIMARY KEY
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    cash_balance numeric(18, 2) NOT NULL DEFAULT 0,
    frozen_balance numeric(18, 2) NOT NULL DEFAULT 0,
    total_deposit numeric(18, 2) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finance.cash_transactions (
    txn_id uuid PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    kind varchar(16) NOT NULL CHECK (kind IN ('deposit', 'buy', 'sell', 'fee')),
    amount numeric(18, 2) NOT NULL,
    balance_after numeric(18, 2) NOT NULL,
    ref_id varchar(64) NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    idempotency_key varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cash_transactions_customer
    ON finance.cash_transactions (customer_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_transactions_idempotency
    ON finance.cash_transactions (customer_id, idempotency_key)
    WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS finance.orders (
    order_id uuid PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    product_code varchar(32) NOT NULL
        REFERENCES finance.products(code),
    side varchar(8) NOT NULL CHECK (side IN ('buy', 'sell')),
    shares double precision NOT NULL,
    price double precision NOT NULL,
    gross_amount numeric(18, 2) NOT NULL,
    fee numeric(18, 2) NOT NULL DEFAULT 0,
    net_amount numeric(18, 2) NOT NULL,
    realized_pnl numeric(18, 2),
    fee_limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    idempotency_key varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer
    ON finance.orders (customer_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_idempotency
    ON finance.orders (customer_id, idempotency_key)
    WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS finance.positions (
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    product_code varchar(32) NOT NULL
        REFERENCES finance.products(code),
    shares double precision NOT NULL,
    cost_amount numeric(18, 2) NOT NULL DEFAULT 0,
    avg_cost double precision NOT NULL DEFAULT 0,
    opened_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, product_code)
);

CREATE INDEX IF NOT EXISTS idx_positions_customer ON finance.positions (customer_id);

-- 已移除的投顾对话 / 编排层遗留表。对新库是空操作。
DROP TABLE IF EXISTS finance.research_results CASCADE;
DROP TABLE IF EXISTS finance.research_feature_snapshots CASCADE;
DROP TABLE IF EXISTS finance.research_runs CASCADE;
DROP TABLE IF EXISTS finance.agent_results CASCADE;
DROP TABLE IF EXISTS finance.agent_runs CASCADE;
DROP TABLE IF EXISTS finance.conversation_messages CASCADE;
DROP TABLE IF EXISTS finance.conversations CASCADE;
DROP TABLE IF EXISTS finance.user_profiles CASCADE;
DROP TABLE IF EXISTS finance.theme_memberships CASCADE;
DROP TABLE IF EXISTS finance.theme_evidence CASCADE;
DROP TABLE IF EXISTS finance.theme_reviews CASCADE;
DROP TABLE IF EXISTS finance.themes CASCADE;
DROP TABLE IF EXISTS finance.faq_documents CASCADE;
DROP TABLE IF EXISTS finance.faq_chunks CASCADE;
DROP TABLE IF EXISTS finance.faq_index_versions CASCADE;
DROP TABLE IF EXISTS finance.async_jobs CASCADE;
DROP TABLE IF EXISTS finance.pending_outcomes CASCADE;
