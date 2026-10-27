import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import comments, issues, people, projects
from ai_oil.errors import ValidationError
from ai_oil.search import fts
from ai_oil.sources import linking, store
from ai_oil.sources.model import EntryData, ObjectMeta, VersionData


@pytest.fixture
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex", is_me=True)


@pytest.fixture
def oil(conn: sqlalchemy.Connection, me: people.Person) -> projects.Project:
    return projects.create_project(conn, "OIL", "Open issues")


@pytest.fixture
def connector_id(conn: sqlalchemy.Connection) -> int:
    return conn.execute(
        text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
    ).scalar_one()


def make_issue(
    conn: sqlalchemy.Connection, me: people.Person, summary: str = "Clarify Inductor lead time", **kwargs
) -> issues.Issue:
    return issues.create_issue(conn, "OIL", summary, "Supplier quoted 26 weeks", actor_id=me.id, **kwargs)


def make_object(
    conn: sqlalchemy.Connection,
    connector_id: int,
    external_id: str,
    *,
    object_type: str = "email_thread",
    title: str | None = "Thread",
    parent_id: int | None = None,
) -> int:
    return store.upsert_object(
        conn, connector_id, ObjectMeta(object_type, external_id, title=title), parent_id=parent_id
    )


def add_entry(
    conn: sqlalchemy.Connection,
    object_id: int,
    body: str,
    *,
    external_id: str = "m1",
    new_text: str | None = None,
) -> int:
    entry_id = store.write_entry(
        conn,
        object_id,
        EntryData("email_message", external_id, "2026-10-20T08:00:00Z", body, new_text=new_text),
    )
    assert entry_id is not None
    return entry_id


def kinds_found(conn: sqlalchemy.Connection, query: str) -> list[tuple[str, int]]:
    return sorted((hit.doc_kind, hit.doc_id) for hit in fts.search_docs(conn, query))


def rowids(conn: sqlalchemy.Connection) -> set[int]:
    return set(conn.execute(text("SELECT rowid FROM search_doc")).scalars())


def test_constants() -> None:
    assert fts.KIND_CODES == {"issue": 1, "comment": 2, "source_version": 3, "source_entry": 4}
    assert fts.ROWID_BASE == 1_000_000_000_000


