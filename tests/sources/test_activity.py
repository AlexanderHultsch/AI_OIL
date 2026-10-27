from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.domain import comments, history, issues, people, projects
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.sources import activity, linking, store
from ai_oil.sources.model import ChildData, EntryData, FetchResult, IdentityRef, ObjectMeta, VersionData

_START = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)
BEA = IdentityRef("email", "bea@acme.com", "Bea Brandt")
CARL = IdentityRef("email", "carl@acme.com", "Carl Conrad")
ALEX = IdentityRef("email", "alex@example.com", "Alex")


@pytest.fixture(autouse=True)
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
    person = people.create_person(conn, "Alex", is_me=True)
    people.add_identity(conn, person.id, "email", "alex@example.com")
    return person


@pytest.fixture
def issue(conn: sqlalchemy.Connection, me: people.Person) -> issues.Issue:
    projects.create_project(conn, "OIL", "Open issues")
    return issues.create_issue(conn, "OIL", "Summary", "Description", actor_id=me.id)


@pytest.fixture
def connector_id(conn: sqlalchemy.Connection) -> int:
    return conn.execute(
        sqlalchemy.text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
    ).scalar_one()


def make_object(
    conn: sqlalchemy.Connection,
    connector_id: int,
    key: str,
    object_type: str,
    external_id: str,
    *,
    title: str | None = None,
    display_ref: str | None = None,
    link: bool = True,
) -> int:
    object_id = store.upsert_object(
        conn, connector_id, ObjectMeta(object_type, external_id, title=title, display_ref=display_ref)
    )
    if link:
        linking.link_source(conn, key, object_id, actor_id=people.get_me(conn).id)
    return object_id


def entry(
    entry_type: str, external_id: str, at: str, text: str, author: IdentityRef | None = BEA, **kwargs
) -> EntryData:
    return EntryData(entry_type, external_id, at, text, author=author, **kwargs)


def set_time(conn: sqlalchemy.Connection, table: str, column: str, row_id: int, value: str) -> None:
    conn.execute(
        sqlalchemy.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
        {"value": value, "id": row_id},
    )


def add_versions(conn: sqlalchemy.Connection, object_id: int, count: int) -> list[int]:
    ids = []
    for number in range(1, count + 1):
        version_id = store.write_version(conn, object_id, VersionData(f"text {number}"))
        assert version_id is not None
        set_time(conn, "source_version", "fetched_at", version_id, f"2026-11-0{number}T12:00:00.000Z")
        ids.append(version_id)
    return ids


def by_kind(items: list[activity.ActivityItem], kind: str) -> list[activity.ActivityItem]:
    return [item for item in items if item.kind == kind]


# --- ordering and kinds -----------------------------------------------------------------------------


