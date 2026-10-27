import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.connectors.outlook import OutlookConnector
from ai_oil.connectors.outlook_session import MailAddress, MailRecord
from ai_oil.domain import people
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

HTMX = {"HX-Request": "true"}
ERROR_PREFIX = '<div class="error" role="alert">'
_KEY = re.compile(r"FAKE-(\d+)")


class FakeJira:
    """Recognises FAKE-<n> keys and returns one version and two comments on every fetch."""

    type: ClassVar[str] = "fakejira"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 2
    failing: ClassVar[set[str]] = set()
    summary: ClassVar[str] = "Summary with frobnicator trouble"

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("reachable", True, "ok"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        match = _KEY.fullmatch(text.strip())
        if match is None:
            return None
        return SourceRef(
            "jira_issue", match.group(1), display_ref=text.strip(), locator=f"https://fake/{text.strip()}"
        )

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(
            "jira_issue",
            ref.external_id,
            title=f"Fake issue {ref.external_id}",
            display_ref=ref.display_ref,
            locator=ref.locator,
        )

    def fetch(self, state: ObjectState) -> FetchResult:
        if state.external_id in self.failing:
            raise RuntimeError("boom")
        return FetchResult(
            version=VersionData(self.summary),
            entries=(
                EntryData("jira_comment", "c1", "2026-10-01T10:00:00.000Z", "Supplier confirmed widgetry"),
                EntryData("jira_comment", "c2", "2026-10-02T10:00:00.000Z", "Second remark"),
            ),
            cursor="u1",
        )

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


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


@pytest.fixture(autouse=True)
def fakejira_registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FakeJira, "failing", set())
    monkeypatch.setattr(FakeJira, "summary", "Summary with frobnicator trouble")
    registry.register_connector_type(FakeJira)
    yield
    registry.unregister_connector_type("fakejira")


@pytest.fixture
def token(detail_app: FastAPI) -> str:
    return detail_app.state.csrf.token


@pytest.fixture
def post(detail_client: TestClient, token: str):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        headers = {**HTMX, **({"X-CSRF-Token": token} if csrf else {})}
        return detail_client.post(path, data=data or {}, headers=headers, follow_redirects=False)

    return send


@pytest.fixture
def add_connector(detail_app: FastAPI):
    def add(connector_type: str = "fakejira", name: str = "Fake") -> int:
        with detail_app.state.engine.begin() as conn:
            return registry.create_connector(conn, connector_type, name).id

    return add


@pytest.fixture
def mailbox(detail_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MailRecord]]:
    registry.register_connector_type(FakeSessionOutlook)
    records = [
        outlook_mail("A1", "CA", "Inductor lead time", 1),
        outlook_mail("A2", "CA", "RE: Inductor lead time", 4, sender="Bob"),
        outlook_mail("B1", "CB", "Inductor sample", 5),
        outlook_mail("C1", "CC", "Capacitor quote", 6),
    ]
    monkeypatch.setattr(FakeOutlookSession, "records", records)
    monkeypatch.setattr(FakeOutlookSession, "selected", [])
    with detail_app.state.engine.begin() as conn:
        registry.create_connector(conn, "outlook", "Outlook")
    yield records
    registry.unregister_connector_type("outlook")


def rows(app: FastAPI, sql: str, **params: object):
    with app.state.engine.begin() as conn:
        return conn.execute(text(sql), params).all()


