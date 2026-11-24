-- Persist the authenticated replacement database digest when retiring a company
-- store so resumed post-retirement restore verifies replacement bytes fail-closed.

CREATE TABLE IF NOT EXISTS company_store_retired_digest (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    request_revision TEXT NOT NULL,
    replacement_sha256 TEXT NOT NULL
) STRICT;
