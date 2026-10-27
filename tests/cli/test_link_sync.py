import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
import sqlalchemy

from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.connectors.outlook import OutlookConnector
from ai_oil.connectors.outlook_session import MailAddress, MailRecord
from ai_oil.db.engine import create_db_engine
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)

_KEY = re.compile(r"FAKE-(\d+)")


class FakeJira:
    """Recognises FAKE-<n> keys and returns one version and two comments on every fetch."""

    type: ClassVar[str] = "fakejira"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 2
    failing: ClassVar[set[str]] = set()

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("reachable", True, "ok"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        match = _KEY.fullmatch(text.strip())
        if match is None:
            return None
        return SourceRef(
            "jira_issue", match.group(1), display_ref=text.strip(), locator=f"https://fake/{text}"
        )

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(
            "jira_issue", ref.external_id, title=f"Fake issue {ref.external_id}", display_ref=ref.display_ref
        )

    def fetch(self, state: ObjectState) -> FetchResult:
        if state.external_id in self.failing:
            raise RuntimeError("boom")
        return FetchResult(
            version=VersionData("Summary with frobnicator trouble"),
            entries=(
                EntryData("jira_comment", "c1", "2026-10-01T10:00:00.000Z", "Supplier confirmed widgetry"),
                EntryData("jira_comment", "c2", "2026-10-02T10:00:00.000Z", "Second remark"),
            ),
            cursor="u1",
        )

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


@pytest.fixture(autouse=True)
def fakejira_registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FakeJira, "failing", set())
    registry.register_connector_type(FakeJira)
    yield
    registry.unregister_connector_type("fakejira")


@pytest.fixture
def add_connector(home):
    def add(name: str = "Fake") -> int:
        engine = create_db_engine(load_config(home).db_path)
        try:
            with engine.begin() as conn:
                return registry.create_connector(conn, "fakejira", name).id
        finally:
            engine.dispose()

    return add


def seed_issue(path: Path) -> None:
    assert main(["--home", str(path), "project", "add", "OIL", "Open issues"]) == 0
    assert (
        main(
            [
                "--home",
                str(path),
                "issue",
                "create",
                "-p",
                "OIL",
                "-s",
                "Fit the filter",
                "-d",
                "Needs a part",
            ]
        )
        == 0
    )


@pytest.fixture
def home_seed(request: pytest.FixtureRequest):
    return ("link-sync-issue", seed_issue) if "issue" in request.fixturenames else None


@pytest.fixture
def issue(home) -> str:
    return "OIL-1"