def entries(app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def local(stamp: str) -> str:
    return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def panel_of(page: str) -> str:
    match = re.search(r'<section class="panel" id="sources-panel">.*?</section>', page, re.DOTALL)
    assert match is not None
    return match.group(0)


def entry_of(fragment: str, source_id: int) -> str:
    match = re.search(rf'<li class="source" id="source-{source_id}">.*?</li>', fragment, re.DOTALL)
    assert match is not None, f"no entry for source {source_id}"
    return match.group(0)


def status_of(entry: str) -> tuple[str, str | None]:
    match = re.search(r'<span class="source-status[^>]*?(?: title="([^"]*)")?>(.*?)</span>', entry)
    assert match is not None
    return match.group(2), match.group(1)


def seed(
    app: FastAPI,
    object_type: str,
    external_id: str,
    *,
    key: str = "OIL-2",
    status: str = "ok",
    error: str | None = None,
    locator: str | None = None,
    title: str | None = None,
    display_ref: str | None = None,
    role: str = "reference",
    checked: str | None = None,
    changed: str | None = None,
) -> int:
    """Link a stored object of a connector that is never instantiated (the page only reads it)."""
    with app.state.engine.begin() as conn:
        connector_id = conn.execute(
            text("SELECT id FROM connector WHERE name = 'Seeded'")
        ).scalar_one_or_none()
        if connector_id is None:
            connector_id = conn.execute(
                text("INSERT INTO connector (type, name) VALUES ('seeded', 'Seeded') RETURNING id")
            ).scalar_one()
        meta = ObjectMeta(object_type, external_id, title=title, display_ref=display_ref, locator=locator)
        object_id = store.upsert_object(conn, connector_id, meta)
        conn.execute(
            text(
                "UPDATE source_object SET fetch_status = :status, fetch_error = :error,"
                " last_checked_at = :checked, last_changed_at = :changed WHERE id = :id"
            ),
            {"status": status, "error": error, "checked": checked, "changed": changed, "id": object_id},
        )
        linking.link_source(conn, key, object_id, role=role, actor_id=people.get_me(conn).id)
    return object_id


# --- the panel ---------------------------------------------------------------------------------------------


def test_the_page_has_the_panel_with_its_buttons_and_the_empty_text(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-4").text
    panel = panel_of(page)

    assert re.search(r"<h2>Sources</h2>", panel)
    assert 'hx-post="/issues/OIL-4/sources/refresh"' in panel
    assert ">Refresh all</button>" in panel
    assert "<summary>Link source</summary>" in panel
    assert "No sources linked yet." in panel
    assert "Coming soon" not in page


def test_sources_are_grouped_by_type_in_the_fixed_order(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    web = seed(detail_app, "web_link", "https://example.com/a", display_ref="example.com/a")
    other = seed(detail_app, "teams_thread", "t1", display_ref="Teams chat")
    note = seed(detail_app, "note", "n1", title="Meeting note")
    sharepoint = seed(detail_app, "file", "f1", display_ref="spec.xlsx")
    jira = seed(detail_app, "jira_issue", "HWDEV-1187", display_ref="HWDEV-1187")
    confluence = seed(detail_app, "confluence_page", "p1", title="Design page")
    email = seed(detail_app, "email_thread", "c1", display_ref="Inductor lead time")
    jira_two = seed(detail_app, "jira_issue", "HWDEV-1188", display_ref="HWDEV-1188")

    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    headings = re.findall(r'<h3 class="source-group">(.*?)</h3>', panel)
    assert headings == ["Email", "Jira", "Confluence", "SharePoint / OneDrive", "Notes", "Web links", "Other"]
    order = [int(i) for i in re.findall(r'<li class="source" id="source-(\d+)">', panel)]
    assert order == [email, jira, jira_two, confluence, sharepoint, note, web, other]
    assert "No sources linked yet." not in panel


def test_groups_without_sources_are_left_out(detail_app: FastAPI, detail_client: TestClient) -> None:
    seed(detail_app, "note", "n1", title="Meeting note")

    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    assert re.findall(r'<h3 class="source-group">(.*?)</h3>', panel) == ["Notes"]


def test_an_entry_shows_label_role_connector_times_and_counts(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    checked = "2026-10-20T08:00:00.000Z"
    changed = "2026-10-19T07:30:00.000Z"
    shown = seed(
        detail_app,
        "jira_issue",
        "HWDEV-1187",
        display_ref="HWDEV-1187",
        title="Inductor",
        role="evidence",
        checked=checked,
        changed=changed,
    )
    fresh = seed(detail_app, "note", "n1", title="Never fetched", status="pending")
    only_title = seed(detail_app, "web_link", "w1", title="A title")
    only_id = seed(detail_app, "confluence_page", "page-9")

    page = panel_of(detail_client.get("/issues/OIL-2").text)

    entry = entry_of(page, shown)
    assert f'<a class="source-label" href="/sources/{shown}">HWDEV-1187</a>' in entry
    assert '<span class="badge">evidence</span>' in entry
    assert "Seeded" in entry
    assert f"checked {local(checked)}" in entry
    assert f"changed {local(changed)}" in entry
    assert "0 versions · 0 entries" in entry
    assert f'hx-post="/issues/OIL-2/sources/{shown}/refresh"' in entry
    assert f'hx-post="/issues/OIL-2/sources/{shown}/unlink"' in entry
    assert 'hx-confirm="Unlink this source?"' in entry
    assert "checked never" in entry_of(page, fresh) and "changed never" in entry_of(page, fresh)
    assert f'href="/sources/{only_title}">A title</a>' in entry_of(page, only_title)
    assert f'href="/sources/{only_id}">page-9</a>' in entry_of(page, only_id)


def test_the_label_is_escaped(detail_app: FastAPI, detail_client: TestClient) -> None:
    seed(detail_app, "note", "n1", title="<b>bold</b>")

    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    assert "&lt;b&gt;bold&lt;/b&gt;" in panel
    assert "<b>bold</b>" not in panel


def test_status_icons_and_tooltips(detail_app: FastAPI, detail_client: TestClient) -> None:
    ok = seed(detail_app, "jira_issue", "A-1", status="ok")
    error = seed(detail_app, "jira_issue", "A-2", status="error", error="HTTP 503 from Jira")
    gone = seed(detail_app, "jira_issue", "A-3", status="gone")
    pending = seed(detail_app, "jira_issue", "A-4", status="pending")
    skipped = seed(detail_app, "jira_issue", "A-5", status="skipped")

    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    assert status_of(entry_of(panel, ok)) == ("✓", None)
    assert status_of(entry_of(panel, error)) == ("⚠", "HTTP 503 from Jira")
    assert status_of(entry_of(panel, gone)) == ("✕", "gone in the source system")
    assert status_of(entry_of(panel, pending)) == ("…", None)
    assert status_of(entry_of(panel, skipped)) == ("–", "connector disabled")


def test_open_in_source_only_for_web_locators_and_file_paths_in_code(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    web = seed(detail_app, "jira_issue", "A-1", locator="https://jira.example.com/browse/A-1")
    plain_http = seed(detail_app, "web_link", "w", locator="http://intranet/page")
    outlook = seed(detail_app, "email_thread", "c1", locator="outlook:S1:A2")
    path = seed(detail_app, "file", "f1", locator="C:\\Users\\Alex\\OneDrive\\spec <v2>.xlsx")
    url_file = seed(detail_app, "file", "f2", locator="https://sharepoint.example.com/spec.xlsx")
    bare = seed(detail_app, "note", "n1", title="No locator")

    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    link = '<a class="source-open" href="%s" target="_blank" rel="noopener">Open in source</a>'
    assert link % "https://jira.example.com/browse/A-1" in entry_of(panel, web)
    assert link % "http://intranet/page" in entry_of(panel, plain_http)
    assert link % "https://sharepoint.example.com/spec.xlsx" in entry_of(panel, url_file)
    assert "<code" not in entry_of(panel, url_file)
    for source in (outlook, path, bare):
        assert "Open in source" not in entry_of(panel, source)
    assert "<code" not in entry_of(panel, outlook)
    assert "<code" not in entry_of(panel, bare)
    assert '<code class="source-path">C:\\Users\\Alex\\OneDrive\\spec &lt;v2&gt;.xlsx</code>' in entry_of(
        panel, path
    )


# --- refresh -----------------------------------------------------------------------------------------------


def link_fake(post, number: int = 7, key: str = "OIL-2") -> int:
    response = post(f"/issues/{key}/sources/link", {"reference": f"FAKE-{number}"})
    assert response.status_code == 200, response.text
    return int(re.findall(r'<li class="source" id="source-(\d+)">', response.text)[-1])


def test_refreshing_one_source_stores_new_content_and_shows_the_summary(
    detail_app: FastAPI, add_connector, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_connector()
    source = link_fake(post)
    monkeypatch.setattr(FakeJira, "summary", "Summary after the supplier answered")

    response = post(f"/issues/OIL-2/sources/{source}/refresh")

    assert response.status_code == 200
    assert response.text.startswith('<section class="panel" id="sources-panel">')
    summary = re.search(r'<div class="sync-summary.*?</div>', response.text, re.DOTALL)
    assert summary is not None
    assert f"Sync source:{source}: ok — 1 checked, 1 changed, 1 new versions, 0 new entries" in plain(
        summary.group(0)
    )
    assert "changed jira_issue: FAKE-7 [Fake]" in plain(summary.group(0))
    assert "2 versions · 2 entries" in entry_of(response.text, source)
    with detail_app.state.engine.begin() as conn:
        assert [v.text for v in store.list_versions(conn, source)] == [
            "Summary with frobnicator trouble",
            "Summary after the supplier answered",
        ]
    runs = rows(detail_app, "SELECT trigger, scope, status FROM sync_run ORDER BY id")
    assert [tuple(run) for run in runs] == [
        ("ui", f"source:{source}", "ok"),
        ("ui", f"source:{source}", "ok"),
    ]


def test_an_unchanged_refresh_shows_only_the_first_line(detail_app: FastAPI, add_connector, post) -> None:
    add_connector()
    source = link_fake(post)

    response = post(f"/issues/OIL-2/sources/{source}/refresh")

    assert "1 checked, 0 changed, 0 new versions, 0 new entries, 0 errors" in response.text
    assert "<li>" not in re.search(r'<div class="sync-summary.*?</div>', response.text, re.DOTALL).group(0)


def test_refresh_all_syncs_every_source_of_the_issue_and_reports_failures(
    detail_app: FastAPI, add_connector, post
) -> None:
    add_connector()
    good = link_fake(post, 1)
    bad = link_fake(post, 2)
    other_issue = link_fake(post, 3, key="OIL-4")
    FakeJira.failing.add("2")

    response = post("/issues/OIL-2/sources/refresh")

    assert response.status_code == 200
    summary = plain(re.search(r'<div class="sync-summary.*?</div>', response.text, re.DOTALL).group(0))
    assert "Sync issue:OIL-2: partial — 2 checked" in summary
    assert "1 errors" in summary
    assert "error jira_issue: FAKE-2 [Fake]: RuntimeError: boom" in summary
    assert status_of(entry_of(response.text, good)) == ("✓", None)
    assert status_of(entry_of(response.text, bad)) == ("⚠", "RuntimeError: boom")
    assert str(other_issue) not in "".join(re.findall(r'id="source-(\d+)"', response.text))
    scopes = [tuple(r) for r in rows(detail_app, "SELECT trigger, scope FROM sync_run ORDER BY id")]
    assert scopes[-1] == ("ui", "issue:OIL-2")
    results = rows(
        detail_app,
        "SELECT source_object_id FROM sync_result WHERE sync_run_id = (SELECT MAX(id) FROM sync_run)",
    )
    assert sorted(r[0] for r in results) == sorted([good, bad])


def test_refreshing_a_source_that_is_not_linked_to_the_issue_is_a_404(
    detail_app: FastAPI, add_connector, post
) -> None:
    add_connector()
    elsewhere = link_fake(post, 5, key="OIL-4")
    runs_before = rows(detail_app, "SELECT id FROM sync_run")

    response = post(f"/issues/OIL-2/sources/{elsewhere}/refresh")

    assert response.status_code == 404
    assert response.text.startswith(ERROR_PREFIX)
    assert rows(detail_app, "SELECT id FROM sync_run") == runs_before
    assert post("/issues/OIL-2/sources/9999/refresh").status_code == 404
    assert post("/issues/NOPE-1/sources/refresh").status_code == 404


def test_a_removed_link_cannot_be_refreshed(detail_app: FastAPI, add_connector, post) -> None:
    add_connector()
    source = link_fake(post)
    assert post(f"/issues/OIL-2/sources/{source}/unlink").status_code == 200

    assert post(f"/issues/OIL-2/sources/{source}/refresh").status_code == 404


# --- unlink ------------------------------------------------------------------------------------------------


def test_unlinking_removes_the_entry_and_audits(detail_app: FastAPI, post) -> None:
    keep = seed(detail_app, "note", "n1", title="Stays")
    gone = seed(detail_app, "web_link", "https://example.com/x", display_ref="example.com/x")

    response = post(f"/issues/OIL-2/sources/{gone}/unlink")

    assert response.status_code == 200
    assert f'id="source-{gone}"' not in response.text
    assert f'id="source-{keep}"' in response.text
    [entry] = entries(detail_app, "source.unlink")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "OIL-2")
    assert entry.detail == {"source_object_id": gone, "label": "web_link: example.com/x", "via": "web"}
    [removed] = rows(
        detail_app,
        "SELECT removed_at FROM issue_source_link WHERE source_object_id = :id",
        id=gone,
    )
    assert removed[0] is not None
    assert rows(detail_app, "SELECT id FROM source_object WHERE id = :id", id=gone)


def test_unlinking_the_last_source_shows_the_empty_text(detail_app: FastAPI, post) -> None:
    only = seed(detail_app, "note", "n1", title="Only")

    response = post(f"/issues/OIL-2/sources/{only}/unlink")

    assert "No sources linked yet." in response.text


def test_unlinking_twice_or_a_foreign_source_is_a_404(detail_app: FastAPI, post) -> None:
    mine = seed(detail_app, "note", "n1", title="Mine")
    foreign = seed(detail_app, "note", "n2", title="Foreign", key="OIL-4")
    assert post(f"/issues/OIL-2/sources/{mine}/unlink").status_code == 200

    assert post(f"/issues/OIL-2/sources/{mine}/unlink").status_code == 404
    assert post(f"/issues/OIL-2/sources/{foreign}/unlink").status_code == 404
    assert len(entries(detail_app, "source.unlink")) == 1


# --- the link dialog: reference ----------------------------------------------------------------------------


def test_previewing_a_web_url_shows_the_manual_connector_and_stores_nothing(
    detail_app: FastAPI, post
) -> None:
    response = post(
        "/issues/OIL-2/sources/preview", {"reference": "HTTPS://Example.com/Spec", "role": "evidence"}
    )

    assert response.status_code == 200
    text_ = plain(response.text)
    assert "Connector Manual (manual)" in text_
    assert "Type web_link" in text_
    assert "Title https://example.com/Spec" in text_
    assert "Reference example.com/Spec" in text_
    assert "Locator https://example.com/Spec" in text_
    form = re.search(r'<form hx-post="/issues/OIL-2/sources/link".*?</form>', response.text, re.DOTALL)
    assert form is not None
    assert 'hx-target="#sources-panel"' in form.group(0)
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form.group(0)))
    assert hidden == {
        "mode": "reference",
        "reference": "HTTPS://Example.com/Spec",
        "role": "evidence",
        "note_text": "",
    }
    assert ">Link</button>" in form.group(0)
    assert rows(detail_app, "SELECT id FROM source_object") == []
    assert rows(detail_app, "SELECT id FROM issue_source_link") == []


def test_previewing_a_connector_key(add_connector, post) -> None:
    add_connector()

    response = post("/issues/OIL-2/sources/preview", {"reference": "FAKE-7", "note_text": "part 2 only"})

    text_ = plain(response.text)
    assert "Connector Fake (fakejira)" in text_
    assert "Type jira_issue" in text_
    assert "Title Fake issue 7" in text_
    assert "Reference FAKE-7" in text_
    assert "Locator https://fake/FAKE-7" in text_
    assert 'name="note_text" value="part 2 only"' in response.text
    assert 'name="role" value="reference"' in response.text


def test_an_ambiguous_reference_is_a_422_with_the_connector_names(add_connector, post) -> None:
    add_connector(name="FakeA")
    add_connector(name="FakeB")

    response = post("/issues/OIL-2/sources/preview", {"reference": "FAKE-1"})

    assert response.status_code == 422
    assert response.text.startswith(ERROR_PREFIX)
    assert "several connectors recognise &#39;FAKE-1&#39; (FakeA, FakeB)" in response.text


def test_a_reference_nobody_recognises_is_a_422(add_connector, post) -> None:
    add_connector()

    response = post("/issues/OIL-2/sources/preview", {"reference": "no idea what this is"})

    assert response.status_code == 422
    assert response.text.startswith(ERROR_PREFIX)
    assert "no connector recognises &#39;no idea what this is&#39;" in response.text


def test_a_blank_reference_is_a_422(post) -> None:
    response = post("/issues/OIL-2/sources/preview", {"reference": "  "})

    assert response.status_code == 422
    assert "enter a URL, key or path" in response.text


def test_linking_a_reference_stores_the_content_and_audits(detail_app: FastAPI, add_connector, post) -> None:
    add_connector()

    response = post(
        "/issues/OIL-2/sources/link",
        {"mode": "reference", "reference": "FAKE-7", "role": "evidence", "note_text": "only part 2"},
    )

    assert response.status_code == 200
    [obj] = rows(detail_app, "SELECT id FROM source_object")
    entry = entry_of(response.text, obj.id)
    assert "FAKE-7" in entry and '<span class="badge">evidence</span>' in entry
    assert "1 versions · 2 entries" in entry
    assert status_of(entry) == ("✓", None)
    summary = plain(re.search(r'<div class="sync-summary.*?</div>', response.text, re.DOTALL).group(0))
    assert f"Sync source:{obj.id}: ok — 1 checked, 1 changed, 1 new versions, 2 new entries" in summary
    with detail_app.state.engine.begin() as conn:
        assert [v.text for v in store.list_versions(conn, obj.id)] == ["Summary with frobnicator trouble"]
        assert len(store.list_entries(conn, obj.id)) == 2
        [link] = linking.list_issue_sources(conn, "OIL-2")
    assert (link.role, link.note, link.link_method) == ("evidence", "only part 2", "manual")
    [logged] = entries(detail_app, "source.link")
    assert (logged.action_class, logged.actor_type, logged.target) == ("B", "user", "OIL-2")
    assert logged.detail == {
        "source_object_id": obj.id,
        "label": "jira_issue: FAKE-7",
        "role": "evidence",
        "via": "web",
    }
    assert [tuple(r) for r in rows(detail_app, "SELECT trigger, scope FROM sync_run")] == [
        ("ui", f"source:{obj.id}")
    ]
    assert len(entries(detail_app, "source.fetch")) == 1


def test_mode_and_role_default_when_missing(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/sources/link", {"reference": "https://example.com/a"})

    assert response.status_code == 200
    with detail_app.state.engine.begin() as conn:
        [link] = linking.list_issue_sources(conn, "OIL-2")
    assert (link.object_type, link.role, link.note) == ("web_link", "reference", None)
    assert "Web links" in response.text


def test_linking_the_same_source_twice_is_rejected(detail_app: FastAPI, post) -> None:
    assert post("/issues/OIL-2/sources/link", {"reference": "https://example.com/a"}).status_code == 200

    response = post("/issues/OIL-2/sources/link", {"reference": "https://EXAMPLE.com/a"})

    assert response.status_code == 422
    assert "already linked" in response.text
    assert len(entries(detail_app, "source.link")) == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"reference": "https://example.com/a", "role": "boss"}, "role must be one of"),
        ({"reference": "https://example.com/a", "mode": "telepathy"}, "mode must be one of"),
        ({"reference": ""}, "enter a URL, key or path"),
    ],
)
def test_invalid_link_requests_are_422_and_store_nothing(
    detail_app: FastAPI, post, data: dict, message: str
) -> None:
    response = post("/issues/OIL-2/sources/link", data)

    assert response.status_code == 422
    assert message in response.text
    assert rows(detail_app, "SELECT id FROM issue_source_link") == []
    assert entries(detail_app, "source.link") == []


