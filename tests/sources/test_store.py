import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.domain import people
from ai_oil.errors import NotFoundError
from ai_oil.sources import store
from ai_oil.sources.blobs import BlobStore
from ai_oil.sources.model import (
    ChildData,
    EntryData,
    FetchResult,
    IdentityRef,
    ObjectMeta,
    VersionData,
)

_START = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)


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
def connector_id(conn: sqlalchemy.Connection) -> int:
    return conn.execute(
        sqlalchemy.text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
    ).scalar_one()


@pytest.fixture
def object_id(conn: sqlalchemy.Connection, connector_id: int) -> int:
    return store.upsert_object(conn, connector_id, ObjectMeta("note", "n1"))


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs", max_bytes=20)


def _entry(external_id: str = "m1", text: str = "hello", **kwargs: object) -> EntryData:
    return EntryData("email_message", external_id, "2026-01-01T10:00:00Z", text, **kwargs)  # type: ignore[arg-type]


def _text(conn: sqlalchemy.Connection, sql: str, **params: object) -> object:
    return conn.execute(sqlalchemy.text(sql), params).scalar_one()


# --- hashing -------------------------------------------------------------------------------------------


def test_version_hash_covers_text_and_sorted_metadata() -> None:
    data = VersionData("Grüße", {"b": 1, "a": "ü"})
    expected = hashlib.sha256(('Grüße\n{"a": "ü", "b": 1}').encode()).hexdigest()
    assert store.version_hash(data) == expected
    assert store.version_hash(VersionData("Grüße", {"a": "ü", "b": 1})) == expected
    assert store.version_hash(VersionData("Grüße", {"a": "ü", "b": 2})) != expected


def test_entry_hash_is_sha256_of_text() -> None:
    assert store.entry_hash(_entry(text="abc")) == hashlib.sha256(b"abc").hexdigest()


# --- objects -------------------------------------------------------------------------------------------


def test_upsert_inserts_pending_object(conn: sqlalchemy.Connection, connector_id: int) -> None:
    meta = ObjectMeta("jira_issue", "10001", title="Fix", display_ref="ABC-1", locator="http://x/ABC-1")
    object_id = store.upsert_object(conn, connector_id, meta)
    obj = store.get_object(conn, object_id)
    assert (obj.connector_id, obj.object_type, obj.external_id) == (connector_id, "jira_issue", "10001")
    assert (obj.title, obj.display_ref, obj.locator) == ("Fix", "ABC-1", "http://x/ABC-1")
    assert obj.fetch_status == "pending"
    assert obj.first_seen_at
    assert obj.parent_id is None
    assert obj.last_checked_at is None
    assert store.find_object(conn, connector_id, "jira_issue", "10001") == obj


def test_upsert_updates_only_given_values_and_returns_same_id(
    conn: sqlalchemy.Connection, connector_id: int
) -> None:
    first = store.upsert_object(
        conn, connector_id, ObjectMeta("note", "1", title="A", display_ref="r", locator="l")
    )
    second = store.upsert_object(conn, connector_id, ObjectMeta("note", "1", title="B"))
    assert second == first
    obj = store.get_object(conn, first)
    assert (obj.title, obj.display_ref, obj.locator) == ("B", "r", "l")
    store.upsert_object(conn, connector_id, ObjectMeta("note", "1"))
    assert store.get_object(conn, first).title == "B"


def test_upsert_sets_parent_only_when_given(conn: sqlalchemy.Connection, connector_id: int) -> None:
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    child = store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"), parent_id=parent)
    assert store.get_object(conn, child).parent_id == parent
    store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"))
    assert store.get_object(conn, child).parent_id == parent


def test_upsert_never_writes_cursor(conn: sqlalchemy.Connection, connector_id: int) -> None:
    object_id = store.upsert_object(conn, connector_id, ObjectMeta("note", "1", cursor="c1"))
    assert store.get_object(conn, object_id).cursor is None
    store.upsert_object(conn, connector_id, ObjectMeta("note", "1", cursor="c2"))
    assert store.get_object(conn, object_id).cursor is None


