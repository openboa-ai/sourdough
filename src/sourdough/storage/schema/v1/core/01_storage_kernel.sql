-- Canonical v1 storage kernel tables: domain registry, append-only audit log,
-- idempotent command receipts, external action outbox, restore receipts, and
-- unified store retirement fencing with replacement digest.

CREATE TABLE installed_domains (
    domain TEXT PRIMARY KEY CHECK (domain IN ('core', 'paper')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    pack_sha256 TEXT NOT NULL,
    sqlite_schema_sha256 TEXT NOT NULL,
    activated_at_ns INTEGER NOT NULL
) STRICT;

CREATE TABLE audit_event (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    run_id TEXT,
    command_id TEXT,
    payload_json TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_audit_event_company_seq
    ON audit_event(company_id, event_seq);

CREATE TRIGGER tr_audit_event_no_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only');
END;

CREATE TRIGGER tr_audit_event_no_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only');
END;

CREATE TRIGGER tr_audit_event_no_replace
BEFORE INSERT ON audit_event
WHEN NEW.event_seq IS NOT NULL AND EXISTS (
    SELECT 1 FROM audit_event WHERE event_seq = NEW.event_seq
)
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only');
END;

CREATE TABLE command_receipt (
    company_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    peer_pid INTEGER,
    command_type TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    preconditions_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('accepted', 'applied', 'denied')),
    denial_code TEXT,
    result_json TEXT,
    applied_seq INTEGER,
    created_at_ns INTEGER NOT NULL,
    finalized_at_ns INTEGER,
    PRIMARY KEY (company_id, command_id)
) STRICT;

CREATE TABLE external_actions (
    idempotency_key TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    run_id TEXT,
    command_id TEXT,
    connector TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'in_flight', 'confirmed', 'rejected', 'unknown')),
    receipt_json TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_external_actions_company_status
    ON external_actions(company_id, status);

CREATE TABLE company_restore_receipt (
    request_revision TEXT PRIMARY KEY,
    checkpoint_sha256 TEXT NOT NULL,
    watermark_json TEXT NOT NULL,
    restored_at_ns INTEGER NOT NULL
) STRICT;

CREATE TABLE company_store_retired (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    request_revision TEXT NOT NULL,
    original_schema INTEGER NOT NULL CHECK (original_schema = 1),
    replacement_sha256 TEXT NOT NULL,
    retired_at_ns INTEGER NOT NULL
) STRICT;
