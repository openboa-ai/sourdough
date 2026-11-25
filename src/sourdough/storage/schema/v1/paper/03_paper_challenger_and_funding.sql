-- Canonical v1 opt-in paper strategy releases, PIT catalogs, 8h funding GL
-- settlements, paired incumbent-vs-challenger trials, and strategy deployments.

CREATE TABLE paper_pit_catalog_manifest (
    manifest_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    dataset_name TEXT NOT NULL,
    valid_from_ns INTEGER NOT NULL CHECK (valid_from_ns > 0),
    valid_through_ns INTEGER NOT NULL CHECK (valid_through_ns > valid_from_ns),
    available_at_ns INTEGER NOT NULL CHECK (available_at_ns >= valid_from_ns),
    files_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0)
) STRICT;

CREATE TABLE paper_strategy_release (
    release_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    owner_agent_id TEXT NOT NULL REFERENCES agent(id),
    parent_release_id TEXT REFERENCES paper_strategy_release(release_id),
    strategy_source_sha256 TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    nautilus_lock_sha256 TEXT NOT NULL,
    instrument_rules_sha256 TEXT NOT NULL,
    risk_config_sha256 TEXT NOT NULL,
    fill_policy_sha256 TEXT NOT NULL,
    pit_manifest_id TEXT NOT NULL REFERENCES paper_pit_catalog_manifest(manifest_id),
    pit_manifest_sha256 TEXT NOT NULL,
    research_trial_count_n INTEGER NOT NULL CHECK (research_trial_count_n >= 1),
    bundle_sha256 TEXT NOT NULL UNIQUE,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    UNIQUE (company_id, research_trial_count_n),
    CHECK (parent_release_id IS NULL OR parent_release_id != release_id)
) STRICT;

CREATE TABLE paper_funding_settlement (
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

CREATE TABLE paper_challenger_trial (
    comparison_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    incumbent_release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
    challenger_release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
    baseline_statement_revision TEXT NOT NULL,
    analysis_plan_json TEXT NOT NULL,
    analysis_plan_sha256 TEXT NOT NULL,
    incumbent_arm_json TEXT NOT NULL,
    challenger_arm_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('registered', 'completed', 'invalidated')),
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('PROMOTE', 'REJECT', 'INCONCLUSIVE')),
    verdict_json TEXT,
    invalidation_reason TEXT,
    registered_at_ns INTEGER NOT NULL CHECK (registered_at_ns > 0),
    evaluated_at_ns INTEGER CHECK (evaluated_at_ns IS NULL OR evaluated_at_ns >= registered_at_ns),
    CHECK (incumbent_release_id != challenger_release_id),
    CHECK ((status = 'completed') = (verdict IS NOT NULL)),
    CHECK ((status = 'completed') = (verdict_json IS NOT NULL)),
    CHECK ((status = 'invalidated') = (invalidation_reason IS NOT NULL)),
    CHECK ((status = 'registered') = (evaluated_at_ns IS NULL))
) STRICT;

CREATE TABLE paper_strategy_deployment (
    deployment_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    release_id TEXT NOT NULL REFERENCES paper_strategy_release(release_id),
    program_identity TEXT NOT NULL UNIQUE,
    binding_revision TEXT NOT NULL,
    comparison_id TEXT UNIQUE REFERENCES paper_challenger_trial(comparison_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'rolled_back', 'superseded')),
    command_id TEXT NOT NULL,
    activated_at_ns INTEGER NOT NULL CHECK (activated_at_ns > 0),
    retired_at_ns INTEGER CHECK (retired_at_ns IS NULL OR retired_at_ns >= activated_at_ns),
    UNIQUE (company_id, command_id),
    CHECK ((status = 'active') = (retired_at_ns IS NULL)),
    FOREIGN KEY (company_id, command_id) REFERENCES command_receipt(company_id, command_id)
) STRICT;

CREATE UNIQUE INDEX idx_paper_strategy_deployment_single_active
    ON paper_strategy_deployment(company_id)
    WHERE status = 'active';