def test_objects_are_unique_per_connector_type_and_external_id(
    conn: sqlalchemy.Connection, connector_id: int
) -> None:
    other_connector = conn.execute(
        sqlalchemy.text("INSERT INTO connector (type, name) VALUES ('fake', 'Other') RETURNING id")
    ).scalar_one()
    ids = {
        store.upsert_object(conn, connector_id, ObjectMeta("note", "1")),
        store.upsert_object(conn, connector_id, ObjectMeta("file", "1")),
        store.upsert_object(conn, other_connector, ObjectMeta("note", "1")),
    }
    assert len(ids) == 3


def test_get_and_find_missing(conn: sqlalchemy.Connection, connector_id: int) -> None:
    with pytest.raises(NotFoundError):
        store.get_object(conn, 999)
    assert store.find_object(conn, connector_id, "note", "nope") is None
    with pytest.raises(NotFoundError):
        store.get_version(conn, 999)


def test_list_children_ordered_by_id(conn: sqlalchemy.Connection, connector_id: int) -> None:
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    second = store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#2"), parent_id=parent)
    first = store.upsert_object(conn, connector_id, ObjectMeta("attachment", "t#1"), parent_id=parent)
    store.upsert_object(conn, connector_id, ObjectMeta("attachment", "x#1"))
    assert [c.id for c in store.list_children(conn, parent)] == [second, first]
    assert store.list_children(conn, second) == []


def test_object_state_reports_latest_content_hash(conn: sqlalchemy.Connection, object_id: int) -> None:
    state = store.object_state(conn, object_id)
    assert state.latest_content_hash is None
    assert (state.id, state.object_type, state.external_id, state.cursor) == (object_id, "note", "n1", None)
    store.write_version(conn, object_id, VersionData("A"))
    store.write_version(conn, object_id, VersionData("B"))
    assert store.object_state(conn, object_id).latest_content_hash == store.version_hash(VersionData("B"))
    with pytest.raises(NotFoundError):
        store.object_state(conn, 999)


# --- versions ------------------------------------------------------------------------------------------


def test_first_version_is_numbered_one_and_round_trips(conn: sqlalchemy.Connection, object_id: int) -> None:
    data = VersionData(
        "body", {"status": "Open"}, source_modified_at="2026-01-01T00:00:00Z", source_version_ref="7"
    )
    version_id = store.write_version(conn, object_id, data, sync_run_id=None)
    assert version_id is not None
    version = store.get_version(conn, version_id)
    assert version.version_no == 1
    assert version.text == "body"
    assert version.metadata == {"status": "Open"}
    assert (version.source_modified_at, version.source_version_ref) == ("2026-01-01T00:00:00Z", "7")
    assert version.content_hash == store.version_hash(data)
    assert version.blob_hash is None
    assert store.latest_version(conn, object_id) == version
    assert store.list_versions(conn, object_id) == [version]


def test_latest_version_none_without_versions(conn: sqlalchemy.Connection, object_id: int) -> None:
    assert store.latest_version(conn, object_id) is None
    assert store.list_versions(conn, object_id) == []


def test_identical_content_writes_no_second_version(conn: sqlalchemy.Connection, object_id: int) -> None:
    assert store.write_version(conn, object_id, VersionData("A")) is not None
    assert store.write_version(conn, object_id, VersionData("A")) is None
    assert len(store.list_versions(conn, object_id)) == 1


def test_revert_creates_three_versions(conn: sqlalchemy.Connection, object_id: int) -> None:
    for text in ("A", "B", "A"):
        assert store.write_version(conn, object_id, VersionData(text)) is not None
    versions = store.list_versions(conn, object_id)
    assert [(v.version_no, v.text) for v in versions] == [(1, "A"), (2, "B"), (3, "A")]
    assert versions[0].content_hash == versions[2].content_hash


