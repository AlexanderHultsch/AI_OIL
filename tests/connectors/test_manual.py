import pytest
import sqlalchemy

from ai_oil.connectors import manual
from ai_oil.connectors.manual import MANUAL_CONNECTOR_NAME, ManualConnector
from ai_oil.errors import ValidationError
from ai_oil.search import fts
from ai_oil.sources import registry, store
from ai_oil.sources.model import ConnectorConfig, FetchResult, ObjectMeta, ObjectState, SourceRef


@pytest.fixture
def connector() -> ManualConnector:
    return ManualConnector(ConnectorConfig(1, "manual", "Manual", True, {}, None))


def state(object_id: int = 1) -> ObjectState:
    return ObjectState(object_id, 1, "note", "x", None, None, None, None, None, None)


def test_class_attributes() -> None:
    assert ManualConnector.type == "manual"
    assert ManualConnector.object_types == frozenset({"note", "web_link"})
    assert ManualConnector.action_class == "A"
    assert ManualConnector.max_parallel == 4


def test_entry_point_is_visible() -> None:
    assert registry.available_connector_types()["manual"] is ManualConnector


def test_check_is_always_ok(connector: ManualConnector) -> None:
    report = connector.check()
    assert report.ok
    assert [(c.name, c.ok, c.detail) for c in report.checks] == [("manual", True, "always available")]


def test_parse_https_normalises_scheme_and_host(connector: ManualConnector) -> None:
    ref = connector.parse_reference("  HTTPS://Example.COM/Some/Path?Q=A#Frag  ")

    url = "https://example.com/Some/Path?Q=A#Frag"
    assert ref == SourceRef("web_link", url, display_ref="example.com/Some/Path", locator=url)


def test_parse_http_and_port_and_userinfo(connector: ManualConnector) -> None:
    ref = connector.parse_reference("Http://User:Pw@Host.Example:8080/a")

    assert ref is not None
    assert ref.external_id == "http://User:Pw@host.example:8080/a"
    assert ref.display_ref == "host.example/a"


def test_parse_host_only(connector: ManualConnector) -> None:
    ref = connector.parse_reference("https://Example.com")

    assert ref is not None
    assert (ref.external_id, ref.display_ref) == ("https://example.com", "example.com")


def test_display_ref_is_limited_to_80_characters(connector: ManualConnector) -> None:
    ref = connector.parse_reference("https://example.com/" + "a" * 200)

    assert ref is not None
    assert len(ref.display_ref or "") == 80


@pytest.mark.parametrize("text", ["PROJ-123", "C:\\files\\a.docx", "ftp://example.com/a", "", "example.com"])
def test_parse_rejects_non_urls(connector: ManualConnector, text: str) -> None:
    assert connector.parse_reference(text) is None


@pytest.mark.parametrize("text", ["https://", "http:///path", "https://?q=1", "https://   "])
def test_parse_rejects_url_without_host(connector: ManualConnector, text: str) -> None:
    assert connector.parse_reference(text) is None


def test_resolve_web_link(connector: ManualConnector) -> None:
    ref = SourceRef(
        "web_link", "https://example.com/a", display_ref="example.com/a", locator="https://example.com/a"
    )

    assert connector.resolve(ref) == ObjectMeta(
        "web_link",
        "https://example.com/a",
        title="https://example.com/a",
        display_ref="example.com/a",
        locator="https://example.com/a",
    )


def test_resolve_other_type_rejected(connector: ManualConnector) -> None:
    with pytest.raises(ValidationError, match="jira_issue"):
        connector.resolve(SourceRef("jira_issue", "10001"))


def test_fetch_returns_empty_ok_result(connector: ManualConnector) -> None:
    assert connector.fetch(state()) == FetchResult()


def test_search_candidates_is_empty(connector: ManualConnector) -> None:
    from ai_oil.sources.model import CandidateQuery

    assert connector.search_candidates(CandidateQuery("anything")) == []


