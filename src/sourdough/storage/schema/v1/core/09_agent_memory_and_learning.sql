-- Canonical v1 core tool call receipt and episodic memory tables (100% STRICT;
-- derived FTS5 episode search indexes live in a physically separate DB).

CREATE TABLE tool_call_receipt (
    company_id TEXT NOT NULL REFERENCES company(id),
    run_id TEXT NOT NULL REFERENCES work_runs(run_id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arg_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'refused', 'error', 'fatal')),
    result_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT,
    offloaded_log_path TEXT,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    PRIMARY KEY (company_id, run_id, call_id)
) STRICT;

CREATE INDEX idx_tool_call_receipt_agent_time
    ON tool_call_receipt(company_id, agent_id, recorded_at_ns);

CREATE TABLE agent_episode (
    episode_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES work_runs(run_id),
    duty_kind TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    active_snapshot_id TEXT,
    outcome_status TEXT NOT NULL CHECK (
        outcome_status IN ('completed', 'waiting', 'blocked', 'failed')
    ),
    summary_text TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    receipts_json TEXT NOT NULL DEFAULT '[]',
    tool_calls_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls_count >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (cost_minor_units >= 0),
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0)
) STRICT;

CREATE INDEX idx_agent_episode_agent_time
    ON agent_episode(company_id, agent_id, recorded_at_ns DESC);
