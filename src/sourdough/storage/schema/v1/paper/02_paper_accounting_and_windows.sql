-- Canonical v1 opt-in paper accounting source, fee delivery, valuation,
-- operating window, and retained statement tables.

CREATE TABLE paper_fill_accounting_source (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill_fee(fill_id),
    destination_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    mapping_revision TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL
) STRICT;

CREATE TABLE paper_fee_delivery (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES paper_fill_accounting_source(fill_id),
    delivery_digest TEXT NOT NULL,
    acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1)),
    journal_id TEXT UNIQUE REFERENCES journal_entry(id)
) STRICT;

CREATE TABLE paper_valuation (
    capture_id TEXT PRIMARY KEY NOT NULL,
    binding_revision TEXT NOT NULL,
    policy TEXT NOT NULL,
    evidence TEXT NOT NULL,
    trading_nav_atoms INTEGER NOT NULL CHECK (trading_nav_atoms >= 0),
    revision TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE paper_operating_window (
    window_id TEXT PRIMARY KEY NOT NULL,
    contract TEXT NOT NULL,
    committed_ns INTEGER CHECK (committed_ns IS NULL OR committed_ns > 0),
    revision TEXT UNIQUE,
    CHECK ((committed_ns IS NULL) = (revision IS NULL))
) STRICT;

CREATE TABLE paper_statement (
    request_id TEXT PRIMARY KEY NOT NULL,
    window_id TEXT NOT NULL REFERENCES paper_operating_window(window_id),
    supersedes TEXT UNIQUE REFERENCES paper_statement(request_id),
    payload TEXT NOT NULL,
    revision TEXT NOT NULL UNIQUE,
    CHECK (supersedes IS NULL OR supersedes != request_id)
) STRICT;
