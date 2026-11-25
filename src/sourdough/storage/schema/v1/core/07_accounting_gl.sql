-- Canonical v1 core general ledger and period accounting close tables.

CREATE TABLE journal_entry (
    id TEXT PRIMARY KEY NOT NULL,
    source TEXT NOT NULL,
    source_event TEXT NOT NULL,
    evidence_revision TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    mapping_revision TEXT NOT NULL,
    reverses TEXT UNIQUE REFERENCES journal_entry(id),
    replaces TEXT UNIQUE REFERENCES journal_entry(id),
    units_per_whole INTEGER CHECK (units_per_whole IS NULL OR units_per_whole > 0),
    UNIQUE (source, source_event),
    CHECK (reverses IS NULL OR replaces IS NULL),
    CHECK (reverses IS NULL OR reverses != id),
    CHECK (replaces IS NULL OR replaces != id)
) STRICT;

CREATE TABLE journal_line (
    entry_id TEXT NOT NULL REFERENCES journal_entry(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    account TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor != 0),
    PRIMARY KEY (entry_id, ordinal)
) STRICT;

CREATE TABLE accounting_close (
    request_id TEXT PRIMARY KEY NOT NULL,
    scope TEXT NOT NULL,
    supersedes TEXT UNIQUE REFERENCES accounting_close(request_id),
    request TEXT NOT NULL,
    receipt TEXT NOT NULL,
    assessed_ns INTEGER CHECK (assessed_ns IS NULL OR assessed_ns > 0),
    CHECK (supersedes IS NULL OR supersedes != request_id)
) STRICT;
