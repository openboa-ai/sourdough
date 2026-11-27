-- Canonical v1 opt-in Trading operating windows, retained statements,
-- 8h perpetual funding GL settlements, and GL fee delivery receipts.

CREATE TABLE trading_operating_window (
    window_id TEXT PRIMARY KEY NOT NULL,
    contract TEXT NOT NULL,
    committed_ns INTEGER CHECK (committed_ns IS NULL OR committed_ns > 0),
    revision TEXT UNIQUE,
    CHECK ((committed_ns IS NULL) = (revision IS NULL))
) STRICT;

CREATE TABLE trading_statement (
    request_id TEXT PRIMARY KEY NOT NULL,
    window_id TEXT NOT NULL REFERENCES trading_operating_window(window_id),
    supersedes TEXT UNIQUE REFERENCES trading_statement(request_id),
    payload TEXT NOT NULL,
    revision TEXT NOT NULL UNIQUE,
    CHECK (supersedes IS NULL OR supersedes != request_id)
) STRICT;

CREATE TABLE trading_funding_settlement (
    settlement_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    destination_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    interval_end_ns INTEGER NOT NULL CHECK (interval_end_ns > 0 AND interval_end_ns % 28800000000000 = 0),
    funding_rate_e9 INTEGER,
    mark_price_atoms INTEGER CHECK (mark_price_atoms IS NULL OR mark_price_atoms > 0),
    signed_position_lots INTEGER NOT NULL,
    funding_cashflow_atoms INTEGER,
    status TEXT NOT NULL CHECK (status IN ('booked', 'zero', 'unavailable')),
    unavailable_reason TEXT,
    evidence_sha256 TEXT NOT NULL,
    journal_id TEXT UNIQUE REFERENCES journal_entry(id),
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    UNIQUE (company_id, destination_id, account_id, instrument_id, interval_end_ns),
    CHECK ((status = 'booked') = (journal_id IS NOT NULL)),
    CHECK ((status = 'unavailable') = (funding_cashflow_atoms IS NULL)),
    CHECK ((status = 'unavailable') = (unavailable_reason IS NOT NULL)),
    CHECK (status != 'zero' OR funding_cashflow_atoms = 0),
    CHECK (status != 'booked' OR funding_cashflow_atoms != 0)
) STRICT;

CREATE TABLE trading_fee_delivery (
    fill_id TEXT PRIMARY KEY NOT NULL REFERENCES trading_book_fill(fill_id),
    delivery_digest TEXT NOT NULL,
    acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1)),
    journal_id TEXT UNIQUE REFERENCES journal_entry(id)
) STRICT;