def test_issue_found_by_prefix_and_key(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    issue = make_issue(conn, me)
    assert kinds_found(conn, "induct") == [("issue", issue.id)]
    assert kinds_found(conn, "OIL-1") == [("issue", issue.id)]
    assert kinds_found(conn, "quoted") == [("issue", issue.id)]
    assert rowids(conn) == {fts.KIND_CODES["issue"] * fts.ROWID_BASE + issue.id}


def test_issue_acceptance_criteria_is_indexed(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    issue = make_issue(conn, me, acceptance_criteria="Sample approved by PPAP")
    assert kinds_found(conn, "ppap") == [("issue", issue.id)]


def test_editing_summary_updates_document(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    issue = make_issue(conn, me)
    issues.update_issue(conn, issue.key, {"summary": "Clarify capacitor lead time"}, actor_id=me.id)
    assert kinds_found(conn, "inductor") == []
    assert kinds_found(conn, "capacitor") == [("issue", issue.id)]
    assert conn.execute(text("SELECT count(*) FROM search_doc")).scalar_one() == 1


def test_comment_found_then_removed_on_delete(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    issue = make_issue(conn, me)
    comment = comments.add_comment(conn, issue.key, "Supplier sent a revised drawing", author_id=me.id)
    assert kinds_found(conn, "drawing") == [("comment", comment.id)]
    comments.edit_comment(conn, comment.id, "Supplier sent a revised schematic", actor_id=me.id)
    assert kinds_found(conn, "drawing") == []
    assert kinds_found(conn, "schematic") == [("comment", comment.id)]
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    assert kinds_found(conn, "schematic") == []


def test_only_latest_source_version_is_indexed(conn: sqlalchemy.Connection, connector_id: int) -> None:
    object_id = make_object(conn, connector_id, "page-1", object_type="confluence_page", title="Spec page")
    first = store.write_version(conn, object_id, VersionData("Torque of 5 Nm"))
    second = store.write_version(conn, object_id, VersionData("Torque of 8 Nm"))
    assert first is not None and second is not None
    assert kinds_found(conn, "torque") == [("source_version", second)]
    assert kinds_found(conn, "5") == []
    assert kinds_found(conn, "spec") == [("source_version", second)]


def test_only_latest_entry_revision_is_indexed(conn: sqlalchemy.Connection, connector_id: int) -> None:
    object_id = make_object(conn, connector_id, "c1", object_type="jira_issue")
    first = store.write_entry(
        conn, object_id, EntryData("jira_comment", "10", "2026-10-20T08:00:00Z", "Pending approval")
    )
    second = store.write_entry(
        conn, object_id, EntryData("jira_comment", "10", "2026-10-20T08:00:00Z", "Approval granted")
    )
    other = store.write_entry(
        conn, object_id, EntryData("jira_comment", "11", "2026-10-21T08:00:00Z", "Approval documented")
    )
    assert first is not None and second is not None and other is not None
    assert kinds_found(conn, "pending") == []
    assert kinds_found(conn, "approval") == [("source_entry", second), ("source_entry", other)]


def test_entry_with_new_text_indexes_new_text(conn: sqlalchemy.Connection, connector_id: int) -> None:
    object_id = make_object(conn, connector_id, "t1")
    entry_id = add_entry(
        conn, object_id, "Fresh reply\n> quoted history about gearbox", new_text="Fresh reply"
    )
    assert kinds_found(conn, "fresh") == [("source_entry", entry_id)]
    assert kinds_found(conn, "gearbox") == []


def test_renaming_source_object_retitles_version_and_entry_docs(
    conn: sqlalchemy.Connection, connector_id: int
) -> None:
    object_id = make_object(conn, connector_id, "page-1", object_type="confluence_page", title="Oldname")
    version_id = store.write_version(conn, object_id, VersionData("Torque 5 Nm"))
    store.write_entry(conn, object_id, EntryData("jira_comment", "10", "2026-10-20T08:00:00Z", "Early"))
    entry_id = store.write_entry(
        conn, object_id, EntryData("jira_comment", "10", "2026-10-20T08:00:00Z", "Late")
    )
    assert version_id is not None and entry_id is not None
    assert kinds_found(conn, "oldname") == [("source_entry", entry_id), ("source_version", version_id)]
    store.upsert_object(conn, connector_id, ObjectMeta("confluence_page", "page-1", title="Newname"))
    assert kinds_found(conn, "oldname") == []
    assert kinds_found(conn, "newname") == [("source_entry", entry_id), ("source_version", version_id)]
    assert conn.execute(text("SELECT count(*) FROM search_doc")).scalar_one() == 2


def test_issue_ids_matching_covers_direct_comment_and_sources(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    first = make_issue(conn, me, "First issue")
    second = make_issue(conn, me, "Second issue")
    third = make_issue(conn, me, "Third issue")
    thread = make_object(conn, connector_id, "thread-1")
    add_entry(conn, thread, "Please confirm the zebra tooling date")
    linking.link_source(conn, first.key, thread, actor_id=me.id)
    linking.link_source(conn, second.key, thread, actor_id=me.id)
    assert fts.issue_ids_matching(conn, "zebra") == {first.id, second.id}

    comments.add_comment(conn, third.key, "Also mentions zebra", author_id=me.id)
    assert fts.issue_ids_matching(conn, "zebra") == {first.id, second.id, third.id}
    assert fts.issue_ids_matching(conn, "third") == {third.id}


def test_issue_ids_matching_ignores_removed_links_and_deleted_comments(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    linked = make_issue(conn, me, "Linked issue")
    unlinked = make_issue(conn, me, "Unlinked issue")
    removed = make_issue(conn, me, "Removed issue")
    thread = make_object(conn, connector_id, "thread-1")
    add_entry(conn, thread, "Please confirm the zebra tooling date")
    linking.link_source(conn, linked.key, thread, actor_id=me.id)
    linking.link_source(conn, removed.key, thread, actor_id=me.id)
    linking.unlink_source(conn, removed.key, thread, actor_id=me.id)
    comment = comments.add_comment(conn, unlinked.key, "zebra in a comment", author_id=me.id)
    comments.delete_comment(conn, comment.id, actor_id=me.id)
    assert fts.issue_ids_matching(conn, "zebra") == {linked.id}


def test_issue_ids_matching_uses_parent_links_for_child_object(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    first = make_issue(conn, me, "First issue")
    second = make_issue(conn, me, "Second issue")
    make_issue(conn, me, "Third issue")
    thread = make_object(conn, connector_id, "thread-1")
    attachment = make_object(
        conn, connector_id, "thread-1#1", object_type="attachment", title="drawing.pdf", parent_id=thread
    )
    store.write_version(conn, attachment, VersionData("Flux density plot"))
    linking.link_source(conn, first.key, thread, actor_id=me.id)
    linking.link_source(conn, second.key, thread, actor_id=me.id)
    assert fts.issue_ids_matching(conn, "flux") == {first.id, second.id}


def test_issue_ids_matching_through_source_version(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    issue = make_issue(conn, me)
    page = make_object(conn, connector_id, "page-1", object_type="confluence_page", title="Spec")
    store.write_version(conn, page, VersionData("Torque 5 Nm"))
    linking.link_source(conn, issue.key, page, actor_id=me.id)
    assert fts.issue_ids_matching(conn, "torque") == {issue.id}
    assert fts.issue_ids_matching(conn, "nothing-like-this") == set()


@pytest.mark.parametrize(
    ("user_text", "expected"),
    [
        ("inductor", '"inductor"*'),
        ("  inductor   lead\ttime ", '"inductor"* "lead"* "time"*'),
        ('"lead time"', '"lead time"'),
        ('"lead  time" supplier', '"lead time" "supplier"*'),
        ('lead "time', '"lead"* "time"*'),
        ('say "hi" now', '"say"* "hi" "now"*'),
        ('""  word', '"word"*'),
        ("a-b c:d (x) ^y +z", '"a-b"* "c:d"* "(x)"* "^y"* "+z"*'),
        ("foo OR bar NOT baz NEAR qux AND *", '"foo"* "OR"* "bar"* "NOT"* "baz"* "NEAR"* "qux"* "AND"* "*"*'),
    ],
)
def test_to_fts_query(user_text: str, expected: str) -> None:
    assert fts.to_fts_query(user_text) == expected


@pytest.mark.parametrize("blank", ["", "   ", "\n\t", '""', '" "'])
def test_to_fts_query_blank_raises(blank: str) -> None:
    with pytest.raises(ValidationError, match="empty search text"):
        fts.to_fts_query(blank)


@pytest.mark.parametrize(
    "user_text",
    [
        "inductor",
        '"lead time"',
        '"lead',
        'lead"',
        "-",
        "-inductor",
        "*",
        "^inductor",
        "inductor:",
        "col:val",
        "(inductor",
        "inductor)",
        "+",
        "OR",
        "AND",
        "NOT",
        "NEAR",
        "NEAR(a b)",
        "inductor OR capacitor",
        "c++ \"don't\" 'x' ;",
        "title:inductor",
    ],
)
def test_special_input_never_raises_syntax_error(
    conn: sqlalchemy.Connection, me: people.Person, oil, user_text: str
) -> None:
    make_issue(conn, me)
    fts.search_docs(conn, user_text)
    fts.issue_ids_matching(conn, user_text)


def test_operator_words_are_literal(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    issue = make_issue(conn, me)
    assert fts.issue_ids_matching(conn, "inductor OR zebra") == set()
    assert fts.issue_ids_matching(conn, "inductor NOT lead") == set()
    assert fts.issue_ids_matching(conn, "inductor lead") == {issue.id}
    assert fts.issue_ids_matching(conn, '"lead time"') == {issue.id}
    assert fts.issue_ids_matching(conn, '"time lead"') == set()


def test_search_docs_hit_fields_and_limit(conn: sqlalchemy.Connection, me: people.Person, oil) -> None:
    make_issue(conn, me, "Alpha inductor")
    make_issue(conn, me, "Beta inductor")
    hits = fts.search_docs(conn, "supplier")
    assert len(hits) == 2
    assert all(hit.doc_kind == "issue" and "[Supplier]" in hit.snippet for hit in hits)
    assert isinstance(hits[0].rank, float)
    assert hits[0].title.startswith("OIL-")
    assert len(fts.search_docs(conn, "supplier", limit=1)) == 1


def populate(conn: sqlalchemy.Connection, me: people.Person, connector_id: int) -> None:
    issue = make_issue(conn, me)
    other = make_issue(conn, me, "Other issue")
    comments.add_comment(conn, issue.key, "A live comment", author_id=me.id)
    dead = comments.add_comment(conn, other.key, "A dead comment", author_id=me.id)
    comments.delete_comment(conn, dead.id, actor_id=me.id)
    page = make_object(conn, connector_id, "page-1", object_type="confluence_page", title="Spec")
    store.write_version(conn, page, VersionData("Version one"))
    store.write_version(conn, page, VersionData("Version two"))
    thread = make_object(conn, connector_id, "thread-1")
    add_entry(conn, thread, "First message", external_id="m1")
    add_entry(conn, thread, "Second message", external_id="m2")
    store.write_entry(
        conn, thread, EntryData("email_message", "m2", "2026-10-21T08:00:00Z", "Second, edited")
    )


def test_reindex_all_reproduces_trigger_maintained_index(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    populate(conn, me, connector_id)
    expected_rowids = rowids(conn)
    expected_docs = conn.execute(
        text("SELECT rowid, doc_kind, doc_id, title, body FROM search_doc ORDER BY rowid")
    ).all()
    assert fts.reindex_all(conn) == {"issue": 2, "comment": 1, "source_version": 1, "source_entry": 2}
    assert rowids(conn) == expected_rowids
    assert (
        conn.execute(text("SELECT rowid, doc_kind, doc_id, title, body FROM search_doc ORDER BY rowid")).all()
        == expected_docs
    )


def test_reindex_all_restores_deleted_rows(
    conn: sqlalchemy.Connection, me: people.Person, oil, connector_id: int
) -> None:
    populate(conn, me, connector_id)
    expected_rowids = rowids(conn)
    conn.execute(text("DELETE FROM search_doc WHERE doc_kind IN ('comment', 'source_entry')"))
    assert rowids(conn) != expected_rowids
    counts = fts.reindex_all(conn)
    assert counts == {"issue": 2, "comment": 1, "source_version": 1, "source_entry": 2}
    assert rowids(conn) == expected_rowids
    assert kinds_found(conn, "edited") != []


def test_reindex_all_on_empty_database(conn: sqlalchemy.Connection) -> None:
    assert fts.reindex_all(conn) == {"issue": 0, "comment": 0, "source_version": 0, "source_entry": 0}
