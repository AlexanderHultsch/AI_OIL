-- AI_OIL — SQLite schema v1
-- Authoritative executable form of docs/specification.md chapter 5.
-- Conventions:
--   * timestamps are ISO-8601 UTC text ('2026-10-26T09:15:00.000Z'), dates are 'YYYY-MM-DD'
--   * enumerations are CHECK constraints, so an invalid value can never be stored
--   * tables marked [P3] / [P4] / [P5] are created by the migration of that phase;
--     they are listed here so the model is reviewed as a whole

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

------------------------------------------------------------------------------
-- 1. People (DM-20 .. DM-23)
------------------------------------------------------------------------------

CREATE TABLE person (
    id              INTEGER PRIMARY KEY,
    display_name    TEXT    NOT NULL,
    organisation    TEXT,
    is_me           INTEGER NOT NULL DEFAULT 0 CHECK (is_me IN (0, 1)),
    merged_into_id  INTEGER REFERENCES person(id),   -- set when two records turn out to be one person
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX ux_person_one_me ON person(is_me) WHERE is_me = 1;

CREATE TABLE person_identity (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    system          TEXT    NOT NULL CHECK (system IN ('email', 'atlassian', 'teams')),
    external_id     TEXT    NOT NULL,   -- lower-case SMTP address / Atlassian accountId / Teams user id
    display_name    TEXT,               -- name as last seen in that system
    UNIQUE (system, external_id)
);

------------------------------------------------------------------------------
-- 2. Projects, components, labels (DM-1 .. DM-4)
------------------------------------------------------------------------------

CREATE TABLE project (
    id              INTEGER PRIMARY KEY,
    key             TEXT    NOT NULL UNIQUE
                            CHECK (key GLOB '[A-Z][A-Z0-9]*' AND length(key) BETWEEN 2 AND 10),
    name            TEXT    NOT NULL,
    description     TEXT,
    lead_id         INTEGER REFERENCES person(id),
    next_number     INTEGER NOT NULL DEFAULT 1,    -- high-water mark; numbers are never reused
    archived        INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE component (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES project(id),
    name            TEXT    NOT NULL,
    description     TEXT,
    archived        INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    UNIQUE (project_id, name)
);

CREATE TABLE label (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE COLLATE NOCASE CHECK (name NOT GLOB '*[ ,]*')
);

------------------------------------------------------------------------------
-- 3. Issues (DM-5 .. DM-15)
------------------------------------------------------------------------------

CREATE TABLE issue (
    id                  INTEGER PRIMARY KEY,
    project_id          INTEGER NOT NULL REFERENCES project(id),
    number              INTEGER NOT NULL,
    key                 TEXT    NOT NULL UNIQUE,     -- 'PRJ-123', immutable, never reused
    issue_type          TEXT    NOT NULL DEFAULT 'Task'
                                CHECK (issue_type IN ('Epic', 'Task', 'Sub-task')),
    parent_id           INTEGER REFERENCES issue(id),
    summary             TEXT    NOT NULL CHECK (length(trim(summary)) BETWEEN 1 AND 120),
    description         TEXT    NOT NULL CHECK (length(trim(description)) >= 1),
    acceptance_criteria TEXT,
    status              TEXT    NOT NULL DEFAULT 'New'
                                CHECK (status IN ('New', 'Ready to Proceed', 'In Progress', 'Blocked', 'Done')),
    resolution          TEXT    CHECK (resolution IN ('Completed', 'Won''t Do', 'Duplicate')),
    blocked_reason      TEXT,
    priority            TEXT    NOT NULL DEFAULT 'P4' CHECK (priority IN ('P1', 'P2', 'P3', 'P4', 'P5')),
    assignee_id         INTEGER REFERENCES person(id),
    reporter_id         INTEGER NOT NULL REFERENCES person(id),
    original_due_date   TEXT    CHECK (original_due_date IS NULL OR date(original_due_date) = original_due_date),
    revised_due_date    TEXT    CHECK (revised_due_date  IS NULL OR date(revised_due_date)  = revised_due_date),
    origin_type         TEXT    NOT NULL DEFAULT 'manual'
                                CHECK (origin_type IN ('manual', 'email', 'jira', 'confluence', 'sharepoint', 'teams', 'meeting')),
    snoozed_until       TEXT,               -- [P4] hidden from My Inbox until this date (AST-10)
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at         TEXT,
    UNIQUE (project_id, number),
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK ((status = 'Done') = (resolution IS NOT NULL)),                      -- DM-9
    CHECK (status <> 'Blocked' OR length(trim(coalesce(blocked_reason, ''))) > 0), -- DM-10
    CHECK ((original_due_date IS NULL) = (revised_due_date IS NULL))           -- DM-12
);
CREATE INDEX ix_issue_status   ON issue(status);
CREATE INDEX ix_issue_assignee ON issue(assignee_id);
CREATE INDEX ix_issue_due      ON issue(revised_due_date);
CREATE INDEX ix_issue_parent   ON issue(parent_id);

-- DM-12: the original due date is written once and never changed afterwards
CREATE TRIGGER tr_issue_original_due_immutable
BEFORE UPDATE OF original_due_date ON issue
WHEN OLD.original_due_date IS NOT NULL AND NEW.original_due_date IS NOT OLD.original_due_date
BEGIN
    SELECT RAISE(ABORT, 'original_due_date is immutable once set (DM-12)');
END;

-- DM-6: key and number never change
CREATE TRIGGER tr_issue_key_immutable
BEFORE UPDATE OF key, number, project_id ON issue
WHEN NEW.key IS NOT OLD.key OR NEW.number IS NOT OLD.number OR NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'issue key, number and project are immutable (DM-6)');
END;

CREATE TABLE issue_component (
    issue_id        INTEGER NOT NULL REFERENCES issue(id),
    component_id    INTEGER NOT NULL REFERENCES component(id),
    PRIMARY KEY (issue_id, component_id)
);

CREATE TABLE issue_label (
    issue_id        INTEGER NOT NULL REFERENCES issue(id),
    label_id        INTEGER NOT NULL REFERENCES label(id),
    PRIMARY KEY (issue_id, label_id)
);

-- DM-14: typed links between issues. 'relates_to' is stored once with from < to.
CREATE TABLE issue_link (
    id              INTEGER PRIMARY KEY,
    link_type       TEXT    NOT NULL CHECK (link_type IN ('relates_to', 'blocks', 'duplicates')),
    from_issue_id   INTEGER NOT NULL REFERENCES issue(id),
    to_issue_id     INTEGER NOT NULL REFERENCES issue(id),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (link_type, from_issue_id, to_issue_id),
    CHECK (from_issue_id <> to_issue_id),
    CHECK (link_type <> 'relates_to' OR from_issue_id < to_issue_id)
);

CREATE TABLE comment (
    id              INTEGER PRIMARY KEY,
    issue_id        INTEGER NOT NULL REFERENCES issue(id),
    author_id       INTEGER NOT NULL REFERENCES person(id),
    body            TEXT    NOT NULL CHECK (length(trim(body)) >= 1),   -- Markdown
    proposal_id     INTEGER REFERENCES proposal(id),                    -- set when an accepted AI proposal created it
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    edited_at       TEXT,
    deleted_at      TEXT                                                -- soft delete; history keeps the text
);
CREATE INDEX ix_comment_issue ON comment(issue_id, created_at);

------------------------------------------------------------------------------
-- 4. History (DM-30 .. DM-33) — append-only
------------------------------------------------------------------------------

CREATE TABLE change_set (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor_type      TEXT    NOT NULL CHECK (actor_type IN ('user', 'sync', 'system')),
    actor_id        INTEGER REFERENCES person(id),
    proposal_id     INTEGER REFERENCES proposal(id),   -- the AI proposal the user accepted, if any
    sync_run_id     INTEGER REFERENCES sync_run(id),
    note            TEXT
);

CREATE TABLE change_event (
    id              INTEGER PRIMARY KEY,
    change_set_id   INTEGER NOT NULL REFERENCES change_set(id),
    issue_id        INTEGER NOT NULL REFERENCES issue(id),
    field           TEXT    NOT NULL,   -- 'status', 'assignee', 'label', 'comment', 'source_link', 'issue_link', ...
    op              TEXT    NOT NULL CHECK (op IN ('create', 'set', 'add', 'remove', 'delete')),
    old_value       TEXT,               -- raw stored value (id or text); resolved to a display name at read time
    new_value       TEXT
);
CREATE INDEX ix_change_event_issue ON change_event(issue_id, id);

CREATE TRIGGER tr_change_set_no_update   BEFORE UPDATE ON change_set   BEGIN SELECT RAISE(ABORT, 'history is append-only (DM-31)'); END;
CREATE TRIGGER tr_change_set_no_delete   BEFORE DELETE ON change_set   BEGIN SELECT RAISE(ABORT, 'history is append-only (DM-31)'); END;
CREATE TRIGGER tr_change_event_no_update BEFORE UPDATE ON change_event BEGIN SELECT RAISE(ABORT, 'history is append-only (DM-31)'); END;
CREATE TRIGGER tr_change_event_no_delete BEFORE DELETE ON change_event BEGIN SELECT RAISE(ABORT, 'history is append-only (DM-31)'); END;

------------------------------------------------------------------------------
-- 5. Sources (DM-40 .. DM-59) — the core of the product
------------------------------------------------------------------------------

-- One row per configured connector instance (a second Jira site is a second row).
CREATE TABLE connector (
    id              INTEGER PRIMARY KEY,
    type            TEXT    NOT NULL,          -- plugin id: 'manual', 'outlook', 'jira', 'confluence', 'sharepoint', 'teams', ...
    name            TEXT    NOT NULL UNIQUE,   -- 'Outlook (work)', 'Jira Cloud'
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    settings_json   TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),  -- non-secret settings only
    credential_ref  TEXT,                      -- Windows Credential Manager target name — never the secret itself
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A thing that lives in another system: an email conversation, a Jira issue, a page, a file.
CREATE TABLE source_object (
    id              INTEGER PRIMARY KEY,
    connector_id    INTEGER NOT NULL REFERENCES connector(id),
    object_type     TEXT    NOT NULL CHECK (object_type IN (
                        'email_thread', 'attachment', 'jira_issue', 'confluence_page',
                        'file', 'teams_thread', 'note', 'web_link')),
    external_id     TEXT    NOT NULL,          -- immutable id in the home system (DM-42); attachments: '<parent message/issue id>#<position>'
    parent_id       INTEGER REFERENCES source_object(id),   -- attachment -> its thread / page / issue
    title           TEXT,                      -- subject, Jira summary, page title, file name
    display_ref     TEXT,                      -- human handle: 'ABC-123', file name, subject
    locator         TEXT,                      -- URL or path that opens it in its home system
    cursor          TEXT,                      -- connector-specific change marker (DM-45)
    first_seen_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_checked_at TEXT,
    last_changed_at TEXT,
    fetch_status    TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (fetch_status IN ('pending', 'ok', 'error', 'gone', 'skipped')),
    fetch_error     TEXT,
    UNIQUE (connector_id, object_type, external_id)
);
CREATE INDEX ix_source_object_parent ON source_object(parent_id);

-- Content-addressed store for raw originals kept on disk (blobs/<aa>/<hash>.gz).
CREATE TABLE blob (
    hash            TEXT    PRIMARY KEY,       -- sha256 of the raw bytes
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT,
    stored_path     TEXT    NOT NULL,          -- relative to the blob root
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Snapshots of the mutable "body" of a source object (DM-47 .. DM-49).
CREATE TABLE source_version (
    id                  INTEGER PRIMARY KEY,
    source_object_id    INTEGER NOT NULL REFERENCES source_object(id),
    version_no          INTEGER NOT NULL,
    fetched_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source_modified_at  TEXT,               -- when the home system says it changed
    source_version_ref  TEXT,               -- Confluence version number, Jira 'updated', file mtime
    content_hash        TEXT    NOT NULL,   -- sha256 over normalised text + significant metadata
    text                TEXT    NOT NULL,   -- normalised plain text: what search and AI read
    metadata_json       TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    blob_hash           TEXT    REFERENCES blob(hash),
    sync_run_id         INTEGER REFERENCES sync_run(id),
    UNIQUE (source_object_id, version_no)
    -- DM-48: a version is only written when content_hash differs from the latest version
    -- (checked by the sync engine, not by a UNIQUE constraint, so a revert A -> B -> A stays visible)
);

-- Append-only items inside a source object: email messages, comments, changelog entries (DM-50 .. DM-53).
CREATE TABLE source_entry (
    id                  INTEGER PRIMARY KEY,
    source_object_id    INTEGER NOT NULL REFERENCES source_object(id),
    entry_type          TEXT    NOT NULL CHECK (entry_type IN (
                            'email_message', 'jira_comment', 'jira_change',
                            'confluence_comment', 'teams_message', 'note_text')),
    external_id         TEXT    NOT NULL,   -- Internet Message-ID, comment id, changelog id
    revision            INTEGER NOT NULL DEFAULT 1,   -- > 1 when the home system edited the entry
    author_identity_id  INTEGER REFERENCES person_identity(id),
    direction           TEXT    CHECK (direction IN ('in', 'out')),  -- 'out' = written by me
    recipients_json     TEXT    CHECK (recipients_json IS NULL OR json_valid(recipients_json)),
    occurred_at         TEXT    NOT NULL,   -- sent / created time in the home system
    fetched_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    text                TEXT    NOT NULL,   -- full normalised body
    new_text            TEXT,               -- body without quoted history and signature (email)
    content_hash        TEXT    NOT NULL,
    metadata_json       TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    blob_hash           TEXT    REFERENCES blob(hash),
    sync_run_id         INTEGER REFERENCES sync_run(id),
    UNIQUE (source_object_id, entry_type, external_id, revision)
);
CREATE INDEX ix_source_entry_object ON source_entry(source_object_id, occurred_at);

-- The "dictionary" of an issue: which sources belong to it (DM-54 .. DM-59). Many-to-many.
CREATE TABLE issue_source_link (
    id                  INTEGER PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issue(id),
    source_object_id    INTEGER NOT NULL REFERENCES source_object(id),
    role                TEXT    NOT NULL DEFAULT 'reference'
                                CHECK (role IN ('origin', 'evidence', 'reference')),
    link_method         TEXT    NOT NULL CHECK (link_method IN ('manual', 'ai_suggestion')),
    proposal_id         INTEGER REFERENCES proposal(id),
    note                TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at          TEXT,
    CHECK (link_method <> 'ai_suggestion' OR proposal_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_issue_source_link_active
    ON issue_source_link(issue_id, source_object_id) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX ux_issue_source_link_one_origin
    ON issue_source_link(issue_id) WHERE role = 'origin' AND removed_at IS NULL;
CREATE INDEX ix_issue_source_link_source ON issue_source_link(source_object_id);

------------------------------------------------------------------------------
-- 6. Sync runs (SYNC-1 .. SYNC-11)
------------------------------------------------------------------------------

CREATE TABLE sync_run (
    id              INTEGER PRIMARY KEY,
    trigger         TEXT    NOT NULL CHECK (trigger IN ('ui', 'cli')),
    scope           TEXT    NOT NULL,   -- 'all' | 'issue:PRJ-12' | 'connector:3' | 'source:42' | 'scan:outlook'
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    stats_json      TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(stats_json))
);

CREATE TABLE sync_result (
    sync_run_id         INTEGER NOT NULL REFERENCES sync_run(id),
    source_object_id    INTEGER NOT NULL REFERENCES source_object(id),
    outcome             TEXT    NOT NULL CHECK (outcome IN ('unchanged', 'changed', 'new', 'error', 'gone', 'skipped')),
    new_versions        INTEGER NOT NULL DEFAULT 0,
    new_entries         INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    duration_ms         INTEGER,
    PRIMARY KEY (sync_run_id, source_object_id)
);

------------------------------------------------------------------------------
-- 7. Search (SRCH-1 .. SRCH-9)
------------------------------------------------------------------------------

-- One FTS document per issue text, comment, latest source version and source entry.
-- Maintained by the application in the same transaction as the row it mirrors.
CREATE VIRTUAL TABLE search_doc USING fts5(
    doc_kind UNINDEXED,    -- 'issue' | 'comment' | 'source_version' | 'source_entry'
    doc_id   UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '3 4 5'
);

CREATE TABLE saved_filter (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE,
    query           TEXT    NOT NULL,          -- OQL text
    columns_json    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(columns_json)),
    is_favourite    INTEGER NOT NULL DEFAULT 0 CHECK (is_favourite IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

------------------------------------------------------------------------------
-- 8. Governance (GOV-1 .. GOV-9)
------------------------------------------------------------------------------

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor_type      TEXT    NOT NULL CHECK (actor_type IN ('user', 'sync', 'ai', 'system')),
    action          TEXT    NOT NULL,          -- 'source.fetch', 'model.call', 'proposal.accept', 'jira.comment', ...
    action_class    TEXT    NOT NULL CHECK (action_class IN ('A', 'B', 'C', 'D', 'E')),
    target          TEXT,                      -- issue key, source locator, recipient
    detail_json     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    approval_id     INTEGER REFERENCES approval(id)
);
CREATE TRIGGER tr_audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only (GOV-7)'); END;
CREATE TRIGGER tr_audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only (GOV-7)'); END;

-- [P5] Single-use approval tokens for class C-E actions (GOV-5, GOV-6).
CREATE TABLE approval (
    id              INTEGER PRIMARY KEY,
    token_hash      TEXT    NOT NULL UNIQUE,
    action_class    TEXT    NOT NULL CHECK (action_class IN ('C', 'D', 'E')),
    target          TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    payload_hash    TEXT    NOT NULL,          -- binds the token to exactly the content that was shown
    issued_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at      TEXT    NOT NULL,
    used_at         TEXT
);

------------------------------------------------------------------------------
-- 9. AI layer [P3] (AI-1 .. AI-20)
------------------------------------------------------------------------------

CREATE TABLE model_call (
    id              INTEGER PRIMARY KEY,
    purpose         TEXT    NOT NULL,   -- 'source_summary', 'issue_summary', 'extraction', 'link_suggestion', 'import_scan', 'briefing'
    provider        TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    prompt_version  TEXT    NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    duration_ms     INTEGER,
    status          TEXT    NOT NULL CHECK (status IN ('ok', 'error', 'rejected_output')),
    error           TEXT
);

CREATE TABLE ai_summary (
    id              INTEGER PRIMARY KEY,
    scope_type      TEXT    NOT NULL CHECK (scope_type IN ('source_object', 'issue')),
    scope_id        INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    basis_json      TEXT    NOT NULL CHECK (json_valid(basis_json)),  -- highest version/entry ids it covers
    model_call_id   INTEGER NOT NULL REFERENCES model_call(id),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    superseded_at   TEXT
);
CREATE UNIQUE INDEX ux_ai_summary_current ON ai_summary(scope_type, scope_id) WHERE superseded_at IS NULL;

CREATE TABLE extracted_item (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT    NOT NULL CHECK (kind IN ('action_item', 'open_question', 'decision', 'risk')),
    issue_id            INTEGER REFERENCES issue(id),             -- NULL for unlinked mail (AST-6)
    source_object_id    INTEGER NOT NULL REFERENCES source_object(id),
    text                TEXT    NOT NULL,
    owner_id            INTEGER REFERENCES person(id),            -- who has to act / answer
    directed_at_me      INTEGER NOT NULL DEFAULT 0 CHECK (directed_at_me IN (0, 1)),
    due_date            TEXT,
    state               TEXT    NOT NULL DEFAULT 'open'
                                CHECK (state IN ('open', 'resolved', 'dismissed', 'superseded')),
    resolved_by_entry_id INTEGER REFERENCES source_entry(id),     -- e.g. my reply that answered it
    snoozed_until       TEXT,                                     -- [P4] AST-10
    model_call_id       INTEGER REFERENCES model_call(id),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX ix_extracted_item_me ON extracted_item(directed_at_me, state);

-- Everything the AI wants to change goes through here and needs a per-item decision (AI-4, GOV-4).
CREATE TABLE proposal (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT    NOT NULL CHECK (kind IN (
                            'new_issue', 'link_source', 'field_change', 'comment', 'external_action')),
    issue_id            INTEGER REFERENCES issue(id),           -- NULL for 'new_issue'
    source_object_id    INTEGER REFERENCES source_object(id),
    payload_json        TEXT    NOT NULL CHECK (json_valid(payload_json)),
    rationale           TEXT    NOT NULL,
    confidence          REAL    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    action_class        TEXT    NOT NULL DEFAULT 'B' CHECK (action_class IN ('B', 'C', 'D', 'E')),
    state               TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (state IN ('pending', 'accepted', 'accepted_edited', 'rejected', 'obsolete')),
    dedup_key           TEXT    NOT NULL,          -- AI-9: a rejected proposal is never raised again
    model_call_id       INTEGER REFERENCES model_call(id),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at          TEXT,
    decision_note       TEXT,
    UNIQUE (kind, dedup_key)
);
CREATE INDEX ix_proposal_pending ON proposal(state) WHERE state = 'pending';

-- AI-6: every AI output points at the exact content it was derived from.
CREATE TABLE evidence (
    id                  INTEGER PRIMARY KEY,
    owner_type          TEXT    NOT NULL CHECK (owner_type IN ('ai_summary', 'extracted_item', 'proposal')),
    owner_id            INTEGER NOT NULL,
    source_version_id   INTEGER REFERENCES source_version(id),
    source_entry_id     INTEGER REFERENCES source_entry(id),
    quote               TEXT,
    CHECK ((source_version_id IS NULL) <> (source_entry_id IS NULL))
);
CREATE INDEX ix_evidence_owner ON evidence(owner_type, owner_id);

------------------------------------------------------------------------------
-- 10. Assistant [P4] (AST-1 .. AST-10)
------------------------------------------------------------------------------

CREATE TABLE briefing (
    id              INTEGER PRIMARY KEY,
    for_date        TEXT    NOT NULL UNIQUE,
    generated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    content_md      TEXT    NOT NULL,
    items_json      TEXT    NOT NULL CHECK (json_valid(items_json)),   -- issue keys / extracted items referenced
    model_call_id   INTEGER REFERENCES model_call(id)
);

------------------------------------------------------------------------------
-- 11. Schema bookkeeping
------------------------------------------------------------------------------

CREATE TABLE schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description     TEXT    NOT NULL
);
INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema — spec v0.1');
