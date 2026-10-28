from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.domain import comments, history, issues, people, projects
from ai_oil.errors import NotFoundError, ValidationError

T0 = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex", is_me=True)


@pytest.fixture
def issue(conn: sqlalchemy.Connection, me: people.Person) -> issues.Issue:
    projects.create_project(conn, "OIL", "Open issues")
    return issues.create_issue(conn, "OIL", "Summary", "Description", actor_id=me.id)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    state = {"now": T0}
    monkeypatch.setattr(timeutil, "_now", lambda: state["now"])

    def advance(**delta: int) -> None:
        state["now"] = state["now"] + timedelta(**delta)

    return advance


def events(
    conn: sqlalchemy.Connection, target: issues.Issue
) -> list[tuple[str, str, str | None, str | None]]:
    return [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, target.id)]


def change_set_count(conn: sqlalchemy.Connection) -> int:
    return conn.execute(text("SELECT count(*) FROM change_set")).scalar_one()


def updated_at(conn: sqlalchemy.Connection, target: issues.Issue) -> str:
    return issues.get_issue(conn, target.key).updated_at


# --- add -----------------------------------------------------------------------------------------


def test_add_comment(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    clock(hours=1)
    comment = comments.add_comment(conn, " oil-1 ", "  First note\n", author_id=me.id)
    assert comment == comments.Comment(
        id=comment.id,
        issue_id=issue.id,
        issue_key="OIL-1",
        author_id=me.id,
        body="First note",
        proposal_id=None,
        created_at="2026-10-27T09:00:00.000Z",
        edited_at=None,
        deleted_at=None,
    )
    assert comments.get_comment(conn, comment.id) == comment


def test_add_records_history_and_touches_the_issue(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    clock(hours=1)
    comment = comments.add_comment(conn, issue.key, "Note", author_id=me.id)
    assert events(conn, issue)[1:] == [("comment", "add", None, "Note")]
    entry = history.issue_history(conn, issue.id)[1]
    assert (entry.actor_type, entry.actor_id, entry.proposal_id) == ("user", me.id, None)
    assert entry.note == f"comment {comment.id}"
    assert updated_at(conn, issue) == "2026-10-27T09:00:00.000Z"


def test_add_stores_the_author_as_actor(conn: sqlalchemy.Connection, me, issue) -> None:
    sam = people.create_person(conn, "Sam")
    comments.add_comment(conn, issue.key, "From Sam", author_id=sam.id)
    entry = history.issue_history(conn, issue.id)[1]
    assert entry.actor_id == sam.id
    assert comments.list_comments(conn, issue.key)[0].author_id == sam.id


def test_add_redirects_a_merged_author_to_the_survivor(conn: sqlalchemy.Connection, me, issue) -> None:
    duplicate = people.create_person(conn, "Sam")
    survivor = people.create_person(conn, "Samuel Smith")
    people.merge_persons(conn, duplicate.id, survivor.id, actor_id=me.id)

    comment = comments.add_comment(conn, issue.key, "From Sam", author_id=duplicate.id)

    assert comment.author_id == survivor.id
    assert history.issue_history(conn, issue.id)[1].actor_id == survivor.id


def test_add_with_proposal_id(conn: sqlalchemy.Connection, me, issue) -> None:
    conn.execute(
        text(
            "INSERT INTO proposal (id, kind, payload_json, rationale, dedup_key)"
            " VALUES (5, 'comment', '{}', 'why', 'k')"
        )
    )
    comment = comments.add_comment(conn, issue.key, "AI note", author_id=me.id, proposal_id=5)
    assert comment.proposal_id == 5
    assert comments.get_comment(conn, comment.id).proposal_id == 5
    assert history.issue_history(conn, issue.id)[1].proposal_id == 5


@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_blank_body_rejected(conn: sqlalchemy.Connection, me, issue, body: str) -> None:
    with pytest.raises(ValidationError, match="body"):
        comments.add_comment(conn, issue.key, body, author_id=me.id)
    assert conn.execute(text("SELECT count(*) FROM comment")).scalar_one() == 0
    assert change_set_count(conn) == 1


def test_non_text_body_rejected(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(ValidationError, match="body"):
        comments.add_comment(conn, issue.key, None, author_id=me.id)  # type: ignore[arg-type]


def test_add_unknown_issue_or_author(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(NotFoundError, match="OIL-9"):
        comments.add_comment(conn, "OIL-9", "x", author_id=me.id)
    with pytest.raises(NotFoundError, match="person 999"):
        comments.add_comment(conn, issue.key, "x", author_id=999)
    assert conn.execute(text("SELECT count(*) FROM comment")).scalar_one() == 0
    assert change_set_count(conn) == 1


def test_get_unknown_comment(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(NotFoundError, match="comment 9"):
        comments.get_comment(conn, 9)


# --- edit ----------------------------------------------------------------------------------------


def test_edit_sets_body_edited_at_and_history(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    comment = comments.add_comment(conn, issue.key, "Old text", author_id=me.id)
    clock(hours=2)
    edited = comments.edit_comment(conn, comment.id, "  New text ", actor_id=me.id)
    assert (edited.body, edited.edited_at, edited.created_at) == (
        "New text",
        "2026-10-27T10:00:00.000Z",
        "2026-10-27T08:00:00.000Z",
    )
    assert comments.get_comment(conn, comment.id) == edited
    assert events(conn, issue)[1:] == [
        ("comment", "add", None, "Old text"),
        ("comment", "set", "Old text", "New text"),
    ]
    entry = history.issue_history(conn, issue.id)[2]
    assert (entry.actor_type, entry.actor_id, entry.note) == ("user", me.id, f"comment {comment.id}")
    assert updated_at(conn, issue) == "2026-10-27T10:00:00.000Z"


def test_edit_actor_may_differ_from_author(conn: sqlalchemy.Connection, me, issue) -> None:
    sam = people.create_person(conn, "Sam")
    comment = comments.add_comment(conn, issue.key, "Old", author_id=sam.id)
    edited = comments.edit_comment(conn, comment.id, "New", actor_id=me.id)
    assert edited.author_id == sam.id
    assert history.issue_history(conn, issue.id)[2].actor_id == me.id


def test_edit_with_unchanged_body_is_a_noop(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    comment = comments.add_comment(conn, issue.key, "Same", author_id=me.id)
    sets_before = change_set_count(conn)
    clock(hours=1)
    assert comments.edit_comment(conn, comment.id, "  Same\n", actor_id=me.id) == comment
    assert change_set_count(conn) == sets_before
    assert comments.get_comment(conn, comment.id).edited_at is None
    assert updated_at(conn, issue) == "2026-10-27T08:00:00.000Z"


def test_edit_blank_body_rejected(conn: sqlalchemy.Connection, me, issue) -> None:
    comment = comments.add_comment(conn, issue.key, "Keep", author_id=me.id)
    with pytest.raises(ValidationError, match="body"):
        comments.edit_comment(conn, comment.id, "  ", actor_id=me.id)
    assert comments.get_comment(conn, comment.id).body == "Keep"


def test_edit_unknown_comment(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(NotFoundError):
        comments.edit_comment(conn, 9, "x", actor_id=me.id)


def test_edit_after_delete_rejected(conn: sqlalchemy.Connection, me, issue) -> None:
    comment = comments.add_comment(conn, issue.key, "Gone", author_id=me.id)
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    sets_before = change_set_count(conn)
    with pytest.raises(ValidationError, match="deleted"):
        comments.edit_comment(conn, comment.id, "Back", actor_id=me.id)
    assert comments.get_comment(conn, comment.id).body == "Gone"
    assert change_set_count(conn) == sets_before


# --- delete --------------------------------------------------------------------------------------


def test_delete_is_soft_and_keeps_the_body(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    comment = comments.add_comment(conn, issue.key, "Note", author_id=me.id)
    clock(hours=3)
    assert comments.delete_comment(conn, comment.id, actor_id=me.id) is None
    deleted = comments.get_comment(conn, comment.id)
    assert (deleted.body, deleted.deleted_at, deleted.edited_at) == ("Note", "2026-10-27T11:00:00.000Z", None)
    assert conn.execute(text("SELECT count(*) FROM comment")).scalar_one() == 1
    assert events(conn, issue)[1:] == [("comment", "add", None, "Note"), ("comment", "delete", "Note", None)]
    entry = history.issue_history(conn, issue.id)[2]
    assert (entry.actor_type, entry.actor_id, entry.note) == ("user", me.id, f"comment {comment.id}")
    assert updated_at(conn, issue) == "2026-10-27T11:00:00.000Z"


def test_delete_after_edit_records_the_latest_text(conn: sqlalchemy.Connection, me, issue) -> None:
    comment = comments.add_comment(conn, issue.key, "v1", author_id=me.id)
    comments.edit_comment(conn, comment.id, "v2", actor_id=me.id)
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    assert events(conn, issue)[1:] == [
        ("comment", "add", None, "v1"),
        ("comment", "set", "v1", "v2"),
        ("comment", "delete", "v2", None),
    ]


def test_delete_twice_is_a_noop(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    comment = comments.add_comment(conn, issue.key, "Note", author_id=me.id)
    clock(hours=1)
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    first = comments.get_comment(conn, comment.id)
    sets_before = change_set_count(conn)
    clock(hours=1)
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    assert comments.get_comment(conn, comment.id) == first
    assert change_set_count(conn) == sets_before
    assert updated_at(conn, issue) == "2026-10-27T09:00:00.000Z"


def test_delete_unknown_comment(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(NotFoundError):
        comments.delete_comment(conn, 9, actor_id=me.id)


# --- list ----------------------------------------------------------------------------------------


def test_list_orders_by_created_at_then_id(conn: sqlalchemy.Connection, me, issue, clock) -> None:
    first = comments.add_comment(conn, issue.key, "first", author_id=me.id)
    second = comments.add_comment(conn, issue.key, "second", author_id=me.id)
    clock(minutes=1)
    third = comments.add_comment(conn, issue.key, "third", author_id=me.id)
    assert [c.id for c in comments.list_comments(conn, issue.key)] == [first.id, second.id, third.id]


def test_list_excludes_deleted_unless_asked(conn: sqlalchemy.Connection, me, issue) -> None:
    kept = comments.add_comment(conn, issue.key, "kept", author_id=me.id)
    removed = comments.add_comment(conn, issue.key, "removed", author_id=me.id)
    comments.delete_comment(conn, removed.id, actor_id=me.id)
    assert [c.id for c in comments.list_comments(conn, issue.key)] == [kept.id]
    with_deleted = comments.list_comments(conn, issue.key, include_deleted=True)
    assert [(c.id, c.body) for c in with_deleted] == [(kept.id, "kept"), (removed.id, "removed")]
    assert with_deleted[1].deleted_at is not None


def test_list_is_per_issue(conn: sqlalchemy.Connection, me, issue) -> None:
    other = issues.create_issue(conn, "OIL", "Other", "D", actor_id=me.id)
    comments.add_comment(conn, issue.key, "on first", author_id=me.id)
    on_other = comments.add_comment(conn, other.key, "on other", author_id=me.id)
    assert [c.id for c in comments.list_comments(conn, other.key)] == [on_other.id]
    assert on_other.issue_key == other.key
    assert comments.list_comments(conn, issue.key)[0].body == "on first"


def test_list_unknown_issue(conn: sqlalchemy.Connection, me, issue) -> None:
    with pytest.raises(NotFoundError):
        comments.list_comments(conn, "OIL-9")
    assert comments.list_comments(conn, issue.key) == []
