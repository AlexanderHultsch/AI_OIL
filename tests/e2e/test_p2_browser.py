"""Phase-2 exit criteria (spec chapter 2) in a real Chromium: the P1 acceptance repeated through the web UI.

The data folder, the project, the people, the issues, the links, the syncs and every edit go through the
browser. Only what the web interface does not offer is prepared through `main([...])`: `oil init` and the
four `oil connector add` calls. Outlook, Jira and Confluence have no real backend here, so their plugins are
registered in-process with fake sessions (as in test_p1.py); SharePoint reads real files below a sync root.
"""

import copy
import io
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from playwright.sync_api import Locator, Page, expect, sync_playwright  # noqa: E402

from ai_oil.cli.main import main  # noqa: E402
from ai_oil.config import load_config  # noqa: E402
from ai_oil.connectors.atlassian import AtlassianClient  # noqa: E402
from ai_oil.connectors.confluence import ConfluenceConnector  # noqa: E402
from ai_oil.connectors.jira import JiraConnector  # noqa: E402
from ai_oil.connectors.outlook import OutlookConnector  # noqa: E402
from ai_oil.connectors.outlook_session import MailAddress, MailRecord  # noqa: E402
from ai_oil.db.engine import create_db_engine  # noqa: E402
from ai_oil.sources import registry  # noqa: E402
from ai_oil.sources.model import ConnectorConfig  # noqa: E402
from ai_oil.web.app import create_app  # noqa: E402

pytestmark = pytest.mark.browser

TIMEOUT_MS = 10_000
HOST = "127.0.0.1"
BUNDLED_CHROMIUM = Path("/opt/pw-browsers/chromium")

TOKEN = "s3cr3t-api-token"
BASE = "https://acme.atlassian.net"
WIKI = BASE + "/wiki"
PAGE_URL = f"{WIKI}/spaces/ABC/pages/123456/Supplier+meeting+notes"
LIBRARY_URL = "https://acme.sharepoint.com/sites/HW/Shared%20Documents"
FILE_URL = f"{LIBRARY_URL}/Specs/Torque%20spec.txt"
INBOX = "\\\\alex@example.com\\Inbox"

JIRA_PAYLOADS: dict[str, Any] = {
    "/rest/api/3/issue/ABC-123": {
        "id": "10001",
        "key": "ABC-123",
        "fields": {
            "summary": "Inductor lead time",
            "description": None,
            "status": {"name": "In Progress"},
            "assignee": None,
            "reporter": {"accountId": "acc-alex", "displayName": "Alex"},
            "priority": {"name": "High"},
            "labels": [],
            "components": [],
            "fixVersions": [],
            "duedate": None,
            "resolution": None,
            "updated": "2026-03-05T10:15:30.123+0100",
            "attachment": [],
        },
    },
    "/rest/api/3/issue/10001/comment": {
        "total": 1,
        "comments": [
            {
                "id": "20001",
                "author": {"accountId": "acc-jane", "displayName": "Jane Doe"},
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Please confirm torque."}]}
                    ],
                },
                "created": "2026-03-02T09:00:00.000+0100",
                "updated": "2026-03-02T09:00:00.000+0100",
            }
        ],
    },
    "/rest/api/3/issue/10001/changelog": {"total": 0, "values": []},
}
JIRA_PAYLOADS["/rest/api/3/issue/10001"] = JIRA_PAYLOADS["/rest/api/3/issue/ABC-123"]

