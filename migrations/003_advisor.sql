-- 投顾会话、轮次与风险画像。
--
-- 幂等：表和索引使用 IF NOT EXISTS。不删除业务表。
-- LangGraph 的基础设施表不在本脚本中创建，也不纳入业务 ORM。

CREATE TABLE IF NOT EXISTS finance.agent_sessions (
    id uuid PRIMARY KEY,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    title varchar(256) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_customer
    ON finance.agent_sessions (customer_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS finance.agent_messages (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL
        REFERENCES finance.agent_sessions(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    role varchar(32) NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL DEFAULT '',
    public_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_messages_request_role
    ON finance.agent_messages (request_id, role);

CREATE INDEX IF NOT EXISTS idx_agent_messages_session
    ON finance.agent_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS finance.agent_turns (
    request_id uuid PRIMARY KEY,
    session_id uuid NOT NULL
        REFERENCES finance.agent_sessions(id) ON DELETE CASCADE,
    status varchar(16) NOT NULL
        CHECK (status IN (
            'pending', 'running', 'interrupted', 'completed', 'failed', 'cancelled'
        )),
    stage varchar(64) NOT NULL DEFAULT '',
    interrupt_payload jsonb,
    final_answer text NOT NULL DEFAULT '',
    error_code varchar(64) NOT NULL DEFAULT '',
    retryable boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE finance.agent_turns
    ADD COLUMN IF NOT EXISTS stage varchar(64) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_agent_turns_session
    ON finance.agent_turns (session_id, updated_at DESC);

-- 每个会话最多一个活动轮次（等待、运行或等待画像确认）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_turns_one_active
    ON finance.agent_turns (session_id)
    WHERE status IN ('pending', 'running', 'interrupted');

CREATE TABLE IF NOT EXISTS finance.advisor_profiles (
    customer_id varchar(64) PRIMARY KEY
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    risk_level varchar(8)
        CHECK (risk_level IS NULL OR risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')),
    investment_horizon varchar(16)
        CHECK (investment_horizon IS NULL OR investment_horizon IN ('短期', '中期', '长期')),
    investment_goal varchar(16)
        CHECK (investment_goal IS NULL OR investment_goal IN ('保本', '收益', '均衡增长', '成长')),
    liquidity_need varchar(8)
        CHECK (liquidity_need IS NULL OR liquidity_need IN ('高', '中', '低')),
    budget_amount numeric(18, 2)
        CHECK (budget_amount IS NULL OR budget_amount > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
