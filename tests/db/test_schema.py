import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_oil.db.migrate import current_schema_version, upgrade

EXPECTED_TABLES = {
    "person", "person_identity", "project", "component", "label", "issue", "issue_component",
    "issue_label", "issue_link", "comment", "change_set", "change_event", "connector",
    "source_object", "blob", "source_version", "source_entry", "issue_source_link", "sync_run",
    "sync_result", "search_doc", "saved_filter", "audit_log", "approval", "model_call", "ai_summary",
    "extracted_item", "proposal", "evidence", "briefing", "schema_version", "alembic_version",
}  # fmt: skip

SEED = [
    "INSERT INTO person(display_name, is_me) VALUES ('Alex', 1)",
    "INSERT INTO person(display_name, organisation) VALUES ('Supplier PM', 'ACME')",
    "INSERT INTO person_identity(person_id, system, external_id) VALUES (2, 'email', 'pm@acme.com')",
    "INSERT INTO project(key, name, next_number) VALUES ('OIL', 'Open issues', 2)",
    """INSERT INTO issue(project_id, number, key, summary, description, reporter_id,
                         original_due_date, revised_due_date, origin_type)
       VALUES (1, 1, 'OIL-1', 'Clarify inductor lead time', 'Supplier quoted 26 weeks', 1,
               '2026-11-15', '2026-11-15', 'email')""",
    "INSERT INTO issue(project_id, number, key, summary, description, reporter_id)"
    " VALUES (1, 2, 'OIL-2', 'Second issue', 'Another one', 1)",
    "INSERT INTO connector(type, name) VALUES ('outlook', 'Outlook (work)')",
    "INSERT INTO source_object(connector_id, object_type, external_id, title)"
    " VALUES (1, 'email_thread', 'CONV123', 'Inductor lead time')",
    "INSERT INTO source_entry(source_object_id, entry_type, external_id, author_identity_id, direction,"
    " occurred_at, text, content_hash)"
    " VALUES (1, 'email_message', '<m1@acme>', 1, 'in', '2026-10-20T08:00:00Z', 'Can you confirm?', 'h1')",
    "INSERT INTO issue_source_link(issue_id, source_object_id, role, link_method)"
    " VALUES (1, 1, 'origin', 'manual')",
    "INSERT INTO change_set(actor_type, actor_id) VALUES ('user', 1)",
    "INSERT INTO change_event(change_set_id, issue_id, field, op, old_value, new_value)"
    " VALUES (1, 1, 'status', 'set', 'New', 'In Progress')",
]  # fmt: skip


@pytest.fixture
def conn(engine: sqlalchemy.Engine):
    with engine.connect() as connection:
        for statement in SEED:
            connection.execute(text(statement))
        connection.commit()
        yield connection


def test_upgrade_creates_all_tables(engine: sqlalchemy.Engine) -> None:
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
            if not row[0].startswith("sqlite_") and not row[0].startswith("search_doc_")
        }
    assert EXPECTED_TABLES <= names