def test_a_failing_first_fetch_keeps_the_link_and_shows_the_error(
    detail_app: FastAPI, add_connector, post
) -> None:
    add_connector()
    FakeJira.failing.add("9")

    response = post("/issues/OIL-2/sources/link", {"reference": "FAKE-9"})

    assert response.status_code == 200
    [obj] = rows(detail_app, "SELECT id FROM source_object")
    assert status_of(entry_of(response.text, obj.id)) == ("⚠", "RuntimeError: boom")
    assert "RuntimeError: boom" in plain(
        re.search(r'<div class="sync-summary.*?</div>', response.text, re.DOTALL).group(0)
    )
    assert len(entries(detail_app, "source.link")) == 1


# --- the link dialog: Outlook ------------------------------------------------------------------------------


def test_previewing_the_selected_outlook_email_and_linking_it(
    detail_app: FastAPI, post, mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeOutlookSession, "selected", [mailbox[1]])

    preview = post("/issues/OIL-2/sources/preview-outlook", {"role": "evidence"})

    assert preview.status_code == 200
    text_ = plain(preview.text)
    assert "Connector Outlook (outlook)" in text_
    assert "Type email_thread" in text_
    assert "Title Inductor lead time" in text_
    assert "Locator outlook:S1:A2" in text_
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', preview.text))
    assert hidden == {"mode": "outlook-selected", "role": "evidence", "note_text": ""}
    assert rows(detail_app, "SELECT id FROM source_object") == []

    response = post("/issues/OIL-2/sources/link", hidden)

    assert response.status_code == 200
    [obj] = rows(detail_app, "SELECT id, external_id, locator FROM source_object")
    assert (obj.external_id, obj.locator) == ("CA", "outlook:S1:A2")
    assert "Email" in re.findall(r'<h3 class="source-group">(.*?)</h3>', response.text)
    assert "2 entries" in entry_of(response.text, obj.id)
    [logged] = entries(detail_app, "source.link")
    assert logged.detail["via"] == "web" and logged.detail["role"] == "evidence"
    assert logged.detail["label"] == "email_thread: Inductor lead time"
    assert rows(detail_app, "SELECT trigger FROM sync_run")[0][0] == "ui"