def test_items_of_all_kinds_are_merged_newest_first(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1", display_ref="Lead time")
    store.write_entry(conn, thread, entry("email_message", "m1", "2026-11-05T09:00:00.000Z", "Hello"))
    [_, second] = add_versions(conn, thread, 2)
    set_time(conn, "source_version", "fetched_at", second, "2026-11-07T09:00:00.000Z")
    comments.add_comment(conn, issue.key, "A comment", author_id=me.id)

    items = activity.issue_activity(conn, issue.key)

    assert [item.kind for item in items] == ["version", "entry", "comment", "history", "history"]
    stamps = [timeutil.parse_iso(item.at) for item in items]
    assert stamps == sorted(stamps, reverse=True)
    assert activity.DEFAULT_LIMIT == 200


def test_the_kinds_filter_limits_the_result(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1")
    store.write_entry(conn, thread, entry("email_message", "m1", "2026-11-05T09:00:00.000Z", "Hello"))
    add_versions(conn, thread, 2)
    comments.add_comment(conn, issue.key, "A comment", author_id=me.id)

    def kinds_of(kinds: tuple[str, ...]) -> set[str]:
        return {item.kind for item in activity.issue_activity(conn, issue.key, kinds=kinds)}

    assert kinds_of(("comment",)) == {"comment"}
    assert kinds_of(("history",)) == {"history"}
    assert kinds_of(("entry", "version")) == {"entry", "version"}
    assert kinds_of(("version",)) == {"version"}
    assert kinds_of(()) == set()
    with pytest.raises(ValidationError, match="activity kinds"):
        activity.issue_activity(conn, issue.key, kinds=("comment", "wiki"))


def test_simultaneous_items_keep_a_stable_order(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1")
    for external_id in ("m1", "m2", "m3"):
        store.write_entry(
            conn, thread, entry("email_message", external_id, "2026-11-05T09:00:00.000Z", external_id)
        )

    first = activity.issue_activity(conn, issue.key, kinds=("entry",))
    second = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert [item.body for item in first] == ["m3", "m2", "m1"]
    assert first == second


def test_timestamps_with_and_without_milliseconds_sort_by_the_moment(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-05T09:00:00Z", "whole second"))
    store.write_entry(conn, jira, entry("jira_comment", "c2", "2026-11-05T09:00:00.500Z", "half second"))

    items = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert [item.body for item in items] == ["half second", "whole second"]


def test_an_unknown_issue_is_not_found(conn: sqlalchemy.Connection, issue: issues.Issue) -> None:
    with pytest.raises(NotFoundError):
        activity.issue_activity(conn, "OIL-99")


# --- comments and history ---------------------------------------------------------------------------


def test_comments_become_items_without_the_deleted_ones(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person
) -> None:
    bea = people.create_person(conn, "Bea Brandt")
    kept = comments.add_comment(conn, issue.key, "From Bea", author_id=bea.id)
    gone = comments.add_comment(conn, issue.key, "Deleted one", author_id=me.id)
    comments.delete_comment(conn, gone.id, actor_id=me.id)
    edited = comments.add_comment(conn, issue.key, "Mine", author_id=me.id)
    comments.edit_comment(conn, edited.id, "Mine, edited", actor_id=me.id)

    items = by_kind(activity.issue_activity(conn, issue.key, kinds=("comment",)), "comment")

    assert [(i.title, i.actor, i.body, i.at) for i in items] == [
        ("Comment by Alex", "Alex", "Mine, edited", items[0].at),
        ("Comment by Bea Brandt", "Bea Brandt", "From Bea", kept.created_at),
    ]
    assert items[0].meta == {"comment_id": edited.id, "edited": True}
    assert items[1].meta["edited"] is False
    assert (items[0].source_object_id, items[0].link) == (None, None)


def test_history_has_one_item_per_change_set_with_the_actor_label(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person
) -> None:
    bea = people.create_person(conn, "Bea Brandt")
    issues.update_issue(
        conn, issue.key, {"summary": "New summary", "description": "Changed"}, actor_id=bea.id
    )

    items = activity.issue_activity(conn, issue.key, kinds=("history",))

    assert [(i.title, i.actor) for i in items] == [("Bea Brandt", "Bea Brandt"), ("You", "You")]
    assert items[0].body is not None
    assert set(items[0].body.splitlines()) == {
        "summary set: Summary → New summary",
        "description set: Description → Changed",
    }
    assert items[1].body is not None and "issue create: — → OIL-1" in items[1].body.splitlines()
    assert all("change_set_id" in item.meta for item in items)


# --- source entries ---------------------------------------------------------------------------------


def test_entry_titles_follow_the_entry_type(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1", display_ref="Lead time")
    jira = make_object(
        conn, connector_id, issue.key, "jira_issue", "J1", title="Jira title", display_ref="HW-1"
    )
    page = make_object(conn, connector_id, issue.key, "confluence_page", "P1", title="Design page")
    chat = make_object(conn, connector_id, issue.key, "teams_thread", "C1")
    note = make_object(conn, connector_id, issue.key, "note", "N1", title="My note")
    store.write_entry(
        conn,
        thread,
        entry("email_message", "m1", "2026-11-01T09:00:00.000Z", "Hi", metadata={"subject": "Lead time"}),
    )
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-02T09:00:00.000Z", "Looks fine"))
    store.write_entry(
        conn, jira, entry("jira_change", "h1:0", "2026-11-03T09:00:00.000Z", "status: Open → Done", None)
    )
    store.write_entry(
        conn, page, entry("confluence_comment", "c1", "2026-11-04T09:00:00.000Z", "Page remark")
    )
    store.write_entry(conn, chat, entry("teams_message", "t1", "2026-11-05T09:00:00.000Z", "Ping", CARL))
    store.write_entry(conn, note, entry("note_text", "n1", "2026-11-06T09:00:00.000Z", "Note body", None))

    items = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert [item.title for item in reversed(items)] == [
        "Email from Bea Brandt: Lead time",
        "Jira comment by Bea Brandt on HW-1",
        "Jira change on HW-1: status: Open → Done",
        "Confluence comment by Bea Brandt on Design page",
        "Teams message by Carl Conrad",
        "Note",
    ]
    assert [item.source_object_id for item in reversed(items)] == [thread, jira, jira, page, chat, note]
    assert items[0].link == f"/sources/{note}"
    assert [item.actor for item in reversed(items)] == [
        "Bea Brandt",
        "Bea Brandt",
        None,
        "Bea Brandt",
        "Carl Conrad",
        None,
    ]


def test_an_email_without_subject_or_author_has_a_plain_title(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1")
    store.write_entry(conn, thread, entry("email_message", "m1", "2026-11-01T09:00:00.000Z", "Hi"))
    store.write_entry(conn, thread, entry("email_message", "m2", "2026-11-02T09:00:00.000Z", "Hi", None))

    items = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert [item.title for item in items] == ["Email from unknown", "Email from Bea Brandt"]


def test_an_email_carries_recipients_direction_and_the_attachment_names(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1", display_ref="Lead time")
    store.write_entry(
        conn,
        thread,
        entry(
            "email_message",
            "m1",
            "2026-11-01T09:00:00.000Z",
            "Full text\n> quoted",
            new_text="Full text",
            recipients=(ALEX, CARL),
            metadata={"subject": "Lead time", "to": ["alex@example.com"], "cc": ["Carl@acme.com"]},
        ),
    )
    for name in ("spec.pdf", "quote.xlsx"):
        child = store.upsert_object(
            conn, connector_id, ObjectMeta("attachment", f"m1#{name}", title=name), parent_id=thread
        )
        store.write_version(conn, child, VersionData(f"content of {name}"))

    [item] = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert item.body == "Full text"
    assert item.meta == {
        "entry_id": item.meta["entry_id"],
        "entry_type": "email_message",
        "direction": "in",
        "recipients": ["Alex", "Carl Conrad"],
        "to": ["Alex"],
        "cc": ["Carl Conrad"],
        "attachments": ["spec.pdf", "quote.xlsx"],
    }


def test_a_sent_email_has_the_outgoing_direction(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1")
    store.write_entry(conn, thread, entry("email_message", "m1", "2026-11-01T09:00:00.000Z", "Hi", ALEX))

    [item] = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert item.meta["direction"] == "out"
    assert item.actor == "Alex"


def test_only_the_latest_revision_of_an_entry_counts(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-01T09:00:00.000Z", "first draft"))
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-01T09:00:00.000Z", "second draft"))

    [item] = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert item.body == "second draft"


def test_the_author_is_the_surviving_person_of_a_merge(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-01T09:00:00.000Z", "Hi", BEA))
    twin = people.find_person_by_identity(conn, "email", "bea@acme.com")
    assert twin is not None
    people.merge_persons(conn, twin.id, people.create_person(conn, "Beatrice Brandt").id, actor_id=me.id)

    [item] = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert item.actor == "Beatrice Brandt"
    assert item.title == "Jira comment by Beatrice Brandt on HW-1"


def test_entries_of_children_are_included(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    child = store.upsert_object(
        conn,
        connector_id,
        ObjectMeta("confluence_page", "child", title="Child page", display_ref="Child ref"),
        parent_id=jira,
    )
    store.write_entry(conn, child, entry("confluence_comment", "c1", "2026-11-01T09:00:00.000Z", "In child"))

    [item] = activity.issue_activity(conn, issue.key, kinds=("entry",))

    assert (item.source_object_id, item.link, item.title) == (
        child,
        f"/sources/{child}",
        "Confluence comment by Bea Brandt on Child page",
    )


# --- versions ---------------------------------------------------------------------------------------


def test_versions_after_the_first_become_items(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    page = make_object(conn, connector_id, issue.key, "confluence_page", "P1", title="Design page")
    single = make_object(conn, connector_id, issue.key, "note", "N1", title="Once")
    versions = add_versions(conn, page, 3)
    add_versions(conn, single, 1)

    items = activity.issue_activity(conn, issue.key, kinds=("version",))

    assert [item.title for item in items] == [
        "New version v3 of Design page",
        "New version v2 of Design page",
    ]
    assert [item.at for item in items] == ["2026-11-03T12:00:00.000Z", "2026-11-02T12:00:00.000Z"]
    assert items[0].meta == {"version_id": versions[2], "version_no": 3, "previous_version_no": 2}
    assert items[1].meta == {"version_id": versions[1], "version_no": 2, "previous_version_no": 1}
    assert all(item.body is None and item.actor is None for item in items)
    assert items[0].link == f"/sources/{page}"
    assert items[0].source_object_id == page


def test_versions_of_children_are_included(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    thread = make_object(conn, connector_id, issue.key, "email_thread", "t1")
    child = store.upsert_object(
        conn, connector_id, ObjectMeta("attachment", "a1", title="spec.pdf"), parent_id=thread
    )
    add_versions(conn, child, 2)

    [item] = activity.issue_activity(conn, issue.key, kinds=("version",))

    assert (item.title, item.source_object_id) == ("New version v2 of spec.pdf", child)


def test_the_first_fetch_through_the_sync_path_adds_no_version_item(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    state = store.object_state(conn, jira)
    store.apply_fetch(
        conn,
        state,
        FetchResult(
            version=VersionData("v1"),
            children=(ChildData(ObjectMeta("attachment", "a1", title="a"), VersionData("x")),),
        ),
    )

    assert activity.issue_activity(conn, issue.key, kinds=("version",)) == []


# --- links and limit --------------------------------------------------------------------------------


def test_unlinked_and_removed_sources_are_left_out(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person, connector_id: int
) -> None:
    kept = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    removed = make_object(conn, connector_id, issue.key, "jira_issue", "J2", display_ref="HW-2")
    stranger = make_object(conn, connector_id, issue.key, "jira_issue", "J3", display_ref="HW-3", link=False)
    for object_id in (kept, removed, stranger):
        store.write_entry(conn, object_id, entry("jira_comment", "c1", "2026-11-01T09:00:00.000Z", "Hi"))
        add_versions(conn, object_id, 2)
    linking.unlink_source(conn, issue.key, removed, actor_id=me.id)

    items = activity.issue_activity(conn, issue.key, kinds=("entry", "version"))

    assert {item.source_object_id for item in items} == {kept}
    assert len(items) == 2


def test_sources_of_other_issues_are_left_out(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person, connector_id: int
) -> None:
    other = issues.create_issue(conn, "OIL", "Other", "Other issue", actor_id=me.id)
    jira = make_object(conn, connector_id, other.key, "jira_issue", "J1", display_ref="HW-1")
    store.write_entry(conn, jira, entry("jira_comment", "c1", "2026-11-01T09:00:00.000Z", "Hi"))

    assert activity.issue_activity(conn, issue.key, kinds=("entry", "version")) == []
    assert len(activity.issue_activity(conn, other.key, kinds=("entry",))) == 1


def test_the_limit_keeps_the_newest_items(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    jira = make_object(conn, connector_id, issue.key, "jira_issue", "J1", display_ref="HW-1")
    for number in range(1, 6):
        store.write_entry(
            conn, jira, entry("jira_comment", f"c{number}", f"2026-11-0{number}T09:00:00.000Z", f"n{number}")
        )
    add_versions(conn, jira, 3)

    everything = activity.issue_activity(conn, issue.key, kinds=("entry", "version"))
    limited = activity.issue_activity(conn, issue.key, kinds=("entry", "version"), limit=3)

    assert len(everything) == 7
    assert limited == everything[:3]
    assert activity.issue_activity(conn, issue.key, limit=1) == everything[:1]


def test_the_number_of_queries_does_not_grow_with_the_linked_sources(
    conn: sqlalchemy.Connection, issue: issues.Issue, connector_id: int
) -> None:
    statements: list[str] = []

    def count(connection, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    def queries() -> int:
        statements.clear()
        sqlalchemy.event.listen(conn, "before_cursor_execute", count)
        try:
            activity.issue_activity(conn, issue.key)
        finally:
            sqlalchemy.event.remove(conn, "before_cursor_execute", count)
        return len(statements)

    def add_source(number: int) -> None:
        jira = make_object(
            conn, connector_id, issue.key, "jira_issue", f"J{number}", display_ref=f"HW-{number}"
        )
        for index in range(3):
            store.write_entry(
                conn,
                jira,
                entry("jira_comment", f"c{index}", f"2026-11-0{index + 1}T09:00:00.000Z", f"n{index}"),
            )
        add_versions(conn, jira, 3)

    add_source(1)
    one = queries()
    for number in range(2, 7):
        add_source(number)

    assert queries() == one


def test_comment_events_are_left_out_of_the_history_items(
    conn: sqlalchemy.Connection, issue: issues.Issue, me: people.Person
) -> None:
    only_comment = comments.add_comment(conn, issue.key, "Just a comment", author_id=me.id)
    mixed = history.start_change_set(conn, "user", actor_id=me.id)
    history.record(conn, mixed, issue.id, "comment", "add", None, "Comment in a bigger save")
    history.record(conn, mixed, issue.id, "priority", "set", "P4", "P1")

    items = activity.issue_activity(conn, issue.key)

    assert [i.body for i in by_kind(items, "comment")] == ["Just a comment"]
    bodies = [i.body for i in by_kind(items, "history")]
    assert "priority set: P4 → P1" in bodies
    assert not any("comment" in (body or "") for body in bodies)
    assert all("Just a comment" not in (body or "") for body in bodies)
    assert only_comment.id in {i.meta.get("comment_id") for i in items}
    change_sets = {i.meta["change_set_id"] for i in by_kind(items, "history")}
    assert mixed in change_sets
    assert (
        len(change_sets) == len(bodies) == 2
    )  # the creation and the mixed save; the comment-only one is gone
