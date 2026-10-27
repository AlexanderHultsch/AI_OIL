from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.domain import history, issues, people, projects
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.sources import linking, store
from ai_oil.sources.model import EntryData, ObjectMeta, VersionData

_START = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Frozen time; every call to `utcnow_iso` advances it by one minute."""
    holder = [_START]

    def fake_now() -> datetime:
        current = holder[0]
        holder[0] = current + timedelta(minutes=1)
        return current

    monkeypatch.setattr(timeutil, "_now", fake_now)
    return holder


@pytest.fixture
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex", is_me=True)


@pytest.fixture
def oil(conn: sqlalchemy.Connection, me: people.Person) -> projects.Project:
    return projects.create_project(conn, "OIL", "Open issues")


@pytest.fixture
def connector_id(conn: sqlalchemy.Connection) -> int:
    return conn.execute(
        sqlalchemy.text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
    ).scalar_one()


@pytest.fixture
def object_id(conn: sqlalchemy.Connection, connector_id: int) -> int:
    return store.upsert_object(conn, connector_id, ObjectMeta("jira_issue", "10001", display_ref="ABC-1"))


def make_issue(conn: sqlalchemy.Connection, me: people.Person) -> issues.Issue:
    return issues.create_issue(conn, "OIL", "Summary", "Description", actor_id=me.id)


def events(conn: sqlalchemy.Connection, key: str) -> list[tuple[str, str, str | None, str | None]]:
    issue = issues.get_issue(conn, key)
    return [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


def make_proposal(conn: sqlalchemy.Connection) -> int:
    return conn.execute(
        sqlalchemy.text(
            "INSERT INTO proposal (kind, payload_json, rationale, dedup_key)"
            " VALUES ('link_source', '{}', 'looks related', 'k1') RETURNING id"
        )
    ).scalar_one()


def test_constants() -> None:
    assert linking.ROLES == ("origin", "evidence", "reference")
    assert linking.LINK_METHODS == ("manual", "ai_suggestion")


def test_source_label_prefers_display_ref_then_title_then_external_id(
    conn: sqlalchemy.Connection, connector_id: int
) -> None:
    ids = [
        store.upsert_object(conn, connector_id, ObjectMeta("file", "a", title="T", display_ref="R")),
        store.upsert_object(conn, connector_id, ObjectMeta("file", "b", title="T")),
        store.upsert_object(conn, connector_id, ObjectMeta("file", "c")),
    ]
    labels = [linking.source_label(store.get_object(conn, object_id)) for object_id in ids]
    assert labels == ["file: R", "file: T", "file: c"]


# --- link / unlink -------------------------------------------------------------------------------------


def test_link_returns_the_linked_source_and_records_history(
    conn: sqlalchemy.Connection,
    me: people.Person,
    oil: projects.Project,
    object_id: int,
    clock: list[datetime],
) -> None:
    issue = make_issue(conn, me)
    linked = linking.link_source(conn, issue.key, object_id, actor_id=me.id, note="only L301")

    assert (linked.issue_key, linked.source_object_id) == (issue.key, object_id)
    assert (linked.role, linked.link_method, linked.proposal_id) == ("reference", "manual", None)
    assert linked.note == "only L301"
    assert linked.removed_at is None
    assert (linked.connector_name, linked.connector_type) == ("Fake", "fake")
    assert (linked.object_type, linked.external_id, linked.display_ref) == ("jira_issue", "10001", "ABC-1")
    assert (linked.fetch_status, linked.version_count, linked.entry_count, linked.child_count) == (
        "pending",
        0,
        0,
        0,
    )
    assert events(conn, issue.key)[-1] == ("source_link", "add", None, "jira_issue: ABC-1")

    after = issues.get_issue(conn, issue.key)
    assert after.updated_at == linked.created_at
    assert after.updated_at > issue.updated_at


def test_history_change_set_carries_actor_and_proposal(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    proposal_id = make_proposal(conn)
    linking.link_source(
        conn,
        issue.key,
        object_id,
        actor_id=None,
        actor_type="system",
        link_method="ai_suggestion",
        proposal_id=proposal_id,
    )
    entry = history.issue_history(conn, issue.id)[-1]
    assert (entry.actor_type, entry.actor_id, entry.proposal_id) == ("system", None, proposal_id)


def test_unlink_keeps_the_row_records_history_and_touches(
    conn: sqlalchemy.Connection,
    me: people.Person,
    oil: projects.Project,
    object_id: int,
    clock: list[datetime],
) -> None:
    issue = make_issue(conn, me)
    linked = linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)

    assert events(conn, issue.key)[-1] == ("source_link", "remove", "jira_issue: ABC-1", None)
    assert linking.list_issue_sources(conn, issue.key) == []
    (removed,) = linking.list_issue_sources(conn, issue.key, include_removed=True)
    assert removed.link_id == linked.link_id
    assert removed.removed_at is not None
    assert issues.get_issue(conn, issue.key).updated_at == removed.removed_at
    assert removed.removed_at > linked.created_at


def test_unlink_without_active_link_is_not_found(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    with pytest.raises(NotFoundError):
        linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)
    linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)
    with pytest.raises(NotFoundError):
        linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)


def test_same_object_can_belong_to_two_issues(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    first, second = make_issue(conn, me), make_issue(conn, me)
    linking.link_source(conn, first.key, object_id, actor_id=me.id)
    linking.link_source(conn, second.key, object_id, actor_id=me.id)
    assert linking.linked_object_ids(conn, first.key) == [object_id]
    assert linking.linked_object_ids(conn, second.key) == [object_id]
    assert linking.issues_for_source(conn, object_id) == [first.key, second.key]


def test_duplicate_active_link_is_a_conflict(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    before = len(history.issue_history(conn, issue.id))
    with pytest.raises(ConflictError):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id, role="evidence")
    assert len(history.issue_history(conn, issue.id)) == before


def test_relink_after_unlink_keeps_the_old_row(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    first = linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)
    second = linking.link_source(conn, issue.key, object_id, actor_id=me.id, role="evidence")

    assert second.link_id != first.link_id
    (active,) = linking.list_issue_sources(conn, issue.key)
    assert active.link_id == second.link_id
    both = linking.list_issue_sources(conn, issue.key, include_removed=True)
    assert [link.link_id for link in both] == [first.link_id, second.link_id]
    assert [link.removed_at is None for link in both] == [False, True]
    assert [e[1] for e in events(conn, issue.key) if e[0] == "source_link"] == ["add", "remove", "add"]


def test_second_origin_is_a_conflict(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, connector_id: int, object_id: int
) -> None:
    issue = make_issue(conn, me)
    other = store.upsert_object(conn, connector_id, ObjectMeta("note", "n1", title="Note"))
    linking.link_source(conn, issue.key, object_id, actor_id=me.id, role="origin")
    with pytest.raises(ConflictError):
        linking.link_source(conn, issue.key, other, actor_id=me.id, role="origin")
    linking.link_source(conn, issue.key, other, actor_id=me.id, role="evidence")
    linking.unlink_source(conn, issue.key, object_id, actor_id=me.id)
    third = store.upsert_object(conn, connector_id, ObjectMeta("note", "n2", title="Third"))
    assert linking.link_source(conn, issue.key, third, actor_id=me.id, role="origin").role == "origin"


def test_unknown_role_and_link_method_are_rejected(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    with pytest.raises(ValidationError):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id, role="boss")
    with pytest.raises(ValidationError):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id, link_method="telepathy")


def test_ai_suggestion_requires_a_proposal(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    with pytest.raises(ValidationError):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id, link_method="ai_suggestion")
    proposal_id = make_proposal(conn)
    linked = linking.link_source(
        conn, issue.key, object_id, actor_id=me.id, link_method="ai_suggestion", proposal_id=proposal_id
    )
    assert (linked.link_method, linked.proposal_id) == ("ai_suggestion", proposal_id)


def test_child_object_cannot_be_linked(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, connector_id: int
) -> None:
    issue = make_issue(conn, me)
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t", title="Thread subject"))
    child = store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"), parent_id=parent)
    with pytest.raises(ValidationError, match=r"email_thread: Thread subject.*link its parent instead"):
        linking.link_source(conn, issue.key, child, actor_id=me.id)
    assert linking.list_issue_sources(conn, issue.key, include_removed=True) == []


def test_unknown_object_and_issue_are_not_found(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    issue = make_issue(conn, me)
    with pytest.raises(NotFoundError):
        linking.link_source(conn, issue.key, 999, actor_id=me.id)
    with pytest.raises(NotFoundError):
        linking.link_source(conn, "OIL-999", object_id, actor_id=me.id)
    with pytest.raises(NotFoundError):
        linking.unlink_source(conn, "OIL-999", object_id, actor_id=me.id)
    with pytest.raises(NotFoundError):
        linking.list_issue_sources(conn, "OIL-999")
    with pytest.raises(NotFoundError):
        linking.linked_object_ids(conn, "OIL-999")
    with pytest.raises(NotFoundError):
        linking.issues_for_source(conn, 999)


# --- listing -------------------------------------------------------------------------------------------


def test_list_counts_versions_distinct_entries_and_children(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, connector_id: int
) -> None:
    issue = make_issue(conn, me)
    thread = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t", display_ref="Subject"))
    store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"), parent_id=thread)
    store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#2"), parent_id=thread)
    for body in ("v1", "v2", "v3"):
        store.write_version(conn, thread, VersionData(body))
    store.write_entry(conn, thread, EntryData("email_message", "m1", "2026-01-01T10:00:00Z", "first"))
    store.write_entry(conn, thread, EntryData("email_message", "m1", "2026-01-01T10:00:00Z", "edited"))
    store.write_entry(conn, thread, EntryData("email_message", "m2", "2026-01-01T11:00:00Z", "second"))
    store.write_entry(
        conn, thread, EntryData("note_text", "m1", "2026-01-01T12:00:00Z", "same id, other type")
    )
    linking.link_source(conn, issue.key, thread, actor_id=me.id)

    (linked,) = linking.list_issue_sources(conn, issue.key)
    assert (linked.version_count, linked.entry_count, linked.child_count) == (3, 3, 2)


def test_list_is_ordered_by_connector_type_then_created_at_then_id(
    conn: sqlalchemy.Connection,
    me: people.Person,
    oil: projects.Project,
    connector_id: int,
    clock: list[datetime],
) -> None:
    issue = make_issue(conn, me)
    jira = conn.execute(
        sqlalchemy.text("INSERT INTO connector (type, name) VALUES ('jira', 'Jira') RETURNING id")
    ).scalar_one()
    first = store.upsert_object(conn, connector_id, ObjectMeta("note", "1"))
    second = store.upsert_object(conn, jira, ObjectMeta("jira_issue", "1"))
    third = store.upsert_object(conn, connector_id, ObjectMeta("note", "2"))
    for object_id in (first, second, third):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    ordered = [link.source_object_id for link in linking.list_issue_sources(conn, issue.key)]
    assert ordered == [first, third, second]


def test_list_only_returns_the_given_issue(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    first, second = make_issue(conn, me), make_issue(conn, me)
    linking.link_source(conn, first.key, object_id, actor_id=me.id)
    assert linking.list_issue_sources(conn, second.key) == []


def test_linked_object_ids_are_active_only_and_ascending(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, connector_id: int
) -> None:
    issue = make_issue(conn, me)
    ids = [store.upsert_object(conn, connector_id, ObjectMeta("note", str(n))) for n in range(3)]
    for object_id in (ids[2], ids[0], ids[1]):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    linking.unlink_source(conn, issue.key, ids[1], actor_id=me.id)
    assert linking.linked_object_ids(conn, issue.key) == [ids[0], ids[2]]


# --- issues_for_source ---------------------------------------------------------------------------------


def test_issues_for_source_covers_children_through_the_parent(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, connector_id: int
) -> None:
    other_project = projects.create_project(conn, "ABC", "Another")
    first = make_issue(conn, me)
    second = make_issue(conn, me)
    third = issues.create_issue(conn, "ABC", "Other", "Description", actor_id=me.id)
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    child = store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"), parent_id=parent)
    for key in (third.key, second.key, first.key):
        linking.link_source(conn, key, parent, actor_id=me.id)
    linking.unlink_source(conn, second.key, parent, actor_id=me.id)

    expected = [third.key, first.key]
    assert other_project.key == "ABC"
    assert linking.issues_for_source(conn, parent) == expected
    assert linking.issues_for_source(conn, child) == expected


def test_issues_for_source_orders_by_number_not_text(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    created = [make_issue(conn, me) for _ in range(10)]
    for issue in reversed(created):
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)
    assert linking.issues_for_source(conn, object_id) == [issue.key for issue in created]


def test_issues_for_unlinked_source_is_empty(conn: sqlalchemy.Connection, object_id: int) -> None:
    assert linking.issues_for_source(conn, object_id) == []


# --- all_linked_object_ids -----------------------------------------------------------------------------


def test_all_linked_object_ids_filters_by_status_and_resolution(
    conn: sqlalchemy.Connection,
    me: people.Person,
    oil: projects.Project,
    connector_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = {
        name: store.upsert_object(conn, connector_id, ObjectMeta("note", name))
        for name in ("new", "progress", "done_old", "done_new", "cancelled", "unlinked", "shared")
    }
    keys = {
        name: make_issue(conn, me).key for name in ("new", "progress", "done_old", "done_new", "cancelled")
    }

    monkeypatch.setattr(timeutil, "_now", lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC))
    issues.transition_issue(conn, keys["done_old"], "Done", actor_id=me.id)
    monkeypatch.setattr(timeutil, "_now", lambda: datetime(2026, 10, 20, 8, 0, tzinfo=UTC))
    issues.transition_issue(conn, keys["done_new"], "Done", actor_id=me.id)
    issues.transition_issue(conn, keys["cancelled"], "Cancelled", actor_id=me.id, reason="obsolete")
    issues.transition_issue(conn, keys["progress"], "In Progress", actor_id=me.id)
    for name, key in keys.items():
        linking.link_source(conn, key, objects[name], actor_id=me.id)
    linking.link_source(conn, keys["done_old"], objects["shared"], actor_id=me.id)
    linking.link_source(conn, keys["new"], objects["shared"], actor_id=me.id)

    def ids(*names: str) -> list[int]:
        return sorted(objects[name] for name in names)

    assert linking.all_linked_object_ids(conn) == ids(
        "new", "progress", "done_old", "done_new", "cancelled", "shared"
    )
    open_statuses = ("New", "Ready to Proceed", "In Progress", "Blocked")
    assert linking.all_linked_object_ids(conn, statuses=open_statuses) == ids("new", "progress", "shared")
    assert linking.all_linked_object_ids(
        conn, statuses=open_statuses, resolved_after="2026-10-01T00:00:00.000Z"
    ) == ids("new", "progress", "done_new", "cancelled", "shared")
    assert linking.all_linked_object_ids(conn, statuses=("Done",)) == ids("done_old", "done_new", "shared")
    assert linking.all_linked_object_ids(conn, statuses=()) == []
    assert linking.all_linked_object_ids(conn, statuses=(), resolved_after="2026-10-01T00:00:00.000Z") == ids(
        "done_new", "cancelled"
    )

    linking.unlink_source(conn, keys["new"], objects["new"], actor_id=me.id)
    assert objects["new"] not in linking.all_linked_object_ids(conn, statuses=open_statuses)
    assert objects["shared"] in linking.all_linked_object_ids(conn, statuses=open_statuses)
    assert objects["unlinked"] not in linking.all_linked_object_ids(conn)


def test_all_linked_object_ids_returns_each_object_once(
    conn: sqlalchemy.Connection, me: people.Person, oil: projects.Project, object_id: int
) -> None:
    for _ in range(3):
        linking.link_source(conn, make_issue(conn, me).key, object_id, actor_id=me.id)
    assert linking.all_linked_object_ids(conn) == [object_id]
    assert linking.all_linked_object_ids(conn, statuses=("New",)) == [object_id]
