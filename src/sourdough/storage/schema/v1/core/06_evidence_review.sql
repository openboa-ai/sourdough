-- Canonical v1 core evidence review qualification and activation tables.
-- Keeps immutable candidates, independent verdicts, effective revisions,
-- lineage parent links, and quarantine status distinct inside company.sqlite.

CREATE TABLE review_qualification (
    company_id TEXT NOT NULL REFERENCES company(id),
    receipt_id TEXT NOT NULL,
    candidate_asset_id TEXT NOT NULL,
    baseline_asset_id TEXT,
    parent_asset_id TEXT,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    trial_count INTEGER NOT NULL DEFAULT 1 CHECK (trial_count >= 1),
    evidence TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL,
    PRIMARY KEY (company_id, receipt_id)
) STRICT;

CREATE INDEX idx_review_qualification_candidate
    ON review_qualification(company_id, candidate_asset_id);

CREATE TABLE review_tool_activation (
    company_id TEXT NOT NULL REFERENCES company(id),
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('activate', 'revert', 'quarantine')),
    candidate_id TEXT NOT NULL,
    qualification_id TEXT NOT NULL,
    parent_asset_id TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    principal_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)),
    quarantine_reason TEXT,
    recorded_at_ns INTEGER NOT NULL,
    PRIMARY KEY (company_id, request_id),
    UNIQUE (company_id, revision),
    FOREIGN KEY (company_id, qualification_id)
        REFERENCES review_qualification(company_id, receipt_id),
    FOREIGN KEY (company_id, request_id)
        REFERENCES command_receipt(company_id, command_id)
) STRICT;

CREATE INDEX idx_review_tool_activation_lineage
    ON review_tool_activation(company_id, parent_asset_id, revision);

CREATE TABLE method_delta (
    company_id TEXT NOT NULL REFERENCES company(id),
    method_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    delta_kind TEXT NOT NULL CHECK (delta_kind IN ('ADD', 'PATCH', 'DEPRECATE')),
    item_kind TEXT NOT NULL CHECK (
        item_kind IN ('work_method', 'improvement_procedure', 'helper_script')
    ),
    trigger_spec_json TEXT NOT NULL,
    body_text TEXT NOT NULL,
    audience_scope TEXT NOT NULL CHECK (audience_scope IN ('one_agent', 'project', 'company')),
    audience_target_id TEXT,
    author_agent_id TEXT NOT NULL REFERENCES agent(id),
    parent_method_ids_json TEXT NOT NULL,
    active_snapshot_id TEXT,
    source_refs_json TEXT NOT NULL,
    procedure_meta_json TEXT,
    uncertainty_note TEXT NOT NULL,
    falsification_criteria TEXT NOT NULL,
    superseded_revision TEXT,
    redaction_passed INTEGER NOT NULL CHECK (redaction_passed IN (0, 1)),
    redaction_report_json TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'disputed', 'active', 'quarantined', 'retired')
    ),
    created_at_ns INTEGER NOT NULL CHECK (created_at_ns > 0),
    PRIMARY KEY (company_id, method_id, revision_sha256),
    CHECK (redaction_passed = 1 OR audience_scope = 'one_agent' OR status != 'active')
) STRICT;

CREATE INDEX idx_method_delta_status
    ON method_delta(company_id, item_kind, audience_scope, status);

CREATE TABLE method_qualification (
    company_id TEXT NOT NULL REFERENCES company(id),
    qualification_id TEXT NOT NULL,
    method_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    baseline_revision_sha256 TEXT,
    stage1_passed INTEGER NOT NULL CHECK (stage1_passed IN (0, 1)),
    stage2_passed INTEGER NOT NULL CHECK (stage2_passed IN (0, 1)),
    stage3_passed INTEGER NOT NULL CHECK (stage3_passed IN (0, 1)),
    trial_count INTEGER NOT NULL CHECK (trial_count >= 1),
    metrics_json TEXT NOT NULL,
    evaluator_revision TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    PRIMARY KEY (company_id, qualification_id),
    FOREIGN KEY (company_id, method_id, revision_sha256)
        REFERENCES method_delta(company_id, method_id, revision_sha256)
) STRICT;

CREATE TABLE method_activation (
    company_id TEXT NOT NULL REFERENCES company(id),
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('activate', 'revert', 'quarantine')),
    method_id TEXT NOT NULL,
    revision_sha256 TEXT NOT NULL,
    qualification_id TEXT NOT NULL,
    audience_scope TEXT NOT NULL CHECK (audience_scope IN ('one_agent', 'project', 'company')),
    audience_target_id TEXT,
    activation_seq INTEGER NOT NULL CHECK (activation_seq >= 1),
    principal_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)),
    quarantine_reason TEXT,
    recorded_at_ns INTEGER NOT NULL CHECK (recorded_at_ns > 0),
    PRIMARY KEY (company_id, request_id),
    UNIQUE (company_id, activation_seq),
    FOREIGN KEY (company_id, qualification_id)
        REFERENCES method_qualification(company_id, qualification_id),
    FOREIGN KEY (company_id, request_id)
        REFERENCES command_receipt(company_id, command_id)
) STRICT;

CREATE INDEX idx_method_activation_scope
    ON method_activation(company_id, audience_scope, audience_target_id, activation_seq);