CONFLUENCE_PAYLOADS: dict[str, Any] = {
    "/wiki/api/v2/pages/123456": {
        "id": "123456",
        "status": "current",
        "title": "Supplier meeting notes",
        "spaceId": "98765",
        "version": {"createdAt": "2026-03-05T10:15:30.123Z", "number": 7, "authorId": "acc-jane"},
        "body": {
            "storage": {
                "representation": "storage",
                "value": "<h1>Supplier meeting</h1><p>Lead time is 26 weeks.</p>",
            }
        },
        "_links": {"webui": "/spaces/ABC/pages/123456/Supplier+meeting+notes"},
    },
    "/wiki/api/v2/pages/123456/footer-comments": {"results": [], "_links": {}},
    "/wiki/api/v2/pages/123456/inline-comments": {"results": [], "_links": {}},
}


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self.url = ""
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeJiraSession:
    payloads: ClassVar[dict[str, Any]] = {}

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        payload = self.payloads.get(url.removeprefix(BASE))
        return FakeResponse(404) if payload is None else FakeResponse(200, payload)


class FakeConfluenceSession:
    payloads: ClassVar[dict[str, Any]] = {}

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        payload = self.payloads.get(url.removeprefix(BASE))
        return FakeResponse(404) if payload is None else FakeResponse(200, payload)


class FixtureJira(JiraConnector):
    """The Jira plugin with a REST client that talks to the fixture site instead of the network."""

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        client = AtlassianClient(
            BASE,
            "alex@example.com",
            config.credential_ref,
            session=FakeJiraSession(),  # type: ignore[arg-type]
        )
        super().__init__(config, client=client)


class FixtureConfluence(ConfluenceConnector):
    """The Confluence plugin with a REST client that talks to the fixture site instead of the network."""

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        client = AtlassianClient(
            WIKI,
            "alex@example.com",
            config.credential_ref,
            session=FakeConfluenceSession(),  # type: ignore[arg-type]
        )
        super().__init__(config, client=client)


def mail(entry_id: str, conversation_id: str, subject: str, day: int, body: str, sender: str) -> MailRecord:
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
        body=body,
        html_body="",
        folder_path=INBOX,
        attachments=(),
    )


class FakeOutlookSession:
    """Scripted mailbox shared through class attributes, since the connector builds its own session."""

    records: ClassVar[list[MailRecord]] = []
    selected: ClassVar[list[MailRecord]] = []

    def folder_paths(self) -> list[str]:
        return [INBOX]

    def selected_messages(self) -> list[MailRecord]:
        return list(self.selected)

    def find_by_subject(self, text: str, *, since: datetime | None, limit: int) -> list[MailRecord]:
        return [r for r in self.records if text.casefold() in r.subject.casefold()][:limit]

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


class FixtureOutlook(OutlookConnector):
    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        super().__init__(config, session=FakeOutlookSession())


