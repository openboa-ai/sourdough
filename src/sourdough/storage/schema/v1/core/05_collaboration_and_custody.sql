-- Canonical v1 core shared projects, selective publication, project conversations,
-- work handoff, and agent retirement archive tables. Private drafts remain physically
-- isolated under each Agent's private Desk directory outside company.sqlite.

CREATE TABLE shared_project (
    project_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    revision TEXT NOT NULL,
    members TEXT NOT NULL,
    publishers TEXT NOT NULL
) STRICT;

CREATE TABLE shared_publication (
    asset_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    project_id TEXT NOT NULL REFERENCES shared_project(project_id),
    author_id TEXT NOT NULL REFERENCES agent(id),
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    audience TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE (author_id, request_id)
) STRICT;

CREATE TABLE project_review_brief (
    work_item_id TEXT PRIMARY KEY NOT NULL REFERENCES work_items(work_item_id),
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    project_id TEXT NOT NULL REFERENCES shared_project(project_id),
    manager_grant TEXT NOT NULL REFERENCES standing_grant(id),
    deliverable TEXT NOT NULL
) STRICT;

CREATE TABLE project_message (
    message_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    project_id TEXT NOT NULL REFERENCES shared_project(project_id),
    sender_id TEXT NOT NULL REFERENCES agent(id),
    run_id TEXT NOT NULL REFERENCES work_runs(run_id),
    body TEXT NOT NULL,
    asset_id TEXT REFERENCES shared_publication(asset_id),
    audience TEXT NOT NULL,
    membership_revision TEXT NOT NULL,
    request_id TEXT NOT NULL,
    UNIQUE (project_id, sender_id, request_id)
) STRICT;

CREATE TABLE project_delivery (
    delivery_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    message_id TEXT NOT NULL REFERENCES project_message(message_id),
    recipient_id TEXT NOT NULL REFERENCES agent(id),
    read_run_id TEXT REFERENCES work_runs(run_id),
    UNIQUE (message_id, recipient_id)
) STRICT;

CREATE TABLE agent_archive (
    cutoff INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL REFERENCES company(id),
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent(id),
    manager_id TEXT NOT NULL REFERENCES agent(id),
    grant_id TEXT NOT NULL REFERENCES standing_grant(id),
    request_id TEXT NOT NULL,
    employment_revision TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_scope TEXT NOT NULL,
    UNIQUE (manager_id, request_id)
) STRICT;

CREATE TABLE work_handoff (
    offer_id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES company(id),
    request_id TEXT NOT NULL,
    sender_id TEXT NOT NULL REFERENCES agent(id),
    sender_run_id TEXT NOT NULL REFERENCES work_runs(run_id),
    original_work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
    recipient_id TEXT NOT NULL REFERENCES agent(id),
    asset_id TEXT NOT NULL REFERENCES shared_publication(asset_id),
    expires_at INTEGER NOT NULL,
    expected_result TEXT NOT NULL,
    successor_work_item_id TEXT REFERENCES work_items(work_item_id),
    accepted_run_id TEXT REFERENCES work_runs(run_id),
    UNIQUE (sender_id, request_id)
) STRICT;
