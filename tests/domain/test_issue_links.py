from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.domain import history, issue_links, issues, people, projects
from ai_oil.errors import ConflictError, NotFoundError, ValidationError

T0 = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex", is_me=True)


@pytest.fixture
def oil(conn: sqlalchemy.Connection, me: people.Person) -> projects.Project:
    return projects.create_project(conn, "OIL", "Open issues")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    state = {"now": T0}
    monkeypatch.setattr(timeutil, "_now", lambda: state["now"])

    def advance(**delta: int) -> None:
        state["now"] = state["now"] + timedelta(**delta)

    return advance


def make(conn: sqlalchemy.Connection, me: people.Person, summary: str = "Summary") -> issues.Issue:
    return issues.create_issue(conn, "OIL", summary, "Description", actor_id=me.id)


def events(conn: sqlalchemy.Connection, key: str) -> list[tuple[str, str, str | None, str | None]]:
    issue = issues.get_issue(conn, key)
    return [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


def change_set_count(conn: sqlalchemy.Connection) -> int:
    return conn.execute(text("SELECT count(*) FROM change_set")).scalar_one()


def link_count(conn: sqlalchemy.Connection) -> int:
    return conn.execute(text("SELECT count(*) FROM issue_link")).scalar_one()


def test_link_types_constant() -> None:
    assert issue_links.LINK_TYPES == ("relates_to", "blocks", "duplicates")


# --- link ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link_type", "outward_label", "inward_label"),
    [
        ("blocks", "blocks", "is blocked by"),
        ("duplicates", "duplicates", "is duplicated by"),
    ],
)
def test_directional_link_views_from_both_sides(
    conn: sqlalchemy.Connection, me, oil, link_type: str, outward_label: str, inward_label: str
) -> None:
    first = make(conn, me, "First")
    second = make(conn, me, "Second")
    link_id = issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    assert issue_links.list_issue_links(conn, first.key) == [
        issue_links.IssueLinkView(link_id, link_type, "outward", outward_label, second.key, "Second", "New")
    ]
    assert issue_links.list_issue_links(conn, second.key) == [
        issue_links.IssueLinkView(link_id, link_type, "inward", inward_label, first.key, "First", "New")
    ]


def test_relates_to_views_use_the_stored_order_for_direction(conn: sqlalchemy.Connection, me, oil) -> None:
    first = make(conn, me, "First")
    second = make(conn, me, "Second")
    link_id = issue_links.link_issues(conn, second.key, "relates_to", first.key, actor_id=me.id)
    assert issue_links.list_issue_links(conn, first.key) == [
        issue_links.IssueLinkView(link_id, "relates_to", "outward", "relates to", second.key, "Second", "New")
    ]
    assert issue_links.list_issue_links(conn, second.key) == [
        issue_links.IssueLinkView(link_id, "relates_to", "inward", "relates to", first.key, "First", "New")
    ]


def test_relates_to_is_stored_with_the_lower_id_first_in_either_argument_order(
    conn: sqlalchemy.Connection, me, oil
) -> None:
    first, second, third = make(conn, me), make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "relates_to", second.key, actor_id=me.id)
    issue_links.link_issues(conn, third.key, "relates_to", first.key, actor_id=me.id)
    rows = conn.execute(text("SELECT from_issue_id, to_issue_id FROM issue_link ORDER BY id")).all()
    assert [tuple(row) for row in rows] == [(first.id, second.id), (first.id, third.id)]


def test_relates_to_duplicate_is_a_conflict_in_either_order(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "relates_to", second.key, actor_id=me.id)
    sets_before = change_set_count(conn)
    with pytest.raises(ConflictError):
        issue_links.link_issues(conn, first.key, "relates_to", second.key, actor_id=me.id)
    with pytest.raises(ConflictError):
        issue_links.link_issues(conn, second.key, "relates_to", first.key, actor_id=me.id)
    assert link_count(conn) == 1
    assert change_set_count(conn) == sets_before


@pytest.mark.parametrize("link_type", ["blocks", "duplicates"])
def test_directional_duplicate_is_a_conflict(conn: sqlalchemy.Connection, me, oil, link_type: str) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    with pytest.raises(ConflictError):
        issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    assert link_count(conn) == 1


@pytest.mark.parametrize("link_type", ["blocks", "duplicates"])
def test_reverse_directional_link_rejected(conn: sqlalchemy.Connection, me, oil, link_type: str) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    sets_before = change_set_count(conn)
    with pytest.raises(ValidationError, match="reverse"):
        issue_links.link_issues(conn, second.key, link_type, first.key, actor_id=me.id)
    assert link_count(conn) == 1
    assert change_set_count(conn) == sets_before