def test_the_selected_outlook_email_needs_a_selection(detail_app: FastAPI, post, mailbox) -> None:
    for path in ("preview-outlook", "link"):
        data = {"mode": "outlook-selected"} if path == "link" else {}
        response = post(f"/issues/OIL-2/sources/{path}", data)

        assert response.status_code == 422
        assert "no email is selected in Outlook" in response.text
    assert rows(detail_app, "SELECT id FROM source_object") == []


def test_outlook_actions_without_an_outlook_connector_are_422(post) -> None:
    for path, data in (
        ("preview-outlook", {}),
        ("outlook-search", {"subject": "x"}),
        ("link", {"mode": "outlook-selected"}),
        ("link", {"mode": "outlook-subject", "subject": "x", "pick": "1"}),
    ):
        response = post(f"/issues/OIL-2/sources/{path}", data)

        assert response.status_code == 422, path
        assert "no enabled Outlook connector" in response.text


def test_the_subject_search_lists_the_candidates_with_a_link_button_each(mailbox, post) -> None:
    response = post("/issues/OIL-2/sources/outlook-search", {"subject": "inductor"})

    assert response.status_code == 200
    items = re.findall(r"<li>(.*?)</li>", response.text, re.DOTALL)
    assert len(items) == 2
    assert re.search(r"1\. Inductor sample — 2026-03-0\d \d\d:\d\d — Jane \(1 messages\)", plain(items[0]))
    assert re.search(r"2\. Inductor lead time — 2026-03-0\d \d\d:\d\d — Bob \(2 messages\)", plain(items[1]))
    for number, item in enumerate(items, start=1):
        assert 'hx-post="/issues/OIL-2/sources/link"' in item
        hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', item))
        assert hidden == {"mode": "outlook-subject", "subject": "inductor", "pick": str(number)}