def test_metadata_change_alone_creates_a_version(conn: sqlalchemy.Connection, object_id: int) -> None:
    assert store.write_version(conn, object_id, VersionData("A", {"status": "Open"})) is not None
    assert store.write_version(conn, object_id, VersionData("A", {"status": "Open"})) is None
    assert store.write_version(conn, object_id, VersionData("A", {"status": "Done"})) is not None
    assert [v.metadata for v in store.list_versions(conn, object_id)] == [
        {"status": "Open"},
        {"status": "Done"},
    ]


def test_versions_are_per_object(conn: sqlalchemy.Connection, connector_id: int, object_id: int) -> None:
    other = store.upsert_object(conn, connector_id, ObjectMeta("note", "n2"))
    assert store.write_version(conn, object_id, VersionData("A")) is not None
    assert store.write_version(conn, other, VersionData("A")) is not None
    assert store.latest_version(conn, other).version_no == 1  # type: ignore[union-attr]


def test_truncation_flags_metadata_without_changing_hash_basis(
    conn: sqlalchemy.Connection, object_id: int
) -> None:
    data = VersionData("x" * 50, {"k": "v"})
    first = store.write_version(conn, object_id, data, max_text_chars=10)
    assert first is not None
    version = store.get_version(conn, first)
    assert version.text == "x" * 10
    assert version.metadata == {"k": "v", "truncated": True}
    assert version.content_hash == store.version_hash(data)
    assert store.write_version(conn, object_id, data, max_text_chars=10) is None


def test_text_at_limit_is_not_truncated(conn: sqlalchemy.Connection, object_id: int) -> None:
    version_id = store.write_version(conn, object_id, VersionData("x" * 10), max_text_chars=10)
    version = store.get_version(conn, version_id)  # type: ignore[arg-type]
    assert version.text == "x" * 10
    assert "truncated" not in version.metadata


def test_raw_is_stored_through_the_blob_store(
    conn: sqlalchemy.Connection, object_id: int, blobs: BlobStore
) -> None:
    raw = b"%PDF raw"
    version_id = store.write_version(
        conn, object_id, VersionData("t", raw=raw, media_type="application/pdf"), blobs=blobs
    )
    version = store.get_version(conn, version_id)  # type: ignore[arg-type]
    assert version.blob_hash == hashlib.sha256(raw).hexdigest()
    assert "raw_skipped" not in version.metadata
    assert blobs.get(conn, version.blob_hash) == raw
    assert _text(conn, "SELECT media_type FROM blob") == "application/pdf"


def test_raw_too_large_is_skipped_and_noted(
    conn: sqlalchemy.Connection, object_id: int, blobs: BlobStore
) -> None:
    version_id = store.write_version(conn, object_id, VersionData("t", raw=b"x" * 21), blobs=blobs)
    version = store.get_version(conn, version_id)  # type: ignore[arg-type]
    assert version.blob_hash is None
    assert version.metadata == {"raw_skipped": "too large"}
    assert _text(conn, "SELECT count(*) FROM blob") == 0


def test_raw_without_blob_store_is_ignored(conn: sqlalchemy.Connection, object_id: int) -> None:
    version_id = store.write_version(conn, object_id, VersionData("t", raw=b"abc"))
    version = store.get_version(conn, version_id)  # type: ignore[arg-type]
    assert version.blob_hash is None
    assert version.metadata == {}


def test_sync_run_id_is_stored(conn: sqlalchemy.Connection, object_id: int) -> None:
    run_id = conn.execute(
        sqlalchemy.text("INSERT INTO sync_run (trigger, scope) VALUES ('cli', 'all') RETURNING id")
    ).scalar_one()
    version_id = store.write_version(conn, object_id, VersionData("A"), sync_run_id=run_id)
    entry_id = store.write_entry(conn, object_id, _entry(), sync_run_id=run_id)
    assert store.get_version(conn, version_id).sync_run_id == run_id  # type: ignore[arg-type]
    assert _text(conn, "SELECT sync_run_id FROM source_entry WHERE id = :id", id=entry_id) == run_id


# --- entries -------------------------------------------------------------------------------------------


