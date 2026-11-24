-- Canonical v1 core staffing, onboarding, private Desk anchor, and company assessment tables.

CREATE TABLE staffing_authority (
    grant_id TEXT PRIMARY KEY NOT NULL REFERENCES standing_grant(id),
    execution_profile TEXT NOT NULL,
    allowed_scope TEXT NOT NULL,
    slots INTEGER NOT NULL CHECK (slots > 0),
    turns INTEGER NOT NULL CHECK (turns > 0),
    manager_scope TEXT NOT NULL
) STRICT;

CREATE TABLE staffing_assignment (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    manager_grant_id TEXT NOT NULL REFERENCES staffing_authority(grant_id),
    evidence TEXT NOT NULL,
    evidence_revision TEXT NOT NULL,
    available_at INTEGER NOT NULL
) STRICT;

CREATE TABLE staffing_read (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    snapshot TEXT NOT NULL,
    revision TEXT NOT NULL,
    read_at INTEGER NOT NULL
) STRICT;

CREATE TABLE staffing_choice (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES staffing_assignment(duty_id),
    run_id TEXT NOT NULL REFERENCES staffing_read(run_id),
    choice_id TEXT NOT NULL UNIQUE,
    terms TEXT NOT NULL,
    decided_at INTEGER NOT NULL
) STRICT;

CREATE TABLE hire_receipt (
    request_id TEXT PRIMARY KEY NOT NULL,
    choice_id TEXT NOT NULL UNIQUE REFERENCES staffing_choice(choice_id),
    manager_grant_id TEXT NOT NULL REFERENCES staffing_authority(grant_id),
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
    grant_id TEXT NOT NULL UNIQUE REFERENCES standing_grant(id),
    allocated_turns INTEGER NOT NULL CHECK (allocated_turns > 0),
    terms TEXT NOT NULL,
    hired_at INTEGER NOT NULL
) STRICT;

CREATE TABLE staffing_follow_up (
    previous_duty_id TEXT PRIMARY KEY NOT NULL REFERENCES staffing_assignment(duty_id),
    duty_id TEXT NOT NULL UNIQUE REFERENCES staffing_assignment(duty_id)
) STRICT;

CREATE TABLE hire_admission_run (
    request_id TEXT PRIMARY KEY NOT NULL REFERENCES hire_receipt(request_id),
    run_id TEXT NOT NULL REFERENCES duty_run(run_id)
) STRICT;

CREATE TABLE hire_onboarding (
    agent_id TEXT PRIMARY KEY NOT NULL REFERENCES hire_receipt(agent_id),
    purpose TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('provisioning', 'blocked', 'ready', 'accepted')),
    attempts INTEGER NOT NULL,
    retry_at INTEGER,
    reason TEXT,
    first_duty_id TEXT UNIQUE REFERENCES duty(id)
) STRICT;

CREATE TABLE private_desk (
    agent_id TEXT PRIMARY KEY NOT NULL REFERENCES agent(id),
    manifest_revision TEXT NOT NULL
) STRICT;

CREATE TABLE onboarding_read (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    revision TEXT NOT NULL
) STRICT;

CREATE TABLE onboarding_acceptance (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES hire_onboarding(first_duty_id),
    run_id TEXT NOT NULL REFERENCES onboarding_read(run_id),
    revision TEXT NOT NULL,
    rationale TEXT NOT NULL
) STRICT;

CREATE TABLE assessment_source (
    company_id TEXT PRIMARY KEY NOT NULL REFERENCES company(id),
    revision TEXT NOT NULL,
    records TEXT NOT NULL
) STRICT;

CREATE TABLE assessment_read (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    revision TEXT NOT NULL,
    contents TEXT NOT NULL,
    read_at INTEGER NOT NULL
) STRICT;

CREATE TABLE assessment_publication (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    run_id TEXT NOT NULL REFERENCES assessment_read(run_id),
    action_id TEXT NOT NULL UNIQUE,
    revision TEXT NOT NULL,
    contents TEXT NOT NULL,
    author_id TEXT NOT NULL REFERENCES agent(id),
    audience_id TEXT NOT NULL REFERENCES agent(id),
    published_at INTEGER NOT NULL,
    review_at INTEGER NOT NULL
) STRICT;
