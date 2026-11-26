-- Unified company trading book ledgers shared identically by company_paper and
-- company_live bindings, physically isolated by composite (binding_id, environment)
-- foreign keys so paper and live rows can never cross-contaminate.

CREATE TABLE trading_book_order (
    order_id TEXT PRIMARY KEY NOT NULL,
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    actor_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    external_order_id TEXT,
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
    UNIQUE (binding_id, action_id),
    FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)
) STRICT;

CREATE INDEX idx_trading_book_order_env_binding
    ON trading_book_order(company_id, environment, binding_id, created_at_ns);

CREATE TABLE trading_book_fill (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES trading_book_order(order_id),
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    actor_id TEXT NOT NULL,
    external_fill_id TEXT,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_minor_units INTEGER NOT NULL CHECK (price_minor_units > 0),
    fee_minor_units INTEGER NOT NULL CHECK (fee_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    filled_at_ns INTEGER NOT NULL CHECK (filled_at_ns > 0),
    FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)
) STRICT;

CREATE INDEX idx_trading_book_fill_env_binding
    ON trading_book_fill(company_id, environment, binding_id, filled_at_ns);

CREATE TABLE trading_book_position (
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    instrument_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_entry_minor_units INTEGER NOT NULL CHECK (avg_entry_minor_units >= 0),
    mark_price_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (mark_price_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    total_fees_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (total_fees_minor_units >= 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns > 0),
    PRIMARY KEY (binding_id, instrument_id),
    FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)
) STRICT;

CREATE TABLE trading_book_valuation (
    valuation_id TEXT PRIMARY KEY NOT NULL,
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    label TEXT NOT NULL,
    nav_minor_units INTEGER NOT NULL,
    cash_minor_units INTEGER NOT NULL,
    gross_exposure_minor_units INTEGER NOT NULL CHECK (gross_exposure_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    unrealized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    total_fees_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (total_fees_minor_units >= 0),
    source TEXT NOT NULL DEFAULT 'book_valuation',
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)
) STRICT;

CREATE INDEX idx_trading_book_valuation_env_binding
    ON trading_book_valuation(company_id, environment, binding_id, recorded_at_ns);