def test_new_entry_is_revision_one(conn: sqlalchemy.Connection, object_id: int) -> None:
    entry_id = store.write_entry(conn, object_id, _entry(text="hello", new_text="hi", metadata={"k": 1}))
    assert entry_id is not None
    (entry,) = store.list_entries(conn, object_id)
    assert entry.id == entry_id
    assert (entry.entry_type, entry.external_id, entry.revision) == ("email_message", "m1", 1)
    assert (entry.text, entry.new_text) == ("hello", "hi")
    assert entry.occurred_at == "2026-01-01T10:00:00Z"
    assert entry.metadata == {"k": 1}
    assert entry.content_hash == store.entry_hash(_entry(text="hello"))
    assert entry.author_identity_id is None
    assert entry.direction is None
    assert entry.recipients == []
    assert entry.fetched_at


def test_unchanged_entry_is_skipped(conn: sqlalchemy.Connection, object_id: int) -> None:
    assert store.write_entry(conn, object_id, _entry()) is not None
    assert store.write_entry(conn, object_id, _entry()) is None
    assert len(store.list_entries(conn, object_id, all_revisions=True)) == 1


def test_edited_entry_gets_new_revision_and_keeps_the_old_one(
    conn: sqlalchemy.Connection, object_id: int
) -> None:
    store.write_entry(conn, object_id, _entry("m1", "first"))
    store.write_entry(conn, object_id, _entry("m2", "other"))
    assert store.write_entry(conn, object_id, _entry("m1", "edited")) is not None
    assert store.write_entry(conn, object_id, _entry("m1", "edited")) is None
    assert store.write_entry(conn, object_id, _entry("m1", "edited again")) is not None

    latest = store.list_entries(conn, object_id)
    assert sorted((e.external_id, e.revision, e.text) for e in latest) == [
        ("m1", 3, "edited again"),
        ("m2", 1, "other"),
    ]
    everything = store.list_entries(conn, object_id, all_revisions=True)
    assert sorted((e.external_id, e.revision, e.text) for e in everything) == [
        ("m1", 1, "first"),
        ("m1", 2, "edited"),
        ("m1", 3, "edited again"),
        ("m2", 1, "other"),
    ]


def test_revisions_are_per_entry_type(conn: sqlalchemy.Connection, object_id: int) -> None:
    store.write_entry(conn, object_id, _entry("1", "a"))
    comment = EntryData("jira_comment", "1", "2026-01-01T11:00:00Z", "a")
    assert store.write_entry(conn, object_id, comment) is not None
    assert {e.revision for e in store.list_entries(conn, object_id)} == {1}


def test_entries_are_ordered_by_occurred_at_then_id(conn: sqlalchemy.Connection, object_id: int) -> None:
    for external_id, occurred in (("late", "2026-01-03T00:00:00Z"), ("a", "2026-01-01T00:00:00Z")):
        store.write_entry(conn, object_id, EntryData("email_message", external_id, occurred, external_id))
    store.write_entry(conn, object_id, EntryData("email_message", "b", "2026-01-01T00:00:00Z", "b"))
    assert [e.external_id for e in store.list_entries(conn, object_id)] == ["a", "b", "late"]


def test_direction_out_for_me_and_in_for_others(conn: sqlalchemy.Connection, object_id: int) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    people.add_identity(conn, me.id, "email", "alex@example.com")
    store.write_entry(conn, object_id, _entry("mine", author=IdentityRef("email", "ALEX@example.com")))
    store.write_entry(conn, object_id, _entry("theirs", author=IdentityRef("email", "bob@example.com")))
    by_id = {e.external_id: e for e in store.list_entries(conn, object_id)}
    assert by_id["mine"].direction == "out"
    assert by_id["theirs"].direction == "in"


def test_direction_follows_merged_person(conn: sqlalchemy.Connection, object_id: int) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    duplicate = people.create_person(conn, "Alex duplicate")
    people.add_identity(conn, duplicate.id, "email", "alex2@example.com")
    people.merge_persons(conn, duplicate.id, me.id, actor_id=me.id)
    store.write_entry(conn, object_id, _entry(author=IdentityRef("email", "alex2@example.com")))
    assert store.list_entries(conn, object_id)[0].direction == "out"