def test_linking_a_picked_candidate(detail_app: FastAPI, post, mailbox) -> None:
    response = post(
        "/issues/OIL-2/sources/link", {"mode": "outlook-subject", "subject": "inductor", "pick": "2"}
    )

    assert response.status_code == 200
    [obj] = rows(detail_app, "SELECT id, external_id FROM source_object")
    assert obj.external_id == "CA"
    assert f'href="/sources/{obj.id}">Inductor lead time</a>' in response.text
    [logged] = entries(detail_app, "source.link")
    assert logged.detail["via"] == "web"


@pytest.mark.parametrize("pick", ["3", "0", "-1", "", "two"])
def test_a_pick_out_of_range_or_missing_is_a_422(detail_app: FastAPI, post, mailbox, pick: str) -> None:
    response = post(
        "/issues/OIL-2/sources/link", {"mode": "outlook-subject", "subject": "inductor", "pick": pick}
    )

    assert response.status_code == 422
    assert response.text.startswith(ERROR_PREFIX)
    if pick in ("3", "0", "-1"):
        assert "out of range: 2 conversations match" in response.text
    assert rows(detail_app, "SELECT id FROM source_object") == []
    assert rows(detail_app, "SELECT id FROM issue_source_link") == []


def test_a_subject_without_a_hit_or_blank_is_a_422(post, mailbox) -> None:
    for path, data in (
        ("outlook-search", {"subject": "gearbox"}),
        ("link", {"mode": "outlook-subject", "subject": "gearbox", "pick": "1"}),
    ):
        response = post(f"/issues/OIL-2/sources/{path}", data)

        assert response.status_code == 422
        assert "no email conversation matches &#39;gearbox&#39;" in response.text
    assert "enter a subject" in post("/issues/OIL-2/sources/outlook-search", {"subject": " "}).text