def test_ensure_manual_connector_is_idempotent(conn: sqlalchemy.Connection) -> None:
    first = manual.ensure_manual_connector(conn)
    second = manual.ensure_manual_connector(conn)

    assert first == second
    assert (first.name, first.type, first.enabled) == (MANUAL_CONNECTOR_NAME, "manual", True)
    assert len(registry.list_connectors(conn)) == 1


def test_create_note_stores_first_version(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "  Supplier call  ", "Lead time is 26 weeks")

    obj = store.get_object(conn, object_id)
    assert (obj.object_type, obj.title, obj.display_ref) == ("note", "Supplier call", "Supplier call")
    assert (obj.fetch_status, obj.fetch_error) == ("ok", None)
    assert obj.last_checked_at is not None
    assert obj.last_changed_at == obj.last_checked_at
    assert len(obj.external_id) == 32
    [version] = store.list_versions(conn, object_id)
    assert (version.version_no, version.text) == (1, "Lead time is 26 weeks")
    assert registry.get_connector(conn, obj.connector_id).name == MANUAL_CONNECTOR_NAME


def test_create_note_twice_gives_distinct_objects(conn: sqlalchemy.Connection) -> None:
    assert manual.create_note(conn, "Same", "text") != manual.create_note(conn, "Same", "text")


@pytest.mark.parametrize(("title", "text"), [("", "text"), ("  ", "text"), ("Title", ""), ("Title", "  \n")])
def test_create_note_rejects_blank_input(conn: sqlalchemy.Connection, title: str, text: str) -> None:
    with pytest.raises(ValidationError):
        manual.create_note(conn, title, text)


def test_edit_note_unchanged_text_writes_nothing(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "Call", "first")

    assert manual.edit_note(conn, object_id, "first") is None
    assert len(store.list_versions(conn, object_id)) == 1


def test_edit_note_changed_text_is_a_new_version(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "Call", "first")
    conn.execute(
        sqlalchemy.text(
            "UPDATE source_object SET last_changed_at = '2000-01-01T00:00:00.000Z' WHERE id = :id"
        ),
        {"id": object_id},
    )

    version_id = manual.edit_note(conn, object_id, "second")

    assert version_id is not None
    assert [(v.version_no, v.text) for v in store.list_versions(conn, object_id)] == [
        (1, "first"),
        (2, "second"),
    ]
    assert store.get_object(conn, object_id).last_changed_at != "2000-01-01T00:00:00.000Z"


def test_edit_note_updates_title(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "Call", "first")

    assert manual.edit_note(conn, object_id, "first", title=" Supplier call ") is None

    obj = store.get_object(conn, object_id)
    assert (obj.title, obj.display_ref) == ("Supplier call", "Supplier call")


def test_edit_note_rejects_blank_text_and_title(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "Call", "first")

    with pytest.raises(ValidationError):
        manual.edit_note(conn, object_id, " ")
    with pytest.raises(ValidationError):
        manual.edit_note(conn, object_id, "second", title=" ")


def test_edit_of_a_web_link_is_rejected(conn: sqlalchemy.Connection) -> None:
    row = manual.ensure_manual_connector(conn)
    link_id = store.upsert_object(conn, row.id, ObjectMeta("web_link", "https://example.com/a"))

    with pytest.raises(ValidationError, match="not a note"):
        manual.edit_note(conn, link_id, "text")


def test_notes_are_found_by_full_text_search(conn: sqlalchemy.Connection) -> None:
    object_id = manual.create_note(conn, "Supplier call", "Inductor lead time is 26 weeks")

    [hit] = fts.search_docs(conn, "inductor")
    assert hit.doc_kind == "source_version"
    assert hit.title == "Supplier call"

    manual.edit_note(conn, object_id, "Capacitor allocation confirmed")
    assert [h.doc_kind for h in fts.search_docs(conn, "capacitor")] == ["source_version"]
