-- Multi-book Company Paper trading metadata, per-book simulated order/fill/position
-- ledgers, and immutable fail-closed Paper-to-Live migration records.

CREATE TABLE company_paper_book (
    binding_id TEXT PRIMARY KEY NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    book_name TEXT NOT NULL,
    strategy_release_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('active', 'nominated', 'shadow', 'retired')),
    shadow_of_live_binding_id TEXT REFERENCES execution_environment_binding(binding_id),
    nominated_question_id TEXT REFERENCES decision_question(question_id),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns >= created_at_ns),
    CHECK (role != 'shadow' OR shadow_of_live_binding_id IS NOT NULL)
) STRICT;

CREATE INDEX idx_company_paper_book_company_role
    ON company_paper_book(company_id, role, created_at_ns);

CREATE TABLE company_paper_order (
    order_id TEXT PRIMARY KEY NOT NULL,
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    actor_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_minor_units INTEGER NOT NULL CHECK (limit_minor_units > 0),
    market_price_minor_units INTEGER NOT NULL CHECK (market_price_minor_units > 0),
    quote_observed_at_ns INTEGER NOT NULL CHECK (quote_observed_at_ns > 0),
    quote_source TEXT NOT NULL,
    fee_bps INTEGER NOT NULL CHECK (fee_bps >= 0 AND fee_bps <= 1000),
    slippage_bps INTEGER NOT NULL CHECK (slippage_bps >= 0 AND slippage_bps <= 500),
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled', 'rejected')),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
    fill_price_minor_units INTEGER CHECK (fill_price_minor_units IS NULL OR fill_price_minor_units > 0),
    fee_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (fee_minor_units >= 0),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns >= created_at_ns),
    UNIQUE (binding_id, action_id)
) STRICT;

CREATE INDEX idx_company_paper_order_binding_time
    ON company_paper_order(binding_id, created_at_ns);

CREATE TABLE company_paper_fill (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES company_paper_order(order_id),
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    actor_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_minor_units INTEGER NOT NULL CHECK (price_minor_units > 0),
    fee_minor_units INTEGER NOT NULL CHECK (fee_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    filled_at_ns INTEGER NOT NULL CHECK (filled_at_ns > 0)
) STRICT;

CREATE INDEX idx_company_paper_fill_binding
    ON company_paper_fill(binding_id, filled_at_ns);

CREATE TABLE company_paper_position (
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    instrument_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_entry_minor_units INTEGER NOT NULL CHECK (avg_entry_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    total_fees_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (total_fees_minor_units >= 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns > 0),
    PRIMARY KEY (binding_id, instrument_id)
) STRICT;

CREATE TABLE paper_live_migration_record (
    migration_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    question_id TEXT REFERENCES decision_question(question_id),
    source_paper_binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    target_live_binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    replaced_live_binding_id TEXT REFERENCES execution_environment_binding(binding_id),
    qualification_receipt_id TEXT NOT NULL REFERENCES venue_qualification_receipt(receipt_id),
    migration_mode TEXT NOT NULL CHECK (migration_mode IN ('clean_start', 'drain_incumbent')),
    source_paper_disposition TEXT NOT NULL CHECK (source_paper_disposition IN ('keep_shadow', 'retire_source')),
    positions_copied INTEGER NOT NULL DEFAULT 0 CHECK (positions_copied = 0),
    pre_check_summary_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0)
) STRICT;

CREATE INDEX idx_paper_live_migration_company_time
    ON paper_live_migration_record(company_id, recorded_at_ns);
