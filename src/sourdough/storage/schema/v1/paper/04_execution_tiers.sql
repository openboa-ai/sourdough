-- Canonical v1 opt-in three-tier execution accounts (agent_sandbox, company_paper,
-- company_live), isolated per-Agent sandbox order/fill/position ledgers, external
-- testnet qualification receipts, and audited live promotion/demotion records.

CREATE TABLE execution_environment_binding (
    binding_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    tier TEXT NOT NULL CHECK (tier IN ('agent_sandbox', 'company_paper', 'company_live')),
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'external_test', 'live')),
    owner_agent_id TEXT REFERENCES agent(id),
    account_id TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    connector_revision TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL,
    currency TEXT NOT NULL,
    starting_minor_units INTEGER NOT NULL CHECK (starting_minor_units > 0),
    exposure_limit_minor_units INTEGER NOT NULL CHECK (
        exposure_limit_minor_units > 0 AND exposure_limit_minor_units <= starting_minor_units
    ),
    fee_bps INTEGER NOT NULL DEFAULT 0 CHECK (fee_bps >= 0 AND fee_bps <= 1000),
    slippage_bps INTEGER NOT NULL DEFAULT 0 CHECK (slippage_bps >= 0 AND slippage_bps <= 500),
    quote_source TEXT NOT NULL DEFAULT 'live_read_only',
    credential_handle_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('registered', 'active', 'suspended', 'retired')),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns >= created_at_ns),
    CHECK ((tier = 'agent_sandbox') = (owner_agent_id IS NOT NULL)),
    CHECK (tier != 'agent_sandbox' OR environment = 'paper'),
    CHECK (tier != 'company_paper' OR environment = 'paper'),
    CHECK (tier != 'company_live' OR environment = 'live'),
    CHECK (environment != 'live' OR credential_handle_ref IS NOT NULL),
    UNIQUE (binding_id, environment)
) STRICT;

CREATE UNIQUE INDEX idx_exec_binding_unique_active_company_paper_account
    ON execution_environment_binding(company_id, account_id)
    WHERE tier = 'company_paper' AND status = 'active';

CREATE UNIQUE INDEX idx_exec_binding_single_active_company_live
    ON execution_environment_binding(company_id)
    WHERE tier = 'company_live' AND status = 'active';

CREATE UNIQUE INDEX idx_exec_binding_single_active_agent_sandbox
    ON execution_environment_binding(company_id, owner_agent_id)
    WHERE tier = 'agent_sandbox' AND status = 'active';

CREATE INDEX idx_exec_binding_company_tier_status
    ON execution_environment_binding(company_id, tier, status);

CREATE TABLE agent_sandbox_order (
    order_id TEXT PRIMARY KEY NOT NULL,
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
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

CREATE INDEX idx_agent_sandbox_order_agent_time
    ON agent_sandbox_order(company_id, agent_id, created_at_ns);

CREATE TABLE agent_sandbox_fill (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES agent_sandbox_order(order_id),
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_minor_units INTEGER NOT NULL CHECK (price_minor_units > 0),
    fee_minor_units INTEGER NOT NULL CHECK (fee_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    filled_at_ns INTEGER NOT NULL CHECK (filled_at_ns > 0)
) STRICT;

CREATE INDEX idx_agent_sandbox_fill_binding
    ON agent_sandbox_fill(binding_id, filled_at_ns);

CREATE TABLE agent_sandbox_position (
    binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    instrument_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_entry_minor_units INTEGER NOT NULL CHECK (avg_entry_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    total_fees_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (total_fees_minor_units >= 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns > 0),
    PRIMARY KEY (binding_id, instrument_id)
) STRICT;

CREATE TABLE venue_qualification_receipt (
    receipt_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    adapter_revision TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('paper', 'external_test')),
    destination_id TEXT NOT NULL,
    instrument_rules_digest TEXT NOT NULL,
    core_digest TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAIL', 'INCOMPLETE', 'BLOCKED')),
    cases_passed INTEGER NOT NULL CHECK (cases_passed >= 0),
    cases_failed INTEGER NOT NULL CHECK (cases_failed >= 0),
    cases_unrun INTEGER NOT NULL CHECK (cases_unrun >= 0),
    evidence_sha256 TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    CHECK (verdict != 'PASS' OR (cases_passed > 0 AND cases_failed = 0))
) STRICT;

CREATE INDEX idx_venue_qual_company_env
    ON venue_qualification_receipt(company_id, environment, recorded_at_ns);

CREATE TABLE environment_promotion_record (
    promotion_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    command_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('promote_live', 'suspend_live', 'demote_to_paper')),
    from_binding_id TEXT REFERENCES execution_environment_binding(binding_id),
    to_binding_id TEXT NOT NULL REFERENCES execution_environment_binding(binding_id),
    qualification_receipt_id TEXT REFERENCES venue_qualification_receipt(receipt_id),
    payload_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    UNIQUE (company_id, command_id),
    FOREIGN KEY (company_id, command_id) REFERENCES command_receipt(company_id, command_id)
) STRICT;