@pytest.fixture
def remotes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fake Outlook, Jira and Confluence backends, registered as the plugins of those types."""
    monkeypatch.setattr(FakeJiraSession, "payloads", copy.deepcopy(JIRA_PAYLOADS))
    monkeypatch.setattr(FakeConfluenceSession, "payloads", copy.deepcopy(CONFLUENCE_PAYLOADS))
    first = mail("A1", "CA", "Inductor lead time", 1, "Please confirm the lead time.", "Jane")
    monkeypatch.setattr(FakeOutlookSession, "records", [first])
    monkeypatch.setattr(FakeOutlookSession, "selected", [first])
    for plugin in (FixtureOutlook, FixtureJira, FixtureConfluence):
        registry.register_connector_type(plugin)
    yield
    for type_name in ("outlook", "jira", "confluence"):
        registry.unregister_connector_type(type_name)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    folder = tmp_path / "OneDrive" / "HW - Shared Documents"
    (folder / "Specs").mkdir(parents=True)
    (folder / "Specs" / "Torque spec.txt").write_text("The flange torque limit is 12 Nm.", encoding="utf-8")
    return folder


@dataclass(frozen=True)
class Site:
    """The running web interface and the folder behind it."""

    url: str
    engine: Any
    library: Path


def oil(home: Path, *argv: str) -> None:
    """A precondition the web interface does not offer, run through the CLI entry point."""
    code = main(["--home", str(home), *argv])
    assert code == 0, f"oil {' '.join(argv)} -> {code}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def wait_until_healthy(url: str) -> None:
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    raise AssertionError(f"the server at {url} did not answer /health")


@pytest.fixture
def site(tmp_path: Path, library: Path, remotes: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    """An initialised data folder with the four connectors, served by uvicorn in a background thread."""
    home = tmp_path / "browser-home"
    oil(home, "init", "--name", "Alex", "--email", "alex@example.com")
    oil(home, "connector", "add", "outlook", "Outlook", "--setting", "scan_days=30")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{TOKEN}\n"))
    oil(
        home,
        "connector",
        "add",
        "jira",
        "Jira",
        "--setting",
        f"base_url={BASE}",
        "--setting",
        "email=alex@example.com",
        "--setting",
        'projects=["HW"]',
        "--token-stdin",
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{TOKEN}\n"))
    oil(
        home,
        "connector",
        "add",
        "confluence",
        "Confluence",
        "--setting",
        f"base_url={WIKI}",
        "--setting",
        "email=alex@example.com",
        "--token-stdin",
    )
    roots = [{"url": "https://acme.sharepoint.com/sites/HW/Shared Documents", "path": str(library)}]
    oil(home, "connector", "add", "sharepoint", "SharePoint", "--setting", f"sync_roots={json.dumps(roots)}")

    config = load_config(home)
    engine = create_db_engine(config.db_path)
    port = free_port()
    url = f"http://{HOST}:{port}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(config, engine, port=port), host=HOST, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_until_healthy(url)
        yield Site(url=url, engine=engine, library=library)
    finally:
        server.should_exit = True
        thread.join(timeout=TIMEOUT_MS / 1000)
        engine.dispose()


def chromium_executable() -> str | None:
    """`AI_OIL_CHROMIUM`, else the pre-installed Chromium under /opt/pw-browsers, else Playwright's own."""
    configured = os.environ.get("AI_OIL_CHROMIUM")
    if configured:
        return configured
    return str(BUNDLED_CHROMIUM) if BUNDLED_CHROMIUM.is_file() else None


@pytest.fixture
def page() -> Iterator[Page]:
    expect.set_options(timeout=TIMEOUT_MS)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
        try:
            browser_page = browser.new_context().new_page()
            browser_page.set_default_timeout(TIMEOUT_MS)
            yield browser_page
        finally:
            browser.close()


# --- helpers that act like a user -------------------------------------------------------------------------


def create_issue(
    page: Page,
    site: Site,
    number: int,
    *,
    summary: str,
    priority: str,
    assignee: str,
    due: str,
    label: str,
) -> None:
    page.goto(f"{site.url}/issues/new?project=OIL")
    page.get_by_label("Summary", exact=True).fill(summary)
    page.get_by_label("Description", exact=True).fill(f"Description of issue {number}")
    page.get_by_label("Priority", exact=True).select_option(priority)
    page.get_by_label("Assignee", exact=True).select_option(label=assignee)
    page.get_by_label("Due", exact=True).fill(due)
    page.get_by_label("Labels", exact=True).fill(label)
    page.get_by_role("button", name="Create", exact=True).click()
    page.wait_for_url(f"{site.url}/issues/OIL-{number}")
    expect(page.locator("#field-summary h1")).to_have_text(summary)


def link_dialog(page: Page) -> Locator:
    """The open link dialog of the sources panel."""
    dialog = page.locator("#sources-panel details.link-dialog")
    if dialog.get_attribute("open") is None:
        dialog.locator("summary").click()
    return dialog


def expect_source(page: Page, heading: str, label: str) -> None:
    """The sources panel lists a source with `label` under the group `heading`."""
    group = page.locator("#sources-panel h3.source-group", has_text=heading)
    expect(group).to_be_visible()
    expect(page.locator("#sources-panel li.source .source-label")).to_contain_text(label)
    expect(page.locator("#sources-panel li.source .source-status").first).to_have_text("✓")