def test_different_types_between_the_same_issues_coexist(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    for link_type in issue_links.LINK_TYPES:
        issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    assert [v.link_type for v in issue_links.list_issue_links(conn, first.key)] == [
        "blocks",
        "duplicates",
        "relates_to",
    ]


def test_self_link_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    for link_type in issue_links.LINK_TYPES:
        with pytest.raises(ValidationError, match="itself"):
            issue_links.link_issues(conn, issue.key, link_type, issue.key.lower(), actor_id=me.id)
    assert link_count(conn) == 0


def test_unknown_link_type_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    with pytest.raises(ValidationError, match="link_type"):
        issue_links.link_issues(conn, first.key, "causes", second.key, actor_id=me.id)
    with pytest.raises(ValidationError, match="link_type"):
        issue_links.unlink_issues(conn, first.key, "causes", second.key, actor_id=me.id)
    assert link_count(conn) == 0


def test_unknown_issue_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(NotFoundError, match="OIL-99"):
        issue_links.link_issues(conn, issue.key, "blocks", "OIL-99", actor_id=me.id)
    with pytest.raises(NotFoundError, match="OIL-99"):
        issue_links.link_issues(conn, "OIL-99", "blocks", issue.key, actor_id=me.id)
    with pytest.raises(NotFoundError, match="OIL-99"):
        issue_links.unlink_issues(conn, issue.key, "blocks", "OIL-99", actor_id=me.id)
    with pytest.raises(NotFoundError, match="OIL-99"):
        issue_links.list_issue_links(conn, "OIL-99")
    assert link_count(conn) == 0
    assert change_set_count(conn) == 1


def test_keys_are_case_insensitive_and_links_cross_projects(conn: sqlalchemy.Connection, me, oil) -> None:
    projects.create_project(conn, "OTH", "Other")
    local = make(conn, me, "Local")
    foreign = issues.create_issue(conn, "OTH", "Foreign", "D", actor_id=me.id)
    issue_links.link_issues(conn, " oil-1 ", "blocks", "oth-1", actor_id=me.id)
    (view,) = issue_links.list_issue_links(conn, foreign.key)
    assert (view.other_key, view.label) == (local.key, "is blocked by")


def test_link_history_on_both_issues(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    assert events(conn, first.key)[1:] == [("issue_link", "add", None, "blocks OIL-2")]
    assert events(conn, second.key)[1:] == [("issue_link", "add", None, "is blocked by OIL-1")]
    entry = history.issue_history(conn, first.id)[1]
    assert (entry.actor_type, entry.actor_id) == ("user", me.id)
    assert change_set_count(conn) == 3


def test_duplicates_history_texts(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, second.key, "duplicates", first.key, actor_id=me.id)
    assert events(conn, second.key)[1:] == [("issue_link", "add", None, "duplicates OIL-1")]
    assert events(conn, first.key)[1:] == [("issue_link", "add", None, "is duplicated by OIL-2")]


def test_relates_to_history_texts_name_the_other_issue(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, second.key, "relates_to", first.key, actor_id=me.id)
    assert events(conn, first.key)[1:] == [("issue_link", "add", None, "relates to OIL-2")]
    assert events(conn, second.key)[1:] == [("issue_link", "add", None, "relates to OIL-1")]


def test_link_touches_both_issues(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    first, second, bystander = make(conn, me), make(conn, me), make(conn, me)
    clock(hours=1)
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    assert issues.get_issue(conn, first.key).updated_at == "2026-10-27T09:00:00.000Z"
    assert issues.get_issue(conn, second.key).updated_at == "2026-10-27T09:00:00.000Z"
    assert issues.get_issue(conn, bystander.key).updated_at == bystander.updated_at


def test_link_returns_the_id_and_stamps_created_at(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    first, second = make(conn, me), make(conn, me)
    clock(minutes=7)
    link_id = issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    row = conn.execute(text("SELECT id, created_at FROM issue_link")).one()
    assert (row.id, row.created_at) == (link_id, "2026-10-27T08:07:00.000Z")


# --- unlink --------------------------------------------------------------------------------------


@pytest.mark.parametrize("link_type", issue_links.LINK_TYPES)
def test_unlink_deletes_the_row_and_records_both_sides(
    conn: sqlalchemy.Connection, me, oil, clock, link_type: str
) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    clock(hours=2)
    issue_links.unlink_issues(conn, first.key, link_type, second.key, actor_id=me.id)
    assert link_count(conn) == 0
    assert issue_links.list_issue_links(conn, first.key) == []
    assert issue_links.list_issue_links(conn, second.key) == []
    for key in (first.key, second.key):
        added, removed = events(conn, key)[1:]
        assert added[0] == removed[0] == "issue_link"
        assert (added[1], removed[1]) == ("add", "remove")
        assert removed[2] == added[3] and removed[3] is None
        assert issues.get_issue(conn, key).updated_at == "2026-10-27T10:00:00.000Z"
    assert change_set_count(conn) == 4


def test_unlink_history_texts_for_blocks(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    issue_links.unlink_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    assert events(conn, first.key)[-1] == ("issue_link", "remove", "blocks OIL-2", None)
    assert events(conn, second.key)[-1] == ("issue_link", "remove", "is blocked by OIL-1", None)


def test_unlink_relates_to_matches_either_order(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second, third = make(conn, me), make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "relates_to", second.key, actor_id=me.id)
    issue_links.link_issues(conn, first.key, "relates_to", third.key, actor_id=me.id)
    issue_links.unlink_issues(conn, first.key, "relates_to", second.key, actor_id=me.id)
    issue_links.unlink_issues(conn, third.key, "relates_to", first.key, actor_id=me.id)
    assert link_count(conn) == 0
    assert events(conn, second.key)[-1] == ("issue_link", "remove", "relates to OIL-1", None)
    assert events(conn, third.key)[-1] == ("issue_link", "remove", "relates to OIL-1", None)


def test_unlink_missing_link_is_not_found(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    with pytest.raises(NotFoundError):
        issue_links.unlink_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    with pytest.raises(NotFoundError):
        issue_links.unlink_issues(conn, second.key, "blocks", first.key, actor_id=me.id)
    with pytest.raises(NotFoundError):
        issue_links.unlink_issues(conn, first.key, "duplicates", second.key, actor_id=me.id)
    assert link_count(conn) == 1
    assert change_set_count(conn) == 3


def test_unlink_self_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match="itself"):
        issue_links.unlink_issues(conn, issue.key, "blocks", issue.key, actor_id=me.id)


def test_relink_after_unlink_is_allowed(conn: sqlalchemy.Connection, me, oil) -> None:
    first, second = make(conn, me), make(conn, me)
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    issue_links.unlink_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    issue_links.link_issues(conn, second.key, "blocks", first.key, actor_id=me.id)
    assert [v.label for v in issue_links.list_issue_links(conn, first.key)] == ["is blocked by"]


# --- list ----------------------------------------------------------------------------------------


def test_list_orders_by_type_then_other_number(conn: sqlalchemy.Connection, me, oil) -> None:
    center = make(conn, me, "Center")
    others = [make(conn, me, f"Other {n}") for n in range(2, 5)]
    issue_links.link_issues(conn, center.key, "relates_to", others[2].key, actor_id=me.id)
    issue_links.link_issues(conn, others[1].key, "blocks", center.key, actor_id=me.id)
    issue_links.link_issues(conn, center.key, "relates_to", others[0].key, actor_id=me.id)
    issue_links.link_issues(conn, center.key, "blocks", others[2].key, actor_id=me.id)
    views = issue_links.list_issue_links(conn, center.key)
    assert [(v.link_type, v.direction, v.other_key) for v in views] == [
        ("blocks", "inward", others[1].key),
        ("blocks", "outward", others[2].key),
        ("relates_to", "outward", others[0].key),
        ("relates_to", "outward", others[2].key),
    ]


def test_list_reports_current_summary_and_status_of_the_other_issue(
    conn: sqlalchemy.Connection, me, oil
) -> None:
    first, second = make(conn, me, "First"), make(conn, me, "Second")
    issue_links.link_issues(conn, first.key, "blocks", second.key, actor_id=me.id)
    issues.update_issue(conn, second.key, {"summary": "Renamed"}, actor_id=me.id)
    issues.transition_issue(conn, second.key, "In Progress", actor_id=me.id)
    (view,) = issue_links.list_issue_links(conn, first.key)
    assert (view.other_summary, view.other_status) == ("Renamed", "In Progress")


def test_list_of_unlinked_issue_is_empty(conn: sqlalchemy.Connection, me, oil) -> None:
    assert issue_links.list_issue_links(conn, make(conn, me).key) == []