def run(oil, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = oil(*argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def rows(db, sql: str, **params: object):
    with db() as conn:
        return conn.execute(sqlalchemy.text(sql), params).all()


# --- oil link: reference path ----------------------------------------------------------------------------


def test_link_web_url_creates_web_link_and_runs_initial_sync(oil, db, capsys, issue) -> None:
    code, out, _ = run(oil, capsys, "link", issue, "HTTPS://Example.com/Spec")

    assert code == 0
    [obj] = rows(
        db, "SELECT id, object_type, external_id, display_ref, locator, fetch_status FROM source_object"
    )
    assert (obj.object_type, obj.external_id, obj.display_ref, obj.locator, obj.fetch_status) == (
        "web_link",
        "https://example.com/Spec",
        "example.com/Spec",
        "https://example.com/Spec",
        "ok",
    )
    assert out.startswith("Linked web_link: example.com/Spec to OIL-1\n")
    assert f"Sync source:{obj.id}: ok" in out
    with db() as conn:
        assert linking.linked_object_ids(conn, issue) == [obj.id]
        [link] = audit.list_entries(conn, action_prefix="source.link")
        [fetch] = audit.list_entries(conn, action_prefix="source.fetch")
    assert (link.action_class, link.actor_type, link.target) == ("B", "user", "OIL-1")
    assert link.detail["source_object_id"] == obj.id
    assert link.detail["label"] == "web_link: example.com/Spec"
    assert (fetch.action_class, fetch.actor_type) == ("A", "sync")


def test_link_by_connector_stores_content_and_shows_it(oil, db, capsys, issue, add_connector) -> None:
    add_connector()

    code, out, _ = run(
        oil, capsys, "link", issue, "FAKE-7", "--role", "evidence", "--note-text", "only part 2"
    )

    assert code == 0
    [obj] = rows(db, "SELECT id FROM source_object")
    assert out.splitlines()[0] == "Linked jira_issue: FAKE-7 to OIL-1"
    assert f"Sync source:{obj.id}: ok" in out
    assert "1 new versions, 2 new entries" in out
    with db() as conn:
        assert [v.text for v in store.list_versions(conn, obj.id)] == ["Summary with frobnicator trouble"]
        assert len(store.list_entries(conn, obj.id)) == 2
        [link] = linking.list_issue_sources(conn, issue)
    assert (link.role, link.note, link.link_method) == ("evidence", "only part 2", "manual")

    code, shown, _ = run(oil, capsys, "issue", "show", issue)
    assert code == 0
    assert re.search(
        rf"\nSources:\n  #{obj.id} jira_issue FAKE-7 \[evidence\] ok, checked 20\d\d-\d\d-\d\dT[\d:.]+Z\n",
        shown,
    )

    code, found, _ = run(oil, capsys, "search", 'text ~ "frobnicator"')
    assert code == 0
    assert "OIL-1" in found
    code, found, _ = run(oil, capsys, "search", 'text ~ "widgetry"')
    assert "OIL-1" in found


def test_issue_show_has_no_sources_section_without_links(oil, capsys, issue) -> None:
    _, out, _ = run(oil, capsys, "issue", "show", issue)

    assert "Sources:" not in out


def test_issue_show_sources_section_comes_between_links_and_comments(oil, capsys, issue) -> None:
    assert oil("issue", "create", "-p", "OIL", "-s", "Other", "-d", "D") == 0
    assert oil("issue-link", issue, "blocks", "OIL-2") == 0
    assert oil("link", issue, "https://example.com/a") == 0
    assert oil("comment", "add", issue, "hello") == 0
    capsys.readouterr()

    _, out, _ = run(oil, capsys, "issue", "show", issue)

    assert out.index("Links:") < out.index("Sources:") < out.index("Comments:")
    assert "  #1 web_link example.com/a [reference] ok, checked " in out


def test_link_ambiguous_reference_needs_connector_choice(oil, db, capsys, issue, add_connector) -> None:
    add_connector("FakeA")
    add_connector("FakeB")

    code, out, err = run(oil, capsys, "link", issue, "FAKE-1")
    assert code == 1
    assert out == ""
    assert "several connectors recognise 'FAKE-1'" in err
    assert "FakeA" in err and "FakeB" in err
    assert "--connector" in err
    assert rows(db, "SELECT id FROM source_object") == []

    code, out, _ = run(oil, capsys, "link", issue, "FAKE-1", "--connector", "FakeB")
    assert code == 0
    [obj] = rows(db, "SELECT c.name FROM source_object o JOIN connector c ON c.id = o.connector_id")
    assert obj.name == "FakeB"


def test_link_unknown_connector_option(oil, capsys, issue) -> None:
    code, _, err = run(oil, capsys, "link", issue, "FAKE-1", "--connector", "Nope")

    assert code == 1
    assert "Nope" in err


def test_link_without_match_is_rejected(oil, db, capsys, issue, add_connector) -> None:
    add_connector()

    code, _, err = run(oil, capsys, "link", issue, "no idea what this is")

    assert code == 1
    assert "no connector recognises 'no idea what this is'" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_connector_option_restricts_the_matching(oil, capsys, issue, add_connector) -> None:
    add_connector()

    code, _, err = run(oil, capsys, "link", issue, "FAKE-1", "--connector", "Manual")

    assert code == 1
    assert "no connector recognises 'FAKE-1'" in err


def test_link_same_source_twice_conflicts(oil, db, capsys, issue) -> None:
    assert oil("link", issue, "https://example.com/a") == 0
    capsys.readouterr()

    code, _, err = run(oil, capsys, "link", issue, "https://EXAMPLE.com/a")

    assert code == 1
    assert "already linked" in err
    assert len(rows(db, "SELECT id FROM source_object")) == 1


def test_link_initial_fetch_failure_keeps_the_link(oil, db, capsys, issue, add_connector) -> None:
    add_connector()
    FakeJira.failing.add("9")

    code, out, _ = run(oil, capsys, "link", issue, "FAKE-9")

    assert code == 0
    assert out.splitlines()[0] == "Linked jira_issue: FAKE-9 to OIL-1"
    assert "failed" in out and "RuntimeError: boom" in out
    with db() as conn:
        assert len(linking.list_issue_sources(conn, issue)) == 1


# --- oil link: note path ---------------------------------------------------------------------------------


def test_link_note_creates_note_without_sync(oil, db, capsys, issue) -> None:
    code, out, _ = run(
        oil,
        capsys,
        "link",
        issue,
        "--title",
        "Supplier call",
        "--text",
        "Lead time is 26 weeks",
        "--role",
        "origin",
    )

    assert (code, out) == (0, "Linked note: Supplier call to OIL-1\n")
    [obj] = rows(db, "SELECT id, object_type, title, fetch_status FROM source_object")
    assert (obj.object_type, obj.title, obj.fetch_status) == ("note", "Supplier call", "ok")
    assert rows(db, "SELECT id FROM sync_run") == []
    with db() as conn:
        [version] = store.list_versions(conn, obj.id)
        [link] = linking.list_issue_sources(conn, issue)
        [entry] = audit.list_entries(conn, action_prefix="source.link")
    assert version.text == "Lead time is 26 weeks"
    assert (link.role, link.connector_name) == ("origin", "Manual")
    assert (entry.action_class, entry.target, entry.detail["source_object_id"]) == ("B", "OIL-1", obj.id)
    assert entry.detail["label"] == "note: Supplier call"

    _, found, _ = run(oil, capsys, "search", 'text ~ "26 weeks"')
    assert "OIL-1" in found


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("--title", "Only a title"),
        ("--text", "Only a text"),
        ("https://example.com/a", "--title", "T", "--text", "X"),
        ("https://example.com/a", "--title", "T"),
    ],
)
def test_link_needs_a_reference_or_a_complete_note(oil, db, capsys, issue, argv: tuple[str, ...]) -> None:
    code, _, err = run(oil, capsys, "link", issue, *argv)

    assert code == 1
    assert "give a reference, or --title and --text for a note" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_note_rejects_blank_title_and_leaves_nothing(oil, db, capsys, issue) -> None:
    code, _, err = run(oil, capsys, "link", issue, "--title", " ", "--text", "text")

    assert code == 1
    assert "title" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_to_unknown_issue_rolls_the_note_back(oil, db, capsys, issue) -> None:
    code, _, _ = run(oil, capsys, "link", "OIL-99", "--title", "T", "--text", "X")

    assert code == 1
    assert rows(db, "SELECT id FROM source_object") == []