def link_by_reference(page: Page, site: Site, key: str, reference: str, heading: str, label: str) -> None:
    page.goto(f"{site.url}/issues/{key}")
    dialog = link_dialog(page)
    form = dialog.locator('form[hx-post$="/sources/preview"]')
    form.get_by_label("Reference", exact=True).fill(reference)
    form.get_by_role("button", name="Preview", exact=True).click()
    preview = page.locator("#link-preview .source-preview")
    expect(preview).to_be_visible()
    expect(preview).to_contain_text(label)
    preview.get_by_role("button", name="Link", exact=True).click()
    expect_source(page, heading, label)


def sync_all(page: Page, site: Site) -> Locator:
    """Press "Sync all now" and return the cells of the newest run of the sync log."""
    page.goto(f"{site.url}/sync")
    runs = page.locator("tbody.sync-run")
    before = runs.count()
    page.get_by_role("button", name="Sync all now").click()
    expect(runs).to_have_count(before + 1)
    return runs.first.locator("tr").first.locator("td")


def search(page: Page, site: Site, oql: str) -> Locator:
    """Type `oql` into the OQL input of the issue list, submit it and return the result rows."""
    page.goto(f"{site.url}/issues")
    box = page.get_by_label("OQL query", exact=True)
    box.fill(oql)
    box.press("Enter")
    page.wait_for_url(re.compile(r"/issues\?q="))
    return page.locator("table.issues tbody tr[data-issue-key]")


def edit_field(page: Page, field: str) -> None:
    """Open the in-place editor of a field; its Edit button only shows while the pointer is over the field."""
    page.locator(f"#field-{field}").hover()
    page.locator(f"#edit-{field}").click()


def counts_text(page: Page) -> Locator:
    return page.locator("dl.source-facts dt", has_text="Counts").locator("xpath=following-sibling::dd[1]")


