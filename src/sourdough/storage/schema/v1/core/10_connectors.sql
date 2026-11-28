-- Canonical v1 core scoped connector registry and actor grant tables.
-- Raw credentials are strictly prohibited in SQLite; only opaque envref:/keychain:
-- handles are permitted on connector_binding.

CREATE TABLE connector_binding (
    binding_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    company_id TEXT NOT NULL REFERENCES company(id),
    connector_kind TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (
        environment IN ('paper', 'external_test', 'live', 'internal')
    ),
    endpoint_url TEXT NOT NULL,
    allowed_hosts_json TEXT NOT NULL,
    operations_json TEXT NOT NULL,
    credential_handle_ref TEXT CHECK (
        credential_handle_ref IS NULL
        OR credential_handle_ref GLOB 'envref:*'
        OR credential_handle_ref GLOB 'keychain:*'
    ),
    recovery_capabilities_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'disabled', 'superseded')
    ),
    max_requests_per_window INTEGER NOT NULL DEFAULT 60 CHECK (
        max_requests_per_window >= 1
    ),
    window_seconds INTEGER NOT NULL DEFAULT 60 CHECK (
        window_seconds >= 1
    ),
    budget_ceiling_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (
        budget_ceiling_minor_units >= 0
    ),
    registered_by_actor_id TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    PRIMARY KEY (binding_id, revision)
) STRICT;

CREATE INDEX idx_connector_binding_company_status
    ON connector_binding(company_id, binding_id, status, revision);

CREATE TABLE connector_grant (
    grant_binding_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    standing_grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL CHECK (binding_revision >= 1),
    allowed_operations_json TEXT NOT NULL,
    max_cost_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (
        max_cost_minor_units >= 0
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'revoked')
    ),
    granted_by_actor_id TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    revoked_at_ns INTEGER,
    FOREIGN KEY (binding_id, binding_revision)
        REFERENCES connector_binding(binding_id, revision)
) STRICT;

CREATE INDEX idx_connector_grant_lookup
    ON connector_grant(company_id, agent_id, binding_id, binding_revision, status);

CREATE TABLE connector_admission (
    admission_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL CHECK (binding_revision >= 1),
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (
        environment IN ('paper', 'external_test', 'live', 'internal')
    ),
    endpoint_url TEXT NOT NULL,
    issued_by_service TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'admitted' CHECK (
        status IN ('admitted', 'consumed', 'revoked')
    ),
    expires_at_ns INTEGER,
    created_at_ns INTEGER NOT NULL,
    consumed_at_ns INTEGER,
    FOREIGN KEY (binding_id, binding_revision)
        REFERENCES connector_binding(binding_id, revision)
) STRICT;

CREATE INDEX idx_connector_admission_lookup
    ON connector_admission(company_id, binding_id, binding_revision, operation, request_id);
