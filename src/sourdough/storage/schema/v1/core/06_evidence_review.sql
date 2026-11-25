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
