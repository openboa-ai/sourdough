-- Canonical v1 core Board Client authority bindings, CEO advisory direct line,
-- Board decision questions, competing operational restriction causes, and
-- Agent presentation profiles. Keeps private Desk drafts outside company.sqlite.

CREATE TABLE board_binding (
    principal_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    role_name TEXT NOT NULL CHECK (role_name IN ('board_viewer', 'board_control')),
    scope_ref TEXT NOT NULL,
    binding_revision INTEGER NOT NULL DEFAULT 1 CHECK (binding_revision >= 1),
    bound_at_ns INTEGER NOT NULL CHECK (bound_at_ns > 0),
    revoked_at_ns INTEGER CHECK (revoked_at_ns IS NULL OR revoked_at_ns >= bound_at_ns)
) STRICT;

CREATE INDEX idx_board_binding_company_role
    ON board_binding(company_id, role_name);

CREATE TABLE ceo_message (
    message_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    sender_kind TEXT NOT NULL CHECK (sender_kind IN ('board', 'ceo')),
    sender_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL REFERENCES agent(id),
    body TEXT NOT NULL,
    context_kind TEXT,
    context_id TEXT,
    context_label TEXT,
    command_id TEXT,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('recorded', 'delivered', 'read')),
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0)
) STRICT;

CREATE INDEX idx_ceo_message_company_time
    ON ceo_message(company_id, recorded_at_ns);

CREATE TABLE decision_question (
    question_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'applied', 'declined', 'expired')),
    respond_by_ns INTEGER CHECK (respond_by_ns IS NULL OR respond_by_ns > 0),
    created_by_agent_id TEXT NOT NULL REFERENCES agent(id),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    resolved_at_ns INTEGER CHECK (resolved_at_ns IS NULL OR resolved_at_ns >= created_at_ns),
    resolved_command_id TEXT,
    resolution_choice TEXT,
    resolution_reason TEXT,
    application_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        application_status IN ('pending', 'applied', 'declined', 'not_applicable', 'failed')
    ),
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'delivered', 'acknowledged')
    )
) STRICT;

CREATE INDEX idx_decision_question_company_state
    ON decision_question(company_id, state, created_at_ns);

CREATE TABLE restriction_cause (
    restriction_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    cause_category TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    restriction_revision INTEGER NOT NULL DEFAULT 1 CHECK (restriction_revision >= 1),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    operator_releasable INTEGER NOT NULL CHECK (operator_releasable IN (0, 1)),
    reason TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    cleared_at_ns INTEGER CHECK (cleared_at_ns IS NULL OR cleared_at_ns >= recorded_at_ns),
    last_command_id TEXT
) STRICT;

CREATE INDEX idx_restriction_cause_company_active
    ON restriction_cause(company_id, active, recorded_at_ns);

CREATE TABLE agent_profile (
    agent_id TEXT PRIMARY KEY NOT NULL REFERENCES agent(id),
    company_id TEXT NOT NULL REFERENCES company(id),
    display_name TEXT NOT NULL,
    role_label TEXT NOT NULL,
    avatar_variant TEXT NOT NULL,
    monthly_budget_minor INTEGER NOT NULL DEFAULT 0 CHECK (monthly_budget_minor >= 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns > 0)
) STRICT;
