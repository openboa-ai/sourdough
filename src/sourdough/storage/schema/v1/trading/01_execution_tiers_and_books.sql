-- Canonical v1 opt-in Trading domain: three-tier execution bindings, unified
-- trading_book_* ledgers (agent_sandbox, paper, live), venue qualification
-- receipts, promotion records, and paper-to-live migration records.

CREATE TABLE execution_environment_binding (
    binding_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    tier TEXT NOT NULL CHECK (tier IN ('agent_sandbox', 'company_paper', 'company_live')),
    environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live')),
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
    binding_revision TEXT NOT NULL DEFAULT '1',
    book_name TEXT,
    strategy_release_id TEXT,
    book_role TEXT CHECK (book_role IS NULL OR book_role IN ('active', 'nominated', 'shadow', 'retired')),
    shadow_of_live_binding_id TEXT REFERENCES execution_environment_binding(binding_id),
    nominated_question_id TEXT REFERENCES decision_question(question_id),
    status TEXT NOT NULL CHECK (status IN ('registered', 'active', 'suspended', 'retired')),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    updated_at_ns INTEGER NOT NULL CHECK (updated_at_ns >= created_at_ns),
    CHECK ((tier = 'agent_sandbox') = (owner_agent_id IS NOT NULL)),
    CHECK (tier != 'agent_sandbox' OR environment = 'agent_sandbox'),
    CHECK (tier != 'company_paper' OR environment = 'paper'),
    CHECK (tier != 'company_live' OR environment = 'live'),
    CHECK (environment != 'live' OR credential_handle_ref IS NOT NULL),
    CHECK (book_role IS NULL OR book_role != 'shadow' OR shadow_of_live_binding_id IS NOT NULL),
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

CREATE INDEX idx_exec_binding_company_book_role
    ON execution_environment_binding(company_id, book_role, created_at_ns)
    WHERE book_role IS NOT NULL;

CREATE TABLE trading_book_order (
    order_id TEXT PRIMARY KEY NOT NULL,
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    actor_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    external_order_id TEXT,
    run_id TEXT REFERENCES work_runs(run_id),
    decision_id TEXT,
    mandate_revision TEXT,
    cancelled_at_ns INTEGER CHECK (cancelled_at_ns IS NULL OR cancelled_at_ns > 0),
    cancel_reason TEXT,
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

CREATE INDEX idx_trading_book_order_actor_time
    ON trading_book_order(company_id, actor_id, created_at_ns);

CREATE TABLE trading_book_fill (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES trading_book_order(order_id),
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live')),
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

CREATE INDEX idx_trading_book_fill_order
    ON trading_book_fill(order_id);

CREATE TABLE trading_book_position (
    binding_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live')),
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
    environment TEXT NOT NULL CHECK (environment IN ('agent_sandbox', 'paper', 'live')),
    company_id TEXT NOT NULL REFERENCES company(id),
    label TEXT NOT NULL,
    nav_minor_units INTEGER NOT NULL,
    cash_minor_units INTEGER NOT NULL,
    gross_exposure_minor_units INTEGER NOT NULL CHECK (gross_exposure_minor_units >= 0),
    realized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    unrealized_pnl_minor_units INTEGER NOT NULL DEFAULT 0,
    total_fees_minor_units INTEGER NOT NULL DEFAULT 0 CHECK (total_fees_minor_units >= 0),
    source TEXT NOT NULL DEFAULT 'book_valuation',
    binding_revision TEXT,
    policy TEXT,
    evidence_json TEXT,
    revision TEXT UNIQUE,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    FOREIGN KEY (binding_id, environment) REFERENCES execution_environment_binding(binding_id, environment)
) STRICT;

CREATE INDEX idx_trading_book_valuation_env_binding
    ON trading_book_valuation(company_id, environment, binding_id, recorded_at_ns);

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

CREATE TABLE trading_live_migration_record (
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

CREATE INDEX idx_trading_live_migration_company_time
    ON trading_live_migration_record(company_id, recorded_at_ns);