# --- the link dialog: note ---------------------------------------------------------------------------------


def test_a_new_note_is_created_and_linked_without_a_sync(detail_app: FastAPI, post) -> None:
    response = post(
        "/issues/OIL-2/sources/note", {"title": " Call with supplier ", "text": "Agreed on 4 weeks"}
    )

    assert response.status_code == 200
    assert re.findall(r'<h3 class="source-group">(.*?)</h3>', response.text) == ["Notes"]
    [obj] = rows(detail_app, "SELECT id, object_type, title, fetch_status FROM source_object")
    assert (obj.object_type, obj.title, obj.fetch_status) == ("note", "Call with supplier", "ok")
    assert f'href="/sources/{obj.id}">Call with supplier</a>' in response.text
    assert "1 versions · 0 entries" in entry_of(response.text, obj.id)
    with detail_app.state.engine.begin() as conn:
        assert [v.text for v in store.list_versions(conn, obj.id)] == ["Agreed on 4 weeks"]
        [link] = linking.list_issue_sources(conn, "OIL-2")
    assert (link.role, link.note) == ("reference", None)
    [logged] = entries(detail_app, "source.link")
    assert logged.detail == {
        "source_object_id": obj.id,
        "label": "note: Call with supplier",
        "role": "reference",
        "via": "web",
    }
    assert rows(detail_app, "SELECT id FROM sync_run") == []
    assert "sync-summary" not in response.text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"title": " ", "text": "x"}, "a note needs a title"),
        ({"title": "T", "text": " "}, "a note needs text"),
    ],
)
def test_an_incomplete_note_is_a_422_and_stores_nothing(
    detail_app: FastAPI, post, data: dict, message: str
) -> None:
    response = post("/issues/OIL-2/sources/note", data)

    assert response.status_code == 422
    assert message in response.text
    assert rows(detail_app, "SELECT id FROM source_object") == []
    assert entries(detail_app, "source.link") == []