def test_phase_2_exit_criteria(page: Page, site: Site) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    url = site.url

    # a. A project with a component, and a second project for the component reload; one more person.
    page.goto(f"{url}/projects")
    for key, name in (("OIL", "Open issues"), ("ABC", "Alphabet")):
        form = page.locator('form[aria-label="Add project"]')
        form.get_by_label("Project key", exact=True).fill(key)
        form.get_by_label("Project name", exact=True).fill(name)
        form.get_by_role("button", name="Add project").click()
        expect(page.locator(f"#project-{key} h2")).to_contain_text(name)
    component_form = page.locator('form[aria-label="Add component to OIL"]')
    component_form.get_by_label("Component name", exact=True).fill("Housing")
    component_form.get_by_role("button", name="Add component").click()
    expect(page.locator("#project-OIL .component-list")).to_contain_text("Housing")

    page.goto(f"{url}/people")
    page.get_by_label("Name", exact=True).fill("Bob")
    page.get_by_label("E-mail addresses", exact=True).fill("bob@example.com")
    page.get_by_role("button", name="Add person").click()
    expect(page.locator("table tbody tr", has_text="Bob")).to_be_visible()

    # b. Twenty issues with varied priority, assignee, due date and label; one picks the component after
    # the project select was changed, which reloads the component options through htmx.
    priorities = ("P1", "P2", "P3", "P4", "P5")
    assignees = ("Unassigned", "Bob", "Alex")
    labels = ("supplier", "emc", "housing", "software")
    for number in range(1, 21):
        if number == 7:
            page.goto(f"{url}/issues/new")
            page.get_by_label("Project", exact=True).select_option("OIL")
            components = page.get_by_label("Components", exact=True)
            expect(components.locator("option")).to_have_count(1)
            page.get_by_label("Project", exact=True).select_option("ABC")
            expect(components.locator("option")).to_have_count(0)
            page.get_by_label("Project", exact=True).select_option("OIL")
            expect(components.locator("option")).to_have_count(1)
            components.select_option("Housing")
            page.get_by_label("Summary", exact=True).fill(f"Issue {number} for the supplier review")
            page.get_by_label("Description", exact=True).fill(f"Description of issue {number}")
            page.get_by_label("Priority", exact=True).select_option(priorities[number % 5])
            page.get_by_label("Assignee", exact=True).select_option(label=assignees[number % 3])
            page.get_by_label("Due", exact=True).fill(f"2026-11-{number + 5:02d}")
            page.get_by_label("Labels", exact=True).fill(labels[number % 4])
            page.get_by_role("button", name="Create", exact=True).click()
            page.wait_for_url(f"{url}/issues/OIL-{number}")
            expect(page.locator("#field-components .chip-name")).to_have_text("Housing")
            continue
        create_issue(
            page,
            site,
            number,
            summary=f"Issue {number} for the supplier review",
            priority=priorities[number % 5],
            assignee=assignees[number % 3],
            due=f"2026-11-{number + 5:02d}",
            label=labels[number % 4],
        )
    page.goto(f"{url}/issues")
    expect(page.locator("p.result-count")).to_have_text("20 issues")
    expect(page.locator("table.issues tbody tr[data-issue-key]")).to_have_count(20)

    # c. One source of each of the five types through the link dialog of five different issues.
    page.goto(f"{url}/issues/OIL-1")
    note_form = link_dialog(page).locator('form[hx-post$="/sources/note"]')
    note_form.get_by_label("Note title", exact=True).fill("Supplier call")
    note_form.get_by_label("Note text", exact=True).fill("Ferrite allocation is open.")
    note_form.get_by_role("button", name="Create note").click()
    expect(page.locator("#sources-panel h3.source-group", has_text="Notes")).to_be_visible()
    expect(page.locator("#sources-panel li.source .source-label")).to_contain_text("Supplier call")

    page.goto(f"{url}/issues/OIL-2")
    link_dialog(page).get_by_role("button", name="Use selected Outlook email").click()
    preview = page.locator("#link-preview .source-preview")
    expect(preview).to_contain_text("Inductor lead time")
    preview.get_by_role("button", name="Link", exact=True).click()
    expect_source(page, "Email", "Inductor lead time")

    link_by_reference(page, site, "OIL-3", "ABC-123", "Jira", "ABC-123")
    link_by_reference(page, site, "OIL-4", PAGE_URL, "Confluence", "Supplier meeting notes")
    link_by_reference(page, site, "OIL-5", FILE_URL, "SharePoint", "Torque spec.txt")

    # d. The first "Sync all now" checks the five sources and finds nothing new.
    cells = sync_all(page, site)
    expect(cells).to_have_text(
        [re.compile(".*"), "ui", "all", "ok", re.compile(".*"), "5", "0", "0", "0", "0"]
    )

    # e. A real change in every source, then the second sync.
    page.goto(f"{url}/issues/OIL-1")
    note_id = int(page.locator("#sources-panel a.source-label").get_attribute("href").rsplit("/", 1)[1])
    FakeOutlookSession.records = [
        *FakeOutlookSession.records,
        mail("A2", "CA", "RE: Inductor lead time", 4, "The lacquer batch is confirmed.", "Bob"),
    ]
    jira = FakeJiraSession.payloads
    jira["/rest/api/3/issue/10001/comment"]["total"] = 2
    jira["/rest/api/3/issue/10001/comment"]["comments"].append(
        {
            "id": "20002",
            "author": {"accountId": "acc-alex", "displayName": "Alex"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Varnish approved."}]}
                ],
            },
            "created": "2026-03-06T09:00:00.000+0100",
            "updated": "2026-03-06T09:00:00.000+0100",
        }
    )
    jira["/rest/api/3/issue/ABC-123"]["fields"]["updated"] = "2026-03-06T09:00:00.000+0100"
    confluence = FakeConfluenceSession.payloads["/wiki/api/v2/pages/123456"]
    confluence["version"] = {"createdAt": "2026-03-07T10:00:00.000Z", "number": 8, "authorId": "acc-jane"}
    confluence["body"]["storage"]["value"] = (
        "<h1>Supplier meeting</h1><p>Lead time is 26 weeks. New gasket drawing.</p>"
    )
    spec = site.library / "Specs" / "Torque spec.txt"
    stat = spec.stat()
    spec.write_text("The flange torque limit is 12 Nm. Spindle runout is checked.", encoding="utf-8")
    os.utime(spec, (stat.st_atime + 60, stat.st_mtime + 60))
    page.goto(f"{url}/sources/{note_id}")
    page.locator("form.note-edit textarea[name='text']").fill("Tantalum allocation confirmed.")
    page.locator("form.note-edit").get_by_role("button", name="Save note").click()
    expect(page.locator("pre.source-text")).to_have_text("Tantalum allocation confirmed.")

    cells = sync_all(page, site)
    expect(cells).to_have_text(
        [re.compile(".*"), "ui", "all", "ok", re.compile(".*"), "5", "4", "2", "2", "0"]
    )
    expect(page.locator("tbody.sync-run").first.locator("summary")).to_have_text(
        "4 results other than unchanged"
    )

    # f. Each changed source is found by a word that only its new content has; its source page shows the
    # second version or the new entry.
    expectations = {
        "tantalum": ("OIL-1", "versions"),
        "lacquer": ("OIL-2", "entries"),
        "varnish": ("OIL-3", "entries"),
        "gasket": ("OIL-4", "versions"),
        "spindle": ("OIL-5", "versions"),
    }
    for word, (key, kind) in expectations.items():
        rows = search(page, site, f'text ~ "{word}"')
        expect(rows).to_have_count(1)
        expect(rows.first).to_have_attribute("data-issue-key", key)
        rows.first.get_by_role("link", name=key, exact=True).click()
        page.wait_for_url(f"{url}/issues/{key}")
        page.locator("#sources-panel a.source-label").click()
        page.wait_for_url(re.compile(r"/sources/\d+$"))
        expect(counts_text(page)).to_contain_text(f"2 {kind}")
        if kind == "versions":
            expect(page.locator("table.versions tbody tr")).to_have_count(2)
            expect(page.locator("pre.source-text")).to_contain_text(word, ignore_case=True)
        else:
            expect(page.locator("ul.entry-list li")).to_have_count(2)
            expect(page.locator("ul.entry-list")).to_contain_text(word, ignore_case=True)
        if word == "gasket":
            page.locator("form.version-compare").get_by_role("button", name="Compare").click()
            page.wait_for_url(re.compile(r"/sources/\d+/diff\?a=1&b=2$"))
            expect(page.locator("table.diff")).to_be_visible()
            expect(page.locator("table.diff")).to_contain_text("gasket")

    # g. Edit one issue in place; the History tab lists one line per change.
    page.goto(f"{url}/issues/OIL-6")
    edit_field(page, "summary")
    page.locator("#editor-summary").fill("Issue 6 renamed")
    page.locator("#field-summary").get_by_role("button", name="Save").click()
    expect(page.locator("#field-summary h1")).to_have_text("Issue 6 renamed")

    edit_field(page, "priority")
    page.locator("#editor-priority").select_option("P1")
    page.locator("#field-priority").get_by_role("button", name="Save").click()
    expect(page.locator("#field-priority .value")).to_have_text("P1")

    edit_field(page, "assignee")
    page.locator("#editor-assignee").select_option(label="Alex")
    page.locator("#field-assignee").get_by_role("button", name="Save").click()
    expect(page.locator("#field-assignee .value")).to_have_text("Alex")

    edit_field(page, "due_date")
    page.locator("#editor-due_date").fill("2026-12-01")
    page.locator("#field-due_date").get_by_role("button", name="Save").click()
    expect(page.locator("#field-due_date .value")).to_contain_text("2026-12-01")

    page.locator("#field-labels").get_by_label("Add label", exact=True).fill("urgent")
    page.locator("#field-labels").get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#field-labels .chip-name")).to_contain_text(["urgent"])

    page.locator("#tab-comments").click()
    page.locator("#comment-body").fill("Torque confirmed with the supplier.")
    page.locator("form.comment-add").get_by_role("button", name="Comment", exact=True).click()
    expect(page.locator(".comment-body")).to_have_text("Torque confirmed with the supplier.")

    status = page.locator("#side-panel").get_by_label("Status", exact=True)
    status.select_option("Blocked")
    page.locator("#side-panel").get_by_label("Reason", exact=True).fill("Waiting for PPAP")
    page.locator("#side-panel form.inline-reason").get_by_role("button", name="Set").click()
    expect(page.locator("#side-panel").get_by_label("Status", exact=True)).to_have_value("Blocked")
    expect(page.locator("#side-panel")).to_contain_text("Waiting for PPAP")

    page.locator("#tab-history").click()
    events = page.locator(".history li")
    expect(events).to_have_count(9)
    expect(events.filter(has_text="issue create")).to_have_count(1)
    shown = Counter(
        (
            event.locator(".event-field").inner_text(),
            event.inner_text().split(":", 1)[0].split(" ", 1)[1],
        )
        for event in events.all()
    )
    assert shown == Counter(
        {
            ("issue", "create"): 1,
            ("summary", "set"): 1,
            ("priority", "set"): 1,
            ("assignee", "set"): 1,
            ("due_date", "set"): 1,
            ("label", "add"): 1,
            ("comment", "add"): 1,
            ("status", "set"): 1,
            ("blocked_reason", "set"): 1,
        }
    )

    # h. An inline change of the priority in the issue list: the row is swapped and shows the new value.
    page.goto(f"{url}/issues")
    row = page.locator('tr[data-issue-key="OIL-8"]')
    expect(row.get_by_label("Priority", exact=True)).to_have_value("P4")
    row.evaluate("element => element.setAttribute('data-stale', 'yes')")
    row.get_by_label("Priority", exact=True).select_option("P1")
    expect(page.locator('tr[data-issue-key="OIL-8"][data-stale]')).to_have_count(0)
    expect(page.locator('tr[data-issue-key="OIL-8"]').get_by_label("Priority", exact=True)).to_have_value(
        "P1"
    )
    page.reload()
    expect(page.locator('tr[data-issue-key="OIL-8"]').get_by_label("Priority", exact=True)).to_have_value(
        "P1"
    )

    # i. Keyboard shortcuts.
    page.goto(f"{url}/issues")
    keys = [row.get_attribute("data-issue-key") for row in page.locator("table.issues tbody tr").all()]
    page.keyboard.press("/")
    expect(page.locator("#global-search")).to_be_focused()
    page.locator("h1").click()  # leaves the search field, so that the list shortcuts apply again
    page.keyboard.press("j")
    expect(page.locator("tr.kb-selected")).to_have_attribute("data-issue-key", keys[0])
    page.keyboard.press("j")
    expect(page.locator("tr.kb-selected")).to_have_attribute("data-issue-key", keys[1])
    page.keyboard.press("Enter")
    page.wait_for_url(f"{url}/issues/{keys[1]}")
    page.keyboard.press("e")
    expect(page.locator("#editor-summary")).to_be_visible()
    page.keyboard.press("m")
    expect(page.locator("#comment-body")).to_be_focused()
    page.locator("h2", has_text="Activity").click()  # focus back on the body
    page.keyboard.press("c")
    page.wait_for_url(f"{url}/issues/new")

    # j. An invalid inline action puts the domain error into #flash and leaves the data as it was.
    page.goto(f"{url}/issues")
    due = page.locator('tr[data-issue-key="OIL-9"]').get_by_label("Due date", exact=True)
    expect(due).to_have_value("2026-11-14")
    due.fill("")
    expect(page.locator("#flash")).to_contain_text("the due date cannot be removed once set")
    page.reload()
    expect(page.locator('tr[data-issue-key="OIL-9"]').get_by_label("Due date", exact=True)).to_have_value(
        "2026-11-14"
    )

    assert page_errors == []
