-- AI_OIL — SQLite schema v2: search index triggers
-- Search index maintained in the same transaction as the row it mirrors (SRCH-1, spec 5.15).
-- Conventions:
--   * every search_doc row has the explicit rowid kind_code * 1000000000000 + <id of the mirrored row>
--     with kind codes issue 1, comment 2, source_version 3, source_entry 4 (ai_oil.search.fts.KIND_CODES)
--   * doc_kind / doc_id carry the same information in readable form
--   * "replace" is DELETE + INSERT by rowid
--   * only the latest version per source object and the latest revision per entry are indexed
--   * the section between BEGIN BACKFILL and END BACKFILL is also executed by ai_oil.search.fts.reindex_all;
--     it must contain plain statements only (no triggers, no semicolons inside comments or strings)

------------------------------------------------------------------------------
-- 1. Issues: key + summary as title, description + acceptance criteria as body
------------------------------------------------------------------------------

CREATE TRIGGER tr_search_issue_insert
AFTER INSERT ON issue
BEGIN
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    VALUES (1000000000000 + NEW.id, 'issue', NEW.id, NEW.key || ' ' || NEW.summary,
            NEW.description || char(10) || coalesce(NEW.acceptance_criteria, ''));
END;

CREATE TRIGGER tr_search_issue_update
AFTER UPDATE OF key, summary, description, acceptance_criteria ON issue
BEGIN
    DELETE FROM search_doc WHERE rowid = 1000000000000 + NEW.id;
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    VALUES (1000000000000 + NEW.id, 'issue', NEW.id, NEW.key || ' ' || NEW.summary,
            NEW.description || char(10) || coalesce(NEW.acceptance_criteria, ''));
END;

------------------------------------------------------------------------------
-- 2. Comments: issue key as title, body as body; a soft-deleted comment leaves the index
------------------------------------------------------------------------------

CREATE TRIGGER tr_search_comment_insert
AFTER INSERT ON comment
WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 2000000000000 + NEW.id, 'comment', NEW.id, i.key, NEW.body
    FROM issue i WHERE i.id = NEW.issue_id;
END;

CREATE TRIGGER tr_search_comment_update
AFTER UPDATE OF body, deleted_at ON comment
BEGIN
    DELETE FROM search_doc WHERE rowid = 2000000000000 + NEW.id;
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 2000000000000 + NEW.id, 'comment', NEW.id, i.key, NEW.body
    FROM issue i WHERE i.id = NEW.issue_id AND NEW.deleted_at IS NULL;
END;

------------------------------------------------------------------------------
-- 3. Source versions: only the latest version of an object is indexed
------------------------------------------------------------------------------

CREATE TRIGGER tr_search_source_version_insert
AFTER INSERT ON source_version
BEGIN
    DELETE FROM search_doc WHERE rowid IN (
        SELECT 3000000000000 + v.id FROM source_version v
        WHERE v.source_object_id = NEW.source_object_id AND v.id <> NEW.id);
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 3000000000000 + NEW.id, 'source_version', NEW.id, coalesce(o.title, o.display_ref, ''), NEW.text
    FROM source_object o WHERE o.id = NEW.source_object_id;
END;

------------------------------------------------------------------------------
-- 4. Source entries: only the latest revision of an entry is indexed
------------------------------------------------------------------------------

CREATE TRIGGER tr_search_source_entry_insert
AFTER INSERT ON source_entry
BEGIN
    DELETE FROM search_doc WHERE rowid IN (
        SELECT 4000000000000 + e.id FROM source_entry e
        WHERE e.source_object_id = NEW.source_object_id AND e.entry_type = NEW.entry_type
          AND e.external_id = NEW.external_id AND e.id <> NEW.id);
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 4000000000000 + NEW.id, 'source_entry', NEW.id, coalesce(o.title, o.display_ref, ''),
           coalesce(NEW.new_text, NEW.text)
    FROM source_object o WHERE o.id = NEW.source_object_id;
END;

------------------------------------------------------------------------------
-- 5. Source titles: a renamed object re-titles its indexed version and entry documents
------------------------------------------------------------------------------

CREATE TRIGGER tr_search_source_object_title
AFTER UPDATE OF title, display_ref ON source_object
WHEN coalesce(NEW.title, NEW.display_ref, '') IS NOT coalesce(OLD.title, OLD.display_ref, '')
BEGIN
    DELETE FROM search_doc WHERE rowid IN (
        SELECT 3000000000000 + v.id FROM source_version v WHERE v.source_object_id = NEW.id);
    DELETE FROM search_doc WHERE rowid IN (
        SELECT 4000000000000 + e.id FROM source_entry e WHERE e.source_object_id = NEW.id);
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 3000000000000 + v.id, 'source_version', v.id, coalesce(NEW.title, NEW.display_ref, ''), v.text
    FROM source_version v
    WHERE v.source_object_id = NEW.id
      AND v.version_no = (SELECT max(x.version_no) FROM source_version x WHERE x.source_object_id = NEW.id);
    INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
    SELECT 4000000000000 + e.id, 'source_entry', e.id, coalesce(NEW.title, NEW.display_ref, ''),
           coalesce(e.new_text, e.text)
    FROM source_entry e
    WHERE e.source_object_id = NEW.id
      AND e.revision = (SELECT max(x.revision) FROM source_entry x
                        WHERE x.source_object_id = e.source_object_id AND x.entry_type = e.entry_type
                          AND x.external_id = e.external_id);
END;

------------------------------------------------------------------------------
-- 6. Backfill of rows that exist already
------------------------------------------------------------------------------

-- BEGIN BACKFILL
INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
SELECT 1000000000000 + i.id, 'issue', i.id, i.key || ' ' || i.summary,
       i.description || char(10) || coalesce(i.acceptance_criteria, '')
FROM issue i;

INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
SELECT 2000000000000 + c.id, 'comment', c.id, i.key, c.body
FROM comment c JOIN issue i ON i.id = c.issue_id
WHERE c.deleted_at IS NULL;

INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
SELECT 3000000000000 + v.id, 'source_version', v.id, coalesce(o.title, o.display_ref, ''), v.text
FROM source_version v JOIN source_object o ON o.id = v.source_object_id
WHERE v.version_no = (SELECT max(x.version_no) FROM source_version x WHERE x.source_object_id = v.source_object_id);

INSERT INTO search_doc (rowid, doc_kind, doc_id, title, body)
SELECT 4000000000000 + e.id, 'source_entry', e.id, coalesce(o.title, o.display_ref, ''),
       coalesce(e.new_text, e.text)
FROM source_entry e JOIN source_object o ON o.id = e.source_object_id
WHERE e.revision = (SELECT max(x.revision) FROM source_entry x
                    WHERE x.source_object_id = e.source_object_id AND x.entry_type = e.entry_type
                      AND x.external_id = e.external_id);
-- END BACKFILL

INSERT INTO schema_version (version, description) VALUES (2, 'Search index triggers');