# --- oil unlink ------------------------------------------------------------------------------------------


def test_unlink_removes_the_link_and_audits(oil, db, capsys, issue) -> None:
    assert oil("link", issue, "https://example.com/a") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "unlink", issue, "1")

    assert (code, out) == (0, "Unlinked #1 from OIL-1\n")
    with db() as conn:
        assert linking.list_issue_sources(conn, issue) == []
        assert len(linking.list_issue_sources(conn, issue, include_removed=True)) == 1
        [entry] = audit.list_entries(conn, action_prefix="source.unlink")
    assert (entry.action_class, entry.target, entry.detail["source_object_id"]) == ("B", "OIL-1", 1)
    _, shown, _ = run(oil, capsys, "issue", "show", issue)
    assert "Sources:" not in shown

    code, _, err = run(oil, capsys, "unlink", issue, "1")
    assert code == 1
    assert "no active link" in err


# --- oil sync --------------------------------------------------------------------------------------------


@pytest.fixture
def linked(oil, capsys, issue, add_connector) -> str:
    add_connector()
    assert oil("link", issue, "FAKE-1") == 0
    assert oil("link", issue, "https://example.com/a") == 0
    capsys.readouterr()
    return issue


def test_sync_issue_scope(oil, db, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", linked)

    assert code == 0
    assert out.startswith("Sync issue:OIL-1: ok — 2 checked")
    assert rows(db, "SELECT scope FROM sync_run ORDER BY id DESC LIMIT 1")[0].scope == "issue:OIL-1"


def test_sync_issue_key_is_case_insensitive(oil, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", "oil-1")

    assert code == 0
    assert out.startswith("Sync issue:OIL-1: ok")


def test_sync_all(oil, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", "--all")

    assert code == 0
    assert out.startswith("Sync all: ok — 2 checked")


def test_sync_all_include_done(oil, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", "--all", "--include-done")

    assert code == 0
    assert out.startswith("Sync all: ok")


def test_sync_source(oil, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", "--source", "1")

    assert code == 0
    assert out.startswith("Sync source:1: ok — 1 checked")


def test_sync_connector(oil, capsys, linked) -> None:
    code, out, _ = run(oil, capsys, "sync", "--connector", "Fake")

    assert code == 0
    assert out.startswith("Sync connector:Fake: ok — 1 checked")


def test_sync_unknown_connector_and_source(oil, capsys, linked) -> None:
    code, _, err = run(oil, capsys, "sync", "--connector", "Nope")
    assert code == 1
    assert "Nope" in err

    code, _, err = run(oil, capsys, "sync", "--source", "99")
    assert code == 1
    assert "99" in err


def test_sync_without_scope_is_rejected(oil, capsys, linked) -> None:
    code, _, err = run(oil, capsys, "sync")

    assert code == 1
    assert "choose KEY, --source, --connector or --all" in err


def test_sync_with_two_scopes_is_rejected(oil, capsys, linked) -> None:
    code, _, err = run(oil, capsys, "sync", linked, "--all")

    assert code == 1
    assert "choose KEY, --source, --connector or --all" in err


def test_sync_include_done_needs_all(oil, capsys, linked) -> None:
    code, _, err = run(oil, capsys, "sync", linked, "--include-done")

    assert code == 1
    assert "--include-done" in err


def test_sync_exit_code_is_one_when_everything_fails(oil, capsys, issue, add_connector) -> None:
    add_connector()
    assert oil("link", issue, "FAKE-3") == 0
    capsys.readouterr()
    FakeJira.failing.add("3")

    code, out, _ = run(oil, capsys, "sync", issue)

    assert code == 1
    assert out.startswith("Sync issue:OIL-1: failed")
    assert "RuntimeError: boom" in out


def test_sync_partial_exits_zero(oil, capsys, linked) -> None:
    FakeJira.failing.add("1")

    code, out, _ = run(oil, capsys, "sync", linked)

    assert code == 0
    assert out.startswith("Sync issue:OIL-1: partial")


# --- oil note edit ---------------------------------------------------------------------------------------


@pytest.fixture
def note(oil, capsys, issue) -> int:
    assert oil("link", issue, "--title", "Call", "--text", "first") == 0
    capsys.readouterr()
    return 1


def test_note_edit_changed_text_makes_a_new_version(oil, db, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "note", "edit", str(note), "--text", "second")

    assert (code, out) == (0, "Updated note #1\n")
    with db() as conn:
        assert [v.text for v in store.list_versions(conn, note)] == ["first", "second"]
        [entry] = audit.list_entries(conn, action_prefix="note.edit")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "note: Call")
    assert entry.detail["source_object_id"] == note


def test_note_edit_unchanged_text(oil, db, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "note", "edit", str(note), "--text", "first")

    assert (code, out) == (0, "Note #1 unchanged\n")
    with db() as conn:
        assert len(store.list_versions(conn, note)) == 1


def test_note_edit_title_only_counts_as_update(oil, db, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "note", "edit", str(note), "--text", "first", "--title", "Renamed")

    assert (code, out) == (0, "Updated note #1\n")
    with db() as conn:
        assert store.get_object(conn, note).title == "Renamed"
        assert len(store.list_versions(conn, note)) == 1


def test_note_edit_of_a_web_link_is_rejected(oil, capsys, issue) -> None:
    assert oil("link", issue, "https://example.com/a") == 0
    capsys.readouterr()

    code, _, err = run(oil, capsys, "note", "edit", "1", "--text", "x")

    assert code == 1
    assert "not a note" in err


def test_note_edit_unknown_object(oil, capsys, issue) -> None:
    code, _, err = run(oil, capsys, "note", "edit", "42", "--text", "x")

    assert code == 1
    assert "42" in err


def test_edited_note_is_found_by_the_new_text(oil, capsys, note) -> None:
    assert oil("note", "edit", str(note), "--text", "Capacitor allocation confirmed") == 0
    capsys.readouterr()

    _, found, _ = run(oil, capsys, "search", 'text ~ "capacitor"')

    assert "OIL-1" in found


# --- oil link: a connector that cannot start is isolated (CON-3) -----------------------------------------


class BrokenConnector:
    """A connector type whose constructor fails, as a missing backend or a bad setting would."""

    type: ClassVar[str] = "broken"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 1

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        raise RuntimeError("cannot start")


@pytest.fixture
def add_broken_connector(home):
    registry.register_connector_type(BrokenConnector)

    def add(name: str = "Broken") -> int:
        engine = create_db_engine(load_config(home).db_path)
        try:
            with engine.begin() as conn:
                return registry.create_connector(conn, "broken", name).id
        finally:
            engine.dispose()

    yield add
    registry.unregister_connector_type("broken")


def test_link_url_still_works_when_another_connector_cannot_start(
    oil, db, capsys, issue, add_broken_connector
) -> None:
    add_broken_connector()

    code, out, _ = run(oil, capsys, "link", issue, "https://example.com/a")

    assert code == 0
    assert out.startswith("Linked web_link: example.com/a to OIL-1\n")
    assert len(rows(db, "SELECT id FROM source_object")) == 1


def test_link_with_a_connector_that_cannot_start_is_rejected(
    oil, db, capsys, issue, add_broken_connector
) -> None:
    add_broken_connector()

    code, _, err = run(oil, capsys, "link", issue, "FAKE-1", "--connector", "Broken")

    assert code == 1
    assert "connector Broken is not usable: RuntimeError: cannot start" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_without_match_names_the_skipped_connector(
    oil, capsys, issue, add_connector, add_broken_connector
) -> None:
    add_connector()
    add_broken_connector()

    code, _, err = run(oil, capsys, "link", issue, "no idea what this is")

    assert code == 1
    assert (
        "no connector recognises 'no idea what this is' (skipped: Broken: RuntimeError: cannot start)" in err
    )


def test_link_by_another_connector_works_when_one_cannot_start(
    oil, capsys, issue, add_connector, add_broken_connector
) -> None:
    add_connector()
    add_broken_connector()

    code, out, _ = run(oil, capsys, "link", issue, "FAKE-5")

    assert code == 0
    assert out.splitlines()[0] == "Linked jira_issue: FAKE-5 to OIL-1"


# --- oil link: Outlook options ---------------------------------------------------------------------------

_INBOX = "\\\\alex@example.com\\Inbox"


def outlook_mail(
    entry_id: str, conversation_id: str, subject: str, day: int, sender: str = "Jane"
) -> MailRecord:
    when = datetime(2026, 3, day, 10, 0, tzinfo=UTC)
    return MailRecord(
        entry_id=entry_id,
        store_id="S1",
        conversation_id=conversation_id,
        conversation_topic=subject,
        internet_message_id=f"<{entry_id.lower()}@acme.com>",
        in_reply_to=None,
        references=None,
        subject=subject,
        sender=MailAddress(sender, f"{sender.lower()}@acme.com"),
        to=(MailAddress("Alex", "alex@example.com"),),
        cc=(),
        sent_on=when,
        received_on=when,
        body=f"Body of {subject}",
        html_body="",
        folder_path=_INBOX,
        attachments=(),
    )


class FakeOutlookSession:
    """Scripted mailbox shared through class attributes, since the connector builds its own session."""

    records: ClassVar[list[MailRecord]] = []
    selected: ClassVar[list[MailRecord]] = []

    def folder_paths(self) -> list[str]:
        return [_INBOX]

    def selected_messages(self) -> list[MailRecord]:
        return list(self.selected)

    def find_by_subject(self, text: str, *, since: datetime | None, limit: int) -> list[MailRecord]:
        hits = [r for r in self.records if text.casefold() in r.subject.casefold()]
        return sorted(hits, key=lambda r: r.sent_on, reverse=True)[:limit]  # type: ignore[arg-type, return-value]

    def find_by_message_id(self, internet_message_id: str) -> MailRecord | None:
        return None

    def conversation(self, entry_id: str, store_id: str) -> list[MailRecord] | None:
        anchor = next((r for r in self.records if r.entry_id == entry_id), None)
        if anchor is None:
            return None
        return [r for r in self.records if r.conversation_id == anchor.conversation_id]

    def conversation_by_topic(self, conversation_id: str, topic: str) -> list[MailRecord]:
        return []

    def find_replies_to(self, internet_message_ids: Sequence[str]) -> list[MailRecord]:
        return []

    def recent_messages(self, *, since: datetime) -> list[MailRecord]:
        return []

    def attachment_bytes(self, entry_id: str, store_id: str, index: int) -> bytes:
        return b""

    def check(self) -> list:
        return []


class FakeSessionOutlook(OutlookConnector):
    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        super().__init__(config, session=FakeOutlookSession())


@pytest.fixture
def add_outlook(home, monkeypatch: pytest.MonkeyPatch):
    registry.register_connector_type(FakeSessionOutlook)
    monkeypatch.setattr(FakeOutlookSession, "records", [])
    monkeypatch.setattr(FakeOutlookSession, "selected", [])

    def add(name: str = "Outlook") -> int:
        engine = create_db_engine(load_config(home).db_path)
        try:
            with engine.begin() as conn:
                return registry.create_connector(conn, "outlook", name).id
        finally:
            engine.dispose()

    yield add
    registry.unregister_connector_type("outlook")


@pytest.fixture
def mailbox(add_outlook, monkeypatch: pytest.MonkeyPatch) -> list[MailRecord]:
    add_outlook()
    records = [
        outlook_mail("A1", "CA", "Inductor lead time", 1),
        outlook_mail("A2", "CA", "RE: Inductor lead time", 4, sender="Bob"),
        outlook_mail("B1", "CB", "Inductor sample", 5),
        outlook_mail("C1", "CC", "Capacitor quote", 6),
    ]
    monkeypatch.setattr(FakeOutlookSession, "records", records)
    return records


def test_link_outlook_selected_links_the_conversation_and_syncs_it(
    oil, db, capsys, issue, mailbox, monkeypatch
) -> None:
    monkeypatch.setattr(FakeOutlookSession, "selected", [mailbox[1]])

    code, out, _ = run(oil, capsys, "link", issue, "--outlook-selected", "--role", "evidence")

    assert code == 0
    [obj] = rows(
        db, "SELECT id, object_type, external_id, display_ref, locator, fetch_status FROM source_object"
    )
    assert (obj.object_type, obj.external_id, obj.display_ref, obj.locator, obj.fetch_status) == (
        "email_thread",
        "CA",
        "Inductor lead time",
        "outlook:S1:A2",
        "ok",
    )
    assert out.splitlines()[0] == "Linked email_thread: Inductor lead time to OIL-1"
    assert "2 new entries" in out
    with db() as conn:
        [link] = linking.list_issue_sources(conn, issue)
        [entry] = audit.list_entries(conn, action_prefix="source.link")
        assert [e.external_id for e in store.list_entries(conn, obj.id)] == ["<a1@acme.com>", "<a2@acme.com>"]
    assert (link.role, link.link_method) == ("evidence", "manual")
    assert (entry.action_class, entry.target) == ("B", "OIL-1")


def test_link_outlook_selected_without_a_selection_is_rejected(oil, db, capsys, issue, mailbox) -> None:
    code, _, err = run(oil, capsys, "link", issue, "--outlook-selected")

    assert code == 1
    assert "no email is selected in Outlook" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_outlook_subject_with_one_match_links_it(oil, db, capsys, issue, mailbox) -> None:
    code, out, _ = run(oil, capsys, "link", issue, "--outlook-subject", "capacitor")

    assert code == 0
    [obj] = rows(db, "SELECT external_id, display_ref FROM source_object")
    assert (obj.external_id, obj.display_ref) == ("CC", "Capacitor quote")
    assert out.splitlines()[0] == "Linked email_thread: Capacitor quote to OIL-1"


def test_link_outlook_subject_with_several_matches_lists_them(oil, db, capsys, issue, mailbox) -> None:
    code, out, err = run(oil, capsys, "link", issue, "--outlook-subject", "inductor")

    assert code == 1
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "Several conversations match; re-run with --pick N:"
    assert re.fullmatch(r"  1\. Inductor sample — 2026-03-0\d \d\d:\d\d — Jane \(1 messages\)", lines[1]), (
        lines[1]
    )
    assert re.fullmatch(r"  2\. Inductor lead time — 2026-03-0\d \d\d:\d\d — Bob \(2 messages\)", lines[2]), (
        lines[2]
    )
    assert len(lines) == 3
    assert rows(db, "SELECT id FROM source_object") == []
    assert rows(db, "SELECT id FROM issue_source_link") == []


def test_link_outlook_subject_with_pick_links_the_chosen_conversation(
    oil, db, capsys, issue, mailbox
) -> None:
    code, out, _ = run(oil, capsys, "link", issue, "--outlook-subject", "inductor", "--pick", "2")

    assert code == 0
    [obj] = rows(db, "SELECT external_id FROM source_object")
    assert obj.external_id == "CA"
    assert out.splitlines()[0] == "Linked email_thread: Inductor lead time to OIL-1"


@pytest.mark.parametrize("pick", ["3", "0", "-1"])
def test_link_outlook_subject_pick_out_of_range(oil, db, capsys, issue, mailbox, pick: str) -> None:
    code, _, err = run(oil, capsys, "link", issue, "--outlook-subject", "inductor", "--pick", pick)

    assert code == 1
    assert "--pick" in err
    assert rows(db, "SELECT id FROM source_object") == []


def test_link_outlook_subject_without_a_hit_is_rejected(oil, db, capsys, issue, mailbox) -> None:
    code, _, err = run(oil, capsys, "link", issue, "--outlook-subject", "gearbox")

    assert code == 1
    assert "no email conversation matches 'gearbox'" in err
    assert rows(db, "SELECT id FROM source_object") == []


@pytest.mark.parametrize("option", ["--outlook-selected", "--outlook-subject"])
def test_link_outlook_without_an_outlook_connector_is_rejected(
    oil, db, capsys, issue, add_connector, option: str
) -> None:
    add_connector()
    argv = ["link", issue, option] + (["x"] if option == "--outlook-subject" else [])

    code, _, err = run(oil, capsys, *argv)

    assert code == 1
    assert "no enabled Outlook connector — add one with `oil connector add outlook`" in err


def test_link_outlook_with_several_connectors_needs_a_choice(
    oil, db, capsys, issue, mailbox, add_outlook
) -> None:
    add_outlook("Second")

    code, _, err = run(oil, capsys, "link", issue, "--outlook-subject", "capacitor")
    assert code == 1
    assert "several Outlook connectors are enabled (Outlook, Second)" in err

    code, out, _ = run(oil, capsys, "link", issue, "--outlook-subject", "capacitor", "--connector", "Second")
    assert code == 0
    [obj] = rows(db, "SELECT c.name FROM source_object o JOIN connector c ON c.id = o.connector_id")
    assert obj.name == "Second"


def test_link_outlook_with_a_connector_of_another_type_is_rejected(
    oil, db, capsys, issue, mailbox, add_connector
) -> None:
    add_connector("Jira")

    code, _, err = run(oil, capsys, "link", issue, "--outlook-selected", "--connector", "Jira")

    assert code == 1
    assert "connector Jira is not an Outlook connector" in err


@pytest.mark.parametrize(
    "argv",
    [
        ("--outlook-selected", "--outlook-subject", "x"),
        ("https://example.com/a", "--outlook-selected"),
        ("https://example.com/a", "--outlook-subject", "x"),
        ("--outlook-selected", "--title", "T", "--text", "X"),
        ("--outlook-subject", "x", "--title", "T", "--text", "X"),
        ("--pick", "1"),
        ("https://example.com/a", "--pick", "1"),
        ("--outlook-selected", "--pick", "1"),
    ],
)
def test_link_outlook_options_are_mutually_exclusive(
    oil, db, capsys, issue, mailbox, argv: tuple[str, ...]
) -> None:
    code, _, err = run(oil, capsys, "link", issue, *argv)

    assert code == 1
    assert "--outlook-selected" in err or "--pick" in err
    assert rows(db, "SELECT id FROM source_object") == []
