-- Stock Agent 工具结果的 24 小时短期证据句柄。

CREATE TABLE IF NOT EXISTS finance.agent_tool_evidence (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL
        REFERENCES finance.agent_turns(request_id) ON DELETE CASCADE,
    session_id uuid NOT NULL
        REFERENCES finance.agent_sessions(id) ON DELETE CASCADE,
    customer_id varchar(64) NOT NULL
        REFERENCES finance.users(customer_id) ON DELETE CASCADE,
    tool_call_id varchar(128) NOT NULL DEFAULT '',
    tool_name varchar(128) NOT NULL,
    input_hash varchar(64) NOT NULL,
    evidence_type varchar(64) NOT NULL,
    sanitized_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source varchar(64) NOT NULL DEFAULT '',
    as_of varchar(64) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_evidence_request
    ON finance.agent_tool_evidence (request_id, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_tool_evidence_expires
    ON finance.agent_tool_evidence (expires_at);