def test_the_dialog_has_the_three_forms(detail_client: TestClient) -> None:
    panel = panel_of(detail_client.get("/issues/OIL-2").text)

    for path in ("preview", "preview-outlook", "outlook-search", "note"):
        assert f'hx-post="/issues/OIL-2/sources/{path}"' in panel
    assert ">Preview</button>" in panel and ">Use selected Outlook email</button>" in panel
    for field in ("reference", "role", "note_text", "subject", "title", "text"):
        assert f'name="{field}"' in panel
    assert re.findall(r'<option value="(\w+)"', panel) == list(linking.ROLES)


# --- protection --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "refresh",
        "1/refresh",
        "1/unlink",
        "preview",
        "preview-outlook",
        "outlook-search",
        "link",
        "note",
    ],
)
def test_every_post_needs_the_csrf_token(detail_app: FastAPI, post, path: str) -> None:
    response = post(f"/issues/OIL-2/sources/{path}", {"reference": "https://example.com/a"}, csrf=False)

    assert response.status_code == 403
    assert rows(detail_app, "SELECT id FROM source_object") == []


def test_the_other_issue_is_untouched(detail_app: FastAPI, post) -> None:
    assert post("/issues/OIL-4/sources/link", {"reference": "https://example.com/a"}).status_code == 200
    with detail_app.state.engine.begin() as conn:
        assert linking.list_issue_sources(conn, "OIL-2") == []
        assert len(linking.list_issue_sources(conn, "OIL-4")) == 1