def test_connection_pragmas(engine: sqlalchemy.Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_second_upgrade_is_noop(engine: sqlalchemy.Engine) -> None:
    upgrade(engine)
    with engine.connect() as connection:
        assert current_schema_version(connection) == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["0001"]


def test_current_schema_version_without_table(tmp_path) -> None:
    from ai_oil.db.engine import create_db_engine

    bare = create_db_engine(tmp_path / "bare.db")
    with bare.connect() as connection:
        assert current_schema_version(connection) == 0
    bare.dispose()


REJECTED = [
    pytest.param(
        "UPDATE issue SET original_due_date = '2026-12-01' WHERE id = 1",
        "original_due_date is immutable",
        id="original-due-change",
    ),
    pytest.param("UPDATE issue SET key = 'OIL-9' WHERE id = 1", "immutable", id="key-change"),
    pytest.param(
        "UPDATE issue SET status = 'Blocked' WHERE id = 1", "CHECK constraint failed", id="blocked-no-reason"
    ),
    pytest.param(
        "UPDATE issue SET status = 'Cancelled' WHERE id = 1",
        "CHECK constraint failed",
        id="cancelled-no-reason",
    ),
    pytest.param(
        "UPDATE issue SET status = 'Cancelled', cancel_reason = '   ' WHERE id = 1",
        "CHECK constraint failed",
        id="cancelled-blank-reason",
    ),
    pytest.param(
        "UPDATE issue SET status = 'Waiting' WHERE id = 1", "CHECK constraint failed", id="bad-status"
    ),
    pytest.param(
        "UPDATE issue SET priority = 'High' WHERE id = 1", "CHECK constraint failed", id="bad-priority"
    ),
    pytest.param("UPDATE change_event SET new_value = 'x'", "append-only", id="history-update"),
    pytest.param("DELETE FROM change_set", "append-only", id="history-delete"),
    pytest.param(
        "INSERT INTO issue_source_link(issue_id, source_object_id, link_method) VALUES (1, 1, 'manual')",
        "UNIQUE constraint failed",
        id="duplicate-active-link",
    ),
    pytest.param(
        "INSERT INTO issue_source_link(issue_id, source_object_id, link_method)"
        " VALUES (2, 1, 'ai_suggestion')",
        "CHECK constraint failed",
        id="ai-link-without-proposal",
    ),
    pytest.param(
        "INSERT INTO person(display_name, is_me) VALUES ('X', 1)",
        "UNIQUE constraint failed",
        id="second-me",
    ),
    pytest.param(
        "INSERT INTO project(key, name) VALUES ('oil', 'x')", "CHECK constraint failed", id="bad-project-key"
    ),
    pytest.param(
        "INSERT INTO issue(project_id, number, key, summary, description, reporter_id)"
        " VALUES (1, 99, 'OIL-99', '" + "x" * 121 + "', 'd', 1)",
        "CHECK constraint failed",
        id="summary-too-long",
    ),
    pytest.param(
        "INSERT INTO issue_link(link_type, from_issue_id, to_issue_id) VALUES ('relates_to', 2, 1)",
        "CHECK constraint failed",
        id="relates-to-reversed",
    ),
    pytest.param(
        "INSERT INTO issue(project_id, number, key, summary, description, reporter_id)"
        " VALUES (1, 98, 'OIL-98', 'FK', 'd', 999)",
        "FOREIGN KEY constraint failed",
        id="foreign-key-violation",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("statement", "message"), REJECTED)
def test_constraint_rejects(conn: sqlalchemy.Connection, statement: str, message: str) -> None:
    with pytest.raises(IntegrityError, match=message):
        conn.execute(text(statement))


def test_cancelled_requires_reason_dm9(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        conn.execute(text("UPDATE issue SET status = 'Cancelled' WHERE id = 1"))


def test_done_needs_no_extra_fields(conn: sqlalchemy.Connection) -> None:
    conn.execute(text("UPDATE issue SET status = 'Done' WHERE id = 1"))
    assert conn.execute(text("SELECT status FROM issue WHERE id = 1")).scalar() == "Done"


def test_cancelled_with_reason_accepted(conn: sqlalchemy.Connection) -> None:
    conn.execute(text("UPDATE issue SET status = 'Cancelled', cancel_reason = 'Moved to OIL-2' WHERE id = 1"))
    assert conn.execute(text("SELECT cancel_reason FROM issue WHERE id = 1")).scalar() == "Moved to OIL-2"


def test_blocked_with_reason_accepted(conn: sqlalchemy.Connection) -> None:
    conn.execute(
        text(
            "UPDATE issue SET status = 'Blocked', blocked_reason = 'Waiting for supplier quote' WHERE id = 1"
        )
    )
    assert conn.execute(text("SELECT status FROM issue WHERE id = 1")).scalar() == "Blocked"


def test_source_version_revert_stays_visible_dm48(conn: sqlalchemy.Connection) -> None:
    for version_no, content_hash in ((1, "A"), (2, "B"), (3, "A")):
        conn.execute(
            text(
                "INSERT INTO source_version(source_object_id, version_no, content_hash, text)"
                " VALUES (1, :v, :h, 'x')"
            ),
            {"v": version_no, "h": content_hash},
        )
    hashes = conn.execute(text("SELECT content_hash FROM source_version ORDER BY version_no")).scalars().all()
    assert hashes == ["A", "B", "A"]


def test_fts_prefix_match(conn: sqlalchemy.Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO search_doc(doc_kind, doc_id, title, body)"
            " VALUES ('issue', 1, 'Clarify inductor lead time', 'Supplier quoted 26 weeks')"
        )
    )
    found = (
        conn.execute(text("SELECT doc_id FROM search_doc WHERE search_doc MATCH 'induct*'")).scalars().all()
    )
    assert [int(doc_id) for doc_id in found] == [1]
