-- Canonical v1 core company identity, agent roster, standing grants, and base duty tables.

CREATE TABLE company (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    id TEXT NOT NULL UNIQUE,
    schema_revision INTEGER NOT NULL CHECK (schema_revision IN (1, -1)),
    charter_revision TEXT NOT NULL
) STRICT;

CREATE TABLE agent (
    id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    execution_profile TEXT NOT NULL
) STRICT;

CREATE TABLE standing_grant (
    id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
    scope TEXT NOT NULL
) STRICT;

CREATE TABLE duty (
    id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agent(id),
    state TEXT NOT NULL
) STRICT;

CREATE TABLE founding_receipt (
    setup_identity TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL UNIQUE REFERENCES company(id),
    founder_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
    grant_id TEXT NOT NULL UNIQUE REFERENCES standing_grant(id),
    due_work_id TEXT NOT NULL UNIQUE REFERENCES duty(id)
) STRICT;

CREATE TABLE work_follow_up (
    previous_duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    duty_id TEXT NOT NULL UNIQUE REFERENCES duty(id),
    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    grant_scope TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    result_kind TEXT NOT NULL,
    result_id TEXT NOT NULL,
    result_revision TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'position_review'
) STRICT;

CREATE TABLE duty_run (
    run_id TEXT PRIMARY KEY NOT NULL,
    duty_id TEXT NOT NULL REFERENCES duty(id),
    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    grant_scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1))
) STRICT;

CREATE UNIQUE INDEX current_duty_run
    ON duty_run(duty_id) WHERE is_current = 1;

CREATE TABLE work_result_read (
    duty_id TEXT NOT NULL REFERENCES work_follow_up(duty_id),
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    result_revision TEXT NOT NULL,
    read_at INTEGER NOT NULL
) STRICT;

CREATE TABLE work_review_decision (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES work_follow_up(duty_id),
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL UNIQUE,
    result_revision TEXT NOT NULL,
    action TEXT NOT NULL,
    rationale TEXT NOT NULL,
    next_check_seconds INTEGER NOT NULL,
    decided_at INTEGER NOT NULL
) STRICT;
