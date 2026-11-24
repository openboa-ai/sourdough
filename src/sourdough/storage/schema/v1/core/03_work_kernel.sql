-- Canonical v1 three-entity Work kernel: work_items, fenced work_runs with
-- single active execution per agent/item and zero-compute waits, standing
-- responsibilities, versioned conditions, and explicit blocking peer requests.

CREATE TABLE work_items (
    work_item_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    owner_agent_id TEXT NOT NULL REFERENCES agent(id),
    duty_kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('due', 'scheduled', 'running', 'waiting', 'settled', 'cancelled', 'blocked')
    ),
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    parent_work_item_id TEXT REFERENCES work_items(work_item_id),
    dedupe_key TEXT UNIQUE,
    payload_json TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_work_items_company_state
    ON work_items(company_id, state, created_at_ns);

CREATE TABLE work_runs (
    run_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    duty_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled', 'fenced')
    ),
    fencing_token INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_expires_at_ns INTEGER,
    wait_until_ns INTEGER,
    wait_event_key TEXT,
    budget_ceiling_minor INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT UNIQUE,
    outcome_json TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE UNIQUE INDEX ux_work_runs_active_agent
    ON work_runs(agent_id) WHERE status = 'running';

CREATE UNIQUE INDEX ux_work_runs_active_item
    ON work_runs(work_item_id) WHERE status = 'running';

CREATE INDEX idx_work_runs_waiting
    ON work_runs(company_id, status, wait_until_ns, wait_event_key);

CREATE TABLE work_responsibilities (
    responsibility_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    owner_agent_id TEXT NOT NULL REFERENCES agent(id),
    duty_kind TEXT NOT NULL,
    remit TEXT NOT NULL,
    readable_scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'revoked')),
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE TABLE work_conditions (
    condition_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    owner_agent_id TEXT NOT NULL REFERENCES agent(id),
    responsibility_id TEXT REFERENCES work_responsibilities(responsibility_id),
    work_item_id TEXT REFERENCES work_items(work_item_id),
    condition_revision INTEGER NOT NULL DEFAULT 1,
    trigger_kind TEXT NOT NULL CHECK (
        trigger_kind IN ('timer', 'shared_asset_revision', 'peer_response')
    ),
    due_at_ns INTEGER,
    source_identity TEXT,
    expected_revision TEXT,
    expires_at_ns INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('armed', 'triggered', 'superseded', 'cancelled', 'expired')
    ),
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_work_conditions_armed
    ON work_conditions(company_id, status, trigger_kind, due_at_ns);

CREATE TABLE work_condition_occurrences (
    company_id TEXT NOT NULL REFERENCES company(id),
    source_identity TEXT NOT NULL,
    condition_revision INTEGER NOT NULL,
    condition_id TEXT NOT NULL REFERENCES work_conditions(condition_id),
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    admitted_at_ns INTEGER NOT NULL,
    PRIMARY KEY (company_id, source_identity, condition_revision)
) STRICT;

CREATE TABLE work_peer_requests (
    request_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    requester_work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    requester_run_id TEXT NOT NULL REFERENCES work_runs(run_id),
    requester_agent_id TEXT NOT NULL REFERENCES agent(id),
    recipient_agent_id TEXT NOT NULL REFERENCES agent(id),
    response_work_item_id TEXT NOT NULL UNIQUE REFERENCES work_items(work_item_id),
    request_key TEXT,
    project_id TEXT,
    question TEXT NOT NULL,
    expected_decision TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    deadline_ns INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'answered', 'declined', 'expired')),
    read_run_id TEXT REFERENCES work_runs(run_id),
    answered_run_id TEXT REFERENCES work_runs(run_id),
    resumed_run_id TEXT UNIQUE REFERENCES work_runs(run_id),
    response_json TEXT,
    resolved_at_ns INTEGER,
    created_at_ns INTEGER NOT NULL
) STRICT;

CREATE UNIQUE INDEX ux_work_peer_requests_open_duty
    ON work_peer_requests(requester_work_item_id) WHERE state = 'open';

CREATE UNIQUE INDEX ux_work_peer_requests_duty_key
    ON work_peer_requests(requester_work_item_id, request_key)
    WHERE request_key IS NOT NULL;