def test_explicit_direction_wins(conn: sqlalchemy.Connection, object_id: int) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    people.add_identity(conn, me.id, "email", "alex@example.com")
    store.write_entry(
        conn, object_id, _entry("a", author=IdentityRef("email", "alex@example.com"), direction="in")
    )
    store.write_entry(
        conn, object_id, _entry("b", author=IdentityRef("email", "bob@example.com"), direction="out")
    )
    by_id = {e.external_id: e for e in store.list_entries(conn, object_id)}
    assert (by_id["a"].direction, by_id["b"].direction) == ("in", "out")


def test_no_author_means_no_direction(conn: sqlalchemy.Connection, object_id: int) -> None:
    people.create_person(conn, "Alex", is_me=True)
    store.write_entry(conn, object_id, _entry())
    entry = store.list_entries(conn, object_id)[0]
    assert (entry.author_identity_id, entry.direction) == (None, None)


def test_unknown_author_creates_a_person(conn: sqlalchemy.Connection, object_id: int) -> None:
    people.create_person(conn, "Alex", is_me=True)
    author = IdentityRef("email", "New.Person@Example.com", "New Person")
    store.write_entry(conn, object_id, _entry(author=author))
    entry = store.list_entries(conn, object_id)[0]
    assert entry.author_identity_id is not None
    person = people.find_person_by_identity(conn, "email", "new.person@example.com")
    assert person is not None
    assert person.display_name == "New Person"
    assert not person.is_me
    assert (
        _text(conn, "SELECT person_id FROM person_identity WHERE id = :id", id=entry.author_identity_id)
        == person.id
    )


def test_recipients_are_stored_as_identity_ids(conn: sqlalchemy.Connection, object_id: int) -> None:
    recipients = (IdentityRef("email", "a@example.com"), IdentityRef("email", "b@example.com", "Bee"))
    store.write_entry(conn, object_id, _entry(recipients=recipients))
    entry = store.list_entries(conn, object_id)[0]
    expected = [people.resolve_identity(conn, "email", ref.external_id).id for ref in recipients]
    assert entry.recipients == expected
    assert len(set(expected)) == 2
    stored = _text(conn, "SELECT recipients_json FROM source_entry WHERE id = :id", id=entry.id)
    assert json.loads(stored) == expected  # type: ignore[arg-type]


def test_entry_truncation_and_raw_handling(
    conn: sqlalchemy.Connection, object_id: int, blobs: BlobStore
) -> None:
    long_entry = _entry("long", "y" * 50, new_text="z" * 40, raw=b"x" * 21)
    assert store.write_entry(conn, object_id, long_entry, blobs=blobs, max_text_chars=10) is not None
    assert store.write_entry(conn, object_id, long_entry, blobs=blobs, max_text_chars=10) is None
    small = _entry("small", "s", raw=b"raw!", media_type="message/rfc822")
    store.write_entry(conn, object_id, small, blobs=blobs, max_text_chars=10)
    by_id = {e.external_id: e for e in store.list_entries(conn, object_id)}
    long, short = by_id["long"], by_id["small"]
    assert (long.text, long.new_text) == ("y" * 10, "z" * 10)
    assert long.metadata == {"truncated": True, "raw_skipped": "too large"}
    assert long.content_hash == store.entry_hash(long_entry)
    assert long.blob_hash is None
    assert short.metadata == {}
    assert blobs.get(conn, short.blob_hash) == b"raw!"  # type: ignore[arg-type]


# --- apply_fetch ---------------------------------------------------------------------------------------


