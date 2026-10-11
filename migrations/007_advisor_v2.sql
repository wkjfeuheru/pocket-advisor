-- V2 Supervisor 的持久会话上下文、轮次隔离与任务归属。
ALTER TABLE finance.agent_sessions
    ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE finance.agent_turns
    ADD COLUMN IF NOT EXISTS graph_version varchar(32) NOT NULL DEFAULT 'legacy';
ALTER TABLE finance.agent_tool_evidence
    ADD COLUMN IF NOT EXISTS task_id varchar(128) NOT NULL DEFAULT '';
ALTER TABLE finance.allocation_plans
    ADD COLUMN IF NOT EXISTS task_id varchar(128) NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_agent_tool_evidence_task
    ON finance.agent_tool_evidence (request_id, task_id);
CREATE INDEX IF NOT EXISTS idx_allocation_plans_generation_task
    ON finance.allocation_plans (request_id, session_id, customer_id, task_id);

-- 完成标记与完整公开结果和方案批次在同一事务提交，不能把部分方案当作完成。
CREATE TABLE IF NOT EXISTS finance.allocation_generations (
    customer_id varchar(64) NOT NULL REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    session_id uuid NOT NULL,
    task_id varchar(128) NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, request_id, session_id, task_id)
);
