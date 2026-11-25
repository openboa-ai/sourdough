-- Canonical v1 opt-in paper domain schema pack.
-- Installed atomically via enable_domain_v1(conn, 'paper') on paper binding,
-- preserving zero paper_* tables at company-only founding.

CREATE TABLE paper_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision TEXT NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    charter_revision TEXT NOT NULL,
    exposure_limit_minor_units INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    connector_revision TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL,
    currency TEXT NOT NULL,
    starting_minor_units INTEGER NOT NULL,
    environment TEXT NOT NULL CHECK (environment = 'paper'),
    destination_id TEXT NOT NULL
) STRICT;

CREATE TABLE paper_binding_change (
    revision TEXT PRIMARY KEY NOT NULL,
    previous_revision TEXT NOT NULL,
    old_terms TEXT NOT NULL,
    new_terms TEXT NOT NULL,
    requested_at_ns INTEGER NOT NULL,
    effective_at_ns INTEGER
) STRICT;

CREATE TABLE paper_order (
    order_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES duty_run(run_id),
    mandate_revision TEXT NOT NULL,
    account_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_minor_units INTEGER NOT NULL,
    reserved_minor_units INTEGER NOT NULL,
    UNIQUE (run_id, action_id)
) STRICT;

CREATE TABLE paper_fill (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES paper_order(order_id),
    quantity INTEGER NOT NULL,
    price_minor_units INTEGER NOT NULL
) STRICT;

CREATE INDEX paper_fill_by_order
    ON paper_fill(order_id);

CREATE TABLE paper_fill_fee (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill(fill_id),
    fee_minor_units INTEGER NOT NULL
) STRICT;

CREATE TABLE paper_cancellation (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_order(order_id),
    event_id TEXT NOT NULL UNIQUE,
    filled_quantity INTEGER NOT NULL,
    released INTEGER NOT NULL CHECK (released IN (0, 1)),
    position_quantity INTEGER,
    available_at INTEGER,
    outcome TEXT NOT NULL DEFAULT 'cancelled'
) STRICT;

CREATE TABLE paper_position_observation (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill(fill_id),
    position_quantity INTEGER NOT NULL,
    available_at INTEGER NOT NULL
) STRICT;

CREATE TABLE paper_reconciliation (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_order(order_id),
    attempts INTEGER NOT NULL,
    next_query_at INTEGER,
    fill_id TEXT,
    sources TEXT NOT NULL
) STRICT;

CREATE TABLE paper_decision (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES work_follow_up(duty_id),
    run_id TEXT NOT NULL REFERENCES duty_run(run_id),
    decision_id TEXT NOT NULL UNIQUE,
    decided_at INTEGER NOT NULL,
    call_id TEXT NOT NULL,
    result_revision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    next_check_seconds INTEGER NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER,
    limit_minor_units INTEGER,
    account_id TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL
) STRICT;

CREATE TABLE paper_review_quote (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    account_id TEXT,
    connector_revision TEXT,
    quote_minor_units INTEGER,
    observed_at INTEGER NOT NULL
) STRICT;

CREATE TABLE paper_research_plan (
    company_id TEXT NOT NULL REFERENCES company(id),
    account_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    inputs TEXT NOT NULL,
    PRIMARY KEY (company_id, account_id)
) STRICT;

CREATE TABLE paper_research_snapshot (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    revision TEXT NOT NULL,
    inputs TEXT NOT NULL
) STRICT;

CREATE TABLE paper_research_finding (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES duty_run(run_id),
    finding_id TEXT NOT NULL UNIQUE,
    revision TEXT NOT NULL,
    published_at INTEGER NOT NULL,
    content TEXT NOT NULL
) STRICT;

CREATE TABLE paper_research_decision (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    run_id TEXT NOT NULL REFERENCES duty_run(run_id),
    finding_id TEXT NOT NULL REFERENCES paper_research_finding(finding_id),
    decision_id TEXT NOT NULL UNIQUE,
    decided_at INTEGER NOT NULL,
    content TEXT NOT NULL
) STRICT;

CREATE TABLE hired_research_order_request (
    duty_id TEXT PRIMARY KEY NOT NULL REFERENCES duty(id),
    run_id TEXT NOT NULL REFERENCES duty_run(run_id),
    decision_id TEXT NOT NULL UNIQUE REFERENCES paper_research_decision(decision_id)
) STRICT;

CREATE TABLE paper_risk_state (
    company_id TEXT PRIMARY KEY NOT NULL REFERENCES company(id),
    suspended INTEGER NOT NULL CHECK (suspended IN (0, 1)),
    effective_revision INTEGER NOT NULL CHECK (effective_revision >= 0),
    last_command_id TEXT,
    updated_at_ns INTEGER NOT NULL,
    FOREIGN KEY (company_id, last_command_id)
        REFERENCES command_receipt(company_id, command_id)
) STRICT;