def test_apply_ok_sets_object_fields_and_returns_ids(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    result = FetchResult(
        version=VersionData("body"), entries=(_entry("m1"), _entry("m2")), cursor="c1", title="Subject"
    )
    outcome = store.apply_fetch(conn, store.object_state(conn, object_id), result)
    assert outcome.changed
    assert outcome.gone is False
    assert outcome.child_ids == ()
    assert [v.id for v in store.list_versions(conn, object_id)] == list(outcome.new_version_ids)
    assert sorted(e.id for e in store.list_entries(conn, object_id)) == sorted(outcome.new_entry_ids)
    assert len(outcome.new_entry_ids) == 2
    obj = store.get_object(conn, object_id)
    assert (obj.fetch_status, obj.fetch_error, obj.cursor, obj.title) == ("ok", None, "c1", "Subject")
    assert obj.last_checked_at is not None
    assert obj.last_changed_at == obj.last_checked_at
    assert store.object_state(conn, object_id).cursor == "c1"


def test_apply_unchanged_updates_checked_but_not_changed(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    result = FetchResult(version=VersionData("body"), entries=(_entry(),), cursor="c1", title="T")
    store.apply_fetch(conn, store.object_state(conn, object_id), result)
    first = store.get_object(conn, object_id)
    outcome = store.apply_fetch(
        conn,
        store.object_state(conn, object_id),
        FetchResult(version=VersionData("body"), entries=(_entry(),)),
    )
    assert not outcome.changed
    assert outcome.new_version_ids == outcome.new_entry_ids == ()
    second = store.get_object(conn, object_id)
    assert second.last_changed_at == first.last_changed_at
    assert second.last_checked_at != first.last_checked_at
    assert second.last_checked_at > first.last_checked_at  # type: ignore[operator]
    assert (second.cursor, second.title) == ("c1", "T")


def test_apply_without_cursor_or_title_keeps_stored_values(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    store.apply_fetch(conn, store.object_state(conn, object_id), FetchResult(cursor="c1", title="T"))
    store.apply_fetch(conn, store.object_state(conn, object_id), FetchResult())
    obj = store.get_object(conn, object_id)
    assert (obj.cursor, obj.title, obj.fetch_status) == ("c1", "T", "ok")
    assert obj.last_changed_at is None


def test_apply_clears_a_previous_error(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    store.mark_error(conn, object_id, "timeout")
    store.apply_fetch(conn, store.object_state(conn, object_id), FetchResult())
    obj = store.get_object(conn, object_id)
    assert (obj.fetch_status, obj.fetch_error) == ("ok", None)


def test_apply_upserts_children_with_parent_cursor_and_content(
    conn: sqlalchemy.Connection, connector_id: int, clock: list[datetime]
) -> None:
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    children = (
        ChildData(
            ObjectMeta("attachment", "t#1", title="a.pdf", cursor="mtime1"),
            version=VersionData("attachment text"),
        ),
        ChildData(ObjectMeta("attachment", "t#2", title="b.pdf"), entries=(_entry("ce"),)),
    )
    result = FetchResult(version=VersionData("thread"), children=children, cursor="p1")
    outcome = store.apply_fetch(conn, store.object_state(conn, parent), result)

    kids = store.list_children(conn, parent)
    assert [k.external_id for k in kids] == ["t#1", "t#2"]
    assert list(outcome.child_ids) == [k.id for k in kids]
    assert all(k.parent_id == parent and k.fetch_status == "ok" for k in kids)
    assert (kids[0].cursor, kids[1].cursor) == ("mtime1", None)
    assert all(k.last_checked_at and k.last_changed_at for k in kids)
    assert len(outcome.new_version_ids) == 2
    assert len(outcome.new_entry_ids) == 1
    assert store.latest_version(conn, kids[0].id).text == "attachment text"  # type: ignore[union-attr]
    assert len(store.list_entries(conn, kids[1].id)) == 1


def test_apply_child_content_alone_marks_parent_changed(
    conn: sqlalchemy.Connection, connector_id: int, clock: list[datetime]
) -> None:
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    first = FetchResult(
        version=VersionData("thread"),
        children=(ChildData(ObjectMeta("attachment", "t#1"), VersionData("v1")),),
    )
    store.apply_fetch(conn, store.object_state(conn, parent), first)
    before = store.get_object(conn, parent)
    second = FetchResult(
        version=VersionData("thread"),
        children=(ChildData(ObjectMeta("attachment", "t#1"), VersionData("v2")),),
    )
    outcome = store.apply_fetch(conn, store.object_state(conn, parent), second)
    assert outcome.changed
    after = store.get_object(conn, parent)
    assert after.last_changed_at > before.last_changed_at  # type: ignore[operator]
    child = store.list_children(conn, parent)[0]
    assert child.last_changed_at == after.last_changed_at


def test_apply_unchanged_child_keeps_its_last_changed(
    conn: sqlalchemy.Connection, connector_id: int, clock: list[datetime]
) -> None:
    parent = store.upsert_object(conn, connector_id, ObjectMeta("email_thread", "t"))
    result = FetchResult(children=(ChildData(ObjectMeta("attachment", "t#1"), VersionData("v1")),))
    store.apply_fetch(conn, store.object_state(conn, parent), result)
    child_before = store.list_children(conn, parent)[0]
    outcome = store.apply_fetch(conn, store.object_state(conn, parent), result)
    child_after = store.list_children(conn, parent)[0]
    assert not outcome.changed
    assert outcome.child_ids == (child_before.id,)
    assert child_after.last_changed_at == child_before.last_changed_at
    assert child_after.last_checked_at > child_before.last_checked_at  # type: ignore[operator]


def test_apply_gone_marks_object_and_keeps_content(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    store.apply_fetch(
        conn,
        store.object_state(conn, object_id),
        FetchResult(version=VersionData("body"), entries=(_entry(),), cursor="c1", title="T"),
    )
    before = store.get_object(conn, object_id)
    outcome = store.apply_fetch(conn, store.object_state(conn, object_id), FetchResult(status="gone"))
    assert outcome.gone is True
    assert not outcome.changed
    assert outcome.child_ids == ()
    after = store.get_object(conn, object_id)
    assert (after.fetch_status, after.fetch_error) == ("gone", None)
    assert after.last_checked_at > before.last_checked_at  # type: ignore[operator]
    assert (after.last_changed_at, after.cursor, after.title) == (before.last_changed_at, "c1", "T")
    assert [v.text for v in store.list_versions(conn, object_id)] == ["body"]
    assert len(store.list_entries(conn, object_id)) == 1


def test_apply_passes_options_to_writers(
    conn: sqlalchemy.Connection, object_id: int, blobs: BlobStore, clock: list[datetime]
) -> None:
    run_id = conn.execute(
        sqlalchemy.text("INSERT INTO sync_run (trigger, scope) VALUES ('cli', 'all') RETURNING id")
    ).scalar_one()
    result = FetchResult(version=VersionData("v" * 30, raw=b"raw"), entries=(_entry(text="e" * 30),))
    store.apply_fetch(
        conn, store.object_state(conn, object_id), result, sync_run_id=run_id, blobs=blobs, max_text_chars=5
    )
    version = store.latest_version(conn, object_id)
    entry = store.list_entries(conn, object_id)[0]
    assert version.text == "v" * 5  # type: ignore[union-attr]
    assert entry.text == "e" * 5
    assert version.sync_run_id == entry.sync_run_id == run_id  # type: ignore[union-attr]
    assert blobs.get(conn, version.blob_hash) == b"raw"  # type: ignore[union-attr, arg-type]


# --- errors --------------------------------------------------------------------------------------------


def test_mark_error_keeps_stored_content(
    conn: sqlalchemy.Connection, object_id: int, clock: list[datetime]
) -> None:
    store.apply_fetch(
        conn,
        store.object_state(conn, object_id),
        FetchResult(version=VersionData("body"), entries=(_entry(),), cursor="c1"),
    )
    before = store.get_object(conn, object_id)
    store.mark_error(conn, object_id, "HTTP 503")
    after = store.get_object(conn, object_id)
    assert (after.fetch_status, after.fetch_error) == ("error", "HTTP 503")
    assert after.last_checked_at > before.last_checked_at  # type: ignore[operator]
    assert (after.cursor, after.last_changed_at) == (before.cursor, before.last_changed_at)
    assert len(store.list_versions(conn, object_id)) == 1
    assert len(store.list_entries(conn, object_id)) == 1
