import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ai_oil.domain import history
from ai_oil.errors import ValidationError


@pytest.fixture
def issue_id(conn: sqlalchemy.Connection) -> int:
    conn.execute(text("INSERT INTO person(id, display_name, is_me) VALUES (1, 'Alex', 1)"))
    conn.execute(text("INSERT INTO project(id, key, name) VALUES (1, 'OIL', 'Open issues')"))
    conn.execute(
        text(
            "INSERT INTO issue(id, project_id, number, key, summary, description, reporter_id)"
            " VALUES (1, 1, 1, 'OIL-1', 'First', 'First issue', 1)"
        )
    )
    return 1


def test_round_trip(conn: sqlalchemy.Connection, issue_id: int) -> None:
    first = history.start_change_set(conn, "user", actor_id=1, note="edit")
    history.record(conn, first, issue_id, "status", "set", "New", "In Progress")
    history.record(conn, first, issue_id, "label", "add", None, "hw")
    second = history.start_change_set(conn, "sync", note="sync run")
    history.record(conn, second, issue_id, "assignee", "set", None, 1)

    entries = history.issue_history(conn, issue_id)

    assert [(e.change_set_id, e.field, e.op, e.old_value, e.new_value) for e in entries] == [
        (first, "status", "set", "New", "In Progress"),
        (first, "label", "add", None, "hw"),
        (second, "assignee", "set", None, "1"),
    ]
    assert entries[0].actor_type == "user"
    assert entries[0].actor_id == 1
    assert entries[0].note == "edit"
    assert entries[0].proposal_id is None
    assert entries[0].sync_run_id is None
    assert entries[0].at.endswith("Z")
    assert entries[2].actor_type == "sync"
    assert entries[2].actor_id is None


def test_history_of_other_issue_is_empty(conn: sqlalchemy.Connection, issue_id: int) -> None:
    change_set = history.start_change_set(conn, "system")
    history.record(conn, change_set, issue_id, "summary", "create", None, "First")
    assert history.issue_history(conn, issue_id + 1) == []


def test_invalid_actor_type_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        history.start_change_set(conn, "ai")


def test_invalid_op_rejected(conn: sqlalchemy.Connection, issue_id: int) -> None:
    change_set = history.start_change_set(conn, "user")
    with pytest.raises(ValidationError):
        history.record(conn, change_set, issue_id, "status", "update", "a", "b")


def test_values_stored_as_text_and_none_as_null(conn: sqlalchemy.Connection, issue_id: int) -> None:
    change_set = history.start_change_set(conn, "user")
    history.record(conn, change_set, issue_id, "assignee", "set", None, 42)
    row = conn.execute(
        text("SELECT old_value, new_value, typeof(old_value), typeof(new_value) FROM change_event")
    ).one()
    assert tuple(row) == (None, "42", "null", "text")


def test_change_event_update_rejected_by_trigger(conn: sqlalchemy.Connection, issue_id: int) -> None:
    change_set = history.start_change_set(conn, "user")
    history.record(conn, change_set, issue_id, "status", "set", "New", "Done")
    with pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text("UPDATE change_event SET new_value = 'x'"))
