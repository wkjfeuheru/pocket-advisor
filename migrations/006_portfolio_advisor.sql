-- 持仓配置与历史复盘：日频净值、方案、AI 账本与对比快照。
--
-- 三条设计约束，改动前请先读：
--
-- 1. `product_nav_history` 是**唯一价格事实源**。`finance.product_performance` 每个
--    产品只有一行最新快照，无法支撑曲线与回测，只能作为缺失时的回退。
-- 2. `ai_*` 表与用户表完全隔离。AI 账本永不写 `finance.positions` /
--    `finance.orders` / `finance.cash_transactions`。用户持仓是事实，AI 账本是反事实。
-- 3. `ai_trades` 是 append-only 且有两级幂等键（见下），不可改成单键。

-- ── 日频净值历史 ──────────────────────────────────────────────────────
-- PRIMARY KEY (product_code, trade_date) 就是幂等 upsert 的冲突目标。
-- 真数据优先覆盖合成：写入走
--   INSERT ... ON CONFLICT (product_code, trade_date) DO UPDATE
--     ... WHERE excluded.data_source = 'real'
--              OR product_nav_history.data_source = 'synthetic'
-- 于是真数据能压过合成，而合成永远压不过真数据。
CREATE TABLE IF NOT EXISTS finance.product_nav_history (
    product_code varchar(32) NOT NULL
        REFERENCES finance.products(code) ON DELETE CASCADE,
    trade_date date NOT NULL,
    nav double precision NOT NULL,
    data_source varchar(16) NOT NULL DEFAULT 'real'
        CHECK (data_source IN ('real', 'synthetic')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_nav_history_trade_date
    ON finance.product_nav_history (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_nav_history_source
    ON finance.product_nav_history (data_source);

-- ── 方案 ──────────────────────────────────────────────────────────────
-- 刻意**不建** status='adopted' 的唯一索引：方案不可逆，用户会多次采纳
-- （新采纳 = 一次新的调仓）。防重复采纳由 ai_trades.trade_key 与请求侧幂等键负责。
CREATE TABLE IF NOT EXISTS finance.allocation_plans (
    plan_id uuid PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    request_id uuid,
    session_id uuid REFERENCES finance.agent_sessions(id) ON DELETE SET NULL,
    tier varchar(16) NOT NULL CHECK (tier IN ('light', 'moderate', 'rebuild')),
    status varchar(16) NOT NULL DEFAULT 'offered'
        CHECK (status IN ('offered', 'adopted', 'shadow', 'unavailable', 'expired')),
    turnover_rate double precision,
    estimated_fee numeric(18, 2),
    risk_level_before varchar(8),
    risk_level_after varchar(8),
    volatility_before double precision,
    volatility_after double precision,
    concentration_after double precision,
    rationale text NOT NULL DEFAULT '',
    repairs jsonb NOT NULL DEFAULT '[]'::jsonb,
    limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    unavailable_reason varchar(64) NOT NULL DEFAULT '',
    nav_as_of date,
    adopted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_allocation_plans_customer
    ON finance.allocation_plans (customer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_allocation_plans_session
    ON finance.allocation_plans (session_id);

-- ── 方案明细 ──────────────────────────────────────────────────────────
-- 权重一律存 0–1 小数（与 PositionView.weight 的百分数口径不同，换算在一处完成）。
CREATE TABLE IF NOT EXISTS finance.allocation_plan_items (
    id bigserial PRIMARY KEY,
    plan_id uuid NOT NULL
        REFERENCES finance.allocation_plans(plan_id) ON DELETE CASCADE,
    product_code varchar(32) NOT NULL REFERENCES finance.products(code),
    product_name varchar(256) NOT NULL DEFAULT '',
    asset_class varchar(32) NOT NULL DEFAULT '',
    action varchar(16) NOT NULL
        CHECK (action IN ('open', 'add', 'reduce', 'close', 'hold')),
    current_weight double precision,
    target_weight double precision NOT NULL CHECK (target_weight >= 0),
    delta_weight double precision NOT NULL,
    current_amount numeric(18, 2),
    target_amount numeric(18, 2) NOT NULL DEFAULT 0,
    reason text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_allocation_plan_items_plan
    ON finance.allocation_plan_items (plan_id);

-- ── AI 账户 ───────────────────────────────────────────────────────────
-- total_units 是**份额**（初始本金按 1 份 = 1 元换算），不是本金也不是总资产。
-- 把份额写成总资产会让 unit_nav 恒为 1.0，费用与调仓摩擦全部消失。
CREATE TABLE IF NOT EXISTS finance.ai_accounts (
    customer_id varchar(64) PRIMARY KEY
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    initial_capital numeric(18, 2) NOT NULL,
    opening_plan_id uuid REFERENCES finance.allocation_plans(plan_id),
    cash_balance numeric(18, 2) NOT NULL DEFAULT 0,
    unit_nav double precision NOT NULL DEFAULT 1.0,
    total_units double precision NOT NULL DEFAULT 0,
    cumulative_inflow numeric(18, 2) NOT NULL DEFAULT 0,
    cumulative_outflow numeric(18, 2) NOT NULL DEFAULT 0,
    nav_as_of date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ── AI 持仓 ───────────────────────────────────────────────────────────
-- 字段与 finance.positions 刻意同名同型，便于估值逻辑复用与人工对照。
CREATE TABLE IF NOT EXISTS finance.ai_positions (
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    product_code varchar(32) NOT NULL REFERENCES finance.products(code),
    shares double precision NOT NULL,
    cost_amount numeric(18, 2) NOT NULL DEFAULT 0,
    avg_cost double precision NOT NULL DEFAULT 0,
    opened_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, product_code)
);

CREATE INDEX IF NOT EXISTS idx_ai_positions_customer
    ON finance.ai_positions (customer_id);

-- ── AI 成交（append-only） ────────────────────────────────────────────
-- 两级幂等键，缺一不可：
--   adoption_key —— **一次采纳**的键，同一次采纳产生的所有成交共用它，用于判断
--                   "这个请求是否已处理过"（重放）。
--   trade_key    —— adoption_key:product_code，**单笔成交**的键，落唯一索引。
-- 若所有成交共用 adoption_key，第一笔插入成功、第二笔就撞唯一约束并被误判成
-- "重复请求"回滚 —— 那正是"采纳一个多标的方案却只成交了一笔"的成因。
CREATE TABLE IF NOT EXISTS finance.ai_trades (
    trade_id uuid PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    plan_id uuid REFERENCES finance.allocation_plans(plan_id),
    product_code varchar(32) NOT NULL REFERENCES finance.products(code),
    side varchar(16) NOT NULL CHECK (side IN ('buy', 'sell', 'subscribe', 'redeem')),
    shares double precision NOT NULL,
    price double precision NOT NULL,
    gross_amount numeric(18, 2) NOT NULL,
    fee numeric(18, 2) NOT NULL DEFAULT 0,
    net_amount numeric(18, 2) NOT NULL,
    realized_pnl numeric(18, 2),
    nav_as_of date,
    data_source varchar(16) NOT NULL DEFAULT 'real'
        CHECK (data_source IN ('real', 'synthetic')),
    adoption_key varchar(128) NOT NULL DEFAULT '',
    trade_key varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_trades_customer
    ON finance.ai_trades (customer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_trades_plan
    ON finance.ai_trades (plan_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_trades_trade_key
    ON finance.ai_trades (customer_id, trade_key)
    WHERE trade_key <> '';

-- ── 持仓快照（用户线与 AI 线共用） ────────────────────────────────────
-- owner 区分两条线，使"同一张图上对比用户与 AI"只需一次查询。
CREATE TABLE IF NOT EXISTS finance.ai_portfolio_snapshots (
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    owner varchar(8) NOT NULL CHECK (owner IN ('user', 'ai')),
    snapshot_date date NOT NULL,
    total_assets numeric(18, 2) NOT NULL,
    cash_balance numeric(18, 2) NOT NULL DEFAULT 0,
    market_value numeric(18, 2) NOT NULL DEFAULT 0,
    unit_nav double precision NOT NULL,
    -- 首日定下的份额锚，跨天继承不变。没有它，unit_nav 会退化成恒等于 1 的直线。
    baseline_units double precision,
    daily_return double precision,
    cumulative_return double precision,
    max_drawdown double precision,
    annualized_volatility double precision,
    nav_as_of date,
    limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, owner, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_ai_snapshots_owner_date
    ON finance.ai_portfolio_snapshots (owner, snapshot_date DESC);
