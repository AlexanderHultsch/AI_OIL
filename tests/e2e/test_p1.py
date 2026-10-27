"""Phase-1 exit criteria (spec chapter 2) as one scenario, driven through `main([...])` only.

Outlook, Jira and Confluence have no real backend here, so their plugins are registered in-process with
fake sessions; SharePoint reads real files below a configured sync root.
"""

import copy
import io
import json
import os
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ai_oil import secrets
from ai_oil.cli.main import main
from ai_oil.connectors.atlassian import AtlassianClient
from ai_oil.connectors.confluence import ConfluenceConnector
from ai_oil.connectors.jira import JiraConnector
from ai_oil.connectors.outlook import OutlookConnector
from ai_oil.connectors.outlook_session import MailAddress, MailRecord
from ai_oil.sources import registry
from ai_oil.sources.model import ConnectorConfig

TOKEN = "s3cr3t-api-token"
BASE = "https://acme.atlassian.net"
WIKI = BASE + "/wiki"
PAGE_URL = f"{WIKI}/spaces/ABC/pages/123456/Supplier+meeting+notes"
LIBRARY_URL = "https://acme.sharepoint.com/sites/HW/Shared%20Documents"
FILE_URL = f"{LIBRARY_URL}/Specs/Torque%20spec.txt"
INBOX = "\\\\alex@example.com\\Inbox"

# A word that exists only in the changed content of each source, and the issue that must be found by it.
NEW_WORDS = {
    "tantalum": "OIL-1",  # note edit
    "lacquer": "OIL-2",  # new email
    "varnish": "OIL-3",  # new Jira comment
    "gasket": "OIL-4",  # new Confluence page version
    "spindle": "OIL-5",  # changed SharePoint file
}

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


class Oil:
    """Runs `oil` against one data folder and captures what it prints."""

    def __init__(self, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._home = home
        self._capsys = capsys
        self.transcript = ""

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--home", str(self._home), *argv])
        captured = self._capsys.readouterr()
        self.transcript += captured.out + captured.err
        return code, captured.out, captured.err

    def ok(self, *argv: str) -> str:
        code, out, err = self.run(*argv)
        assert (code, err) == (0, ""), f"oil {' '.join(argv)} -> {code}\n{out}{err}"
        return out

    def search_keys(self, query: str) -> list[str]:
        return [row["key"] for row in json.loads(self.ok("search", query, "--json"))]


@pytest.fixture
def oil(tmp_path: Path, capsys: pytest.CaptureFixture[str], remotes: None) -> Oil:
    return Oil(tmp_path / "e2e-home", capsys)


def feed_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def counts(oil: Oil, source_id: int) -> tuple[int, int]:
    """(versions, entries) of a stored source, as `oil source show` reports them."""
    shown = oil.ok("source", "show", str(source_id))
    match = re.search(r"^Versions: (\d+) \| Entries: (\d+) \| Children: \d+$", shown, re.MULTILINE)
    assert match is not None, shown
    return int(match.group(1)), int(match.group(2))


def source_id_of(oil: Oil, key: str) -> int:
    shown = oil.ok("issue", "show", key)
    [found] = re.findall(r"^  #(\d+) ", shown, re.MULTILINE)
    return int(found)


def history_of(oil: Oil, key: str) -> list[tuple[str, str, str, str]]:
    """(field, op, old, new) per line of `oil issue show KEY --history`, oldest first."""
    shown = oil.ok("issue", "show", key, "--history")
    lines = shown.split("\nHistory:\n", 1)[1].splitlines()
    parsed = []
    for line in lines:
        match = re.fullmatch(r"\s+\S+ \S+ (\w+) (\w+) (.*) -> (.*)", line)
        assert match is not None, line
        parsed.append((match.group(1), match.group(2), match.group(3), match.group(4)))
    return parsed


def test_phase_1_exit_criteria(oil: Oil, library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Set up through the CLI: data folder, project, people and the four connectors.
    oil.ok("init", "--name", "Alex", "--email", "alex@example.com")
    oil.ok("project", "add", "OIL", "Open issues")
    oil.ok("person", "add", "Bob", "--email", "bob@example.com")

    assert oil.ok("connector", "add", "outlook", "Outlook", "--setting", "scan_days=30") == (
        "Added connector Outlook (outlook)\n"
    )
    feed_stdin(monkeypatch, f"{TOKEN}\n")
    oil.ok(
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
    feed_stdin(monkeypatch, f"{TOKEN}\n")
    oil.ok(
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
    oil.ok("connector", "add", "sharepoint", "SharePoint", "--setting", f"sync_roots={json.dumps(roots)}")
    assert secrets.get_secret("ai_oil/Jira") == TOKEN
    assert secrets.get_secret("ai_oil/Confluence") == TOKEN
    assert secrets.get_secret("ai_oil/Outlook") is None
    listed = oil.ok("connector", "list")
    assert [line.split("  ")[1] for line in listed.splitlines() if not line.startswith(" ")] == [
        "Confluence",
        "Jira",
        "Outlook",
        "SharePoint",
    ]
    assert "credential: stored" in listed

    # 2. Twenty issues with varied priority, assignee, labels and due dates.
    priorities = ("P1", "P2", "P3", "P4", "P5")
    assignees = (None, "Bob", "me")
    labels = ("supplier", "emc", "housing", "software")
    created = []
    for number in range(1, 21):
        argv = [
            "issue",
            "create",
            "-p",
            "OIL",
            "-s",
            f"Issue {number} for the supplier review",
            "-d",
            f"Description of issue {number}",
            "--priority",
            priorities[number % 5],
            "--due",
            f"2026-11-{number + 5:02d}",
            "--label",
            labels[number % 4],
        ]
        if assignees[number % 3] is not None:
            argv += ["--assignee", assignees[number % 3] or ""]
        created.append(oil.ok(*argv).strip())
    assert created == [f"OIL-{number}" for number in range(1, 21)]
    assert len(oil.search_keys("project = OIL")) == 20
    assert oil.search_keys("assignee = Bob") != []
    assert oil.search_keys("priority = P1 ORDER BY key") == ["OIL-5", "OIL-10", "OIL-15", "OIL-20"]

    # 3. One source of each of the five types.
    linked = {
        "note": oil.ok("link", "OIL-1", "--title", "Supplier call", "--text", "Ferrite allocation is open."),
        "email": oil.ok("link", "OIL-2", "--outlook-selected"),
        "jira": oil.ok("link", "OIL-3", "ABC-123"),
        "confluence": oil.ok("link", "OIL-4", PAGE_URL),
        "file": oil.ok("link", "OIL-5", FILE_URL),
    }
    assert linked["note"].splitlines()[0] == "Linked note: Supplier call to OIL-1"
    assert linked["email"].splitlines()[0] == "Linked email_thread: Inductor lead time to OIL-2"
    assert linked["jira"].splitlines()[0] == "Linked jira_issue: ABC-123 to OIL-3"
    assert linked["confluence"].splitlines()[0] == "Linked confluence_page: Supplier meeting notes to OIL-4"
    assert linked["file"].splitlines()[0] == "Linked file: Torque spec.txt to OIL-5"
    source_ids = {key: source_id_of(oil, key) for key in ("OIL-1", "OIL-2", "OIL-3", "OIL-4", "OIL-5")}
    assert len(set(source_ids.values())) == 5
    for word in NEW_WORDS:
        assert oil.search_keys(f'text ~ "{word}"') == []
    assert oil.search_keys('text ~ "ferrite"') == ["OIL-1"]
    assert oil.search_keys('text ~ "flange"') == ["OIL-5"]

    # 4. The first `oil sync --all` checks every source and finds nothing new.
    first = oil.ok("sync", "--all")
    assert first.startswith("Sync all: ok — 5 checked, 0 changed, 0 new versions, 0 new entries, 0 errors")
    for source_id in source_ids.values():
        assert "Status: ok, checked 20" in oil.ok("source", "show", str(source_id))
    before = {key: counts(oil, source_id) for key, source_id in source_ids.items()}
    assert before["OIL-1"][0] == 1
    assert before["OIL-2"][1] == 1
    assert before["OIL-3"][1] == 1
    assert before["OIL-4"][0] == 1
    assert before["OIL-5"][0] == 1

    # 5. A real change in every source.
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
    page = FakeConfluenceSession.payloads["/wiki/api/v2/pages/123456"]
    page["version"] = {"createdAt": "2026-03-07T10:00:00.000Z", "number": 8, "authorId": "acc-jane"}
    page["body"]["storage"]["value"] = (
        "<h1>Supplier meeting</h1><p>Lead time is 26 weeks. New gasket drawing.</p>"
    )
    spec = library / "Specs" / "Torque spec.txt"
    stat = spec.stat()
    spec.write_text("The flange torque limit is 12 Nm. Spindle runout is checked.", encoding="utf-8")
    os.utime(spec, (stat.st_atime + 60, stat.st_mtime + 60))
    assert oil.ok("note", "edit", str(source_ids["OIL-1"]), "--text", "Tantalum allocation confirmed.") == (
        f"Updated note #{source_ids['OIL-1']}\n"
    )

    # 6. The second sync reports the four changed remote sources and stores a version or entry for each.
    second = oil.ok("sync", "--all")
    assert second.startswith("Sync all: ok — 5 checked, 4 changed, 2 new versions, 2 new entries, 0 errors")
    changed_lines = [line for line in second.splitlines() if line.startswith("  changed ")]
    assert sorted(re.sub(r"^  changed\s+", "", line) for line in changed_lines) == sorted(
        [
            "email_thread: Inductor lead time [Outlook]",
            "jira_issue: ABC-123 [Jira]",
            "confluence_page: Supplier meeting notes [Confluence]",
            "file: Torque spec.txt [SharePoint]",
        ]
    )
    after = {key: counts(oil, source_id) for key, source_id in source_ids.items()}
    assert after["OIL-1"][0] == before["OIL-1"][0] + 1
    assert after["OIL-2"][1] == before["OIL-2"][1] + 1
    assert after["OIL-3"][1] == before["OIL-3"][1] + 1
    assert after["OIL-4"][0] == before["OIL-4"][0] + 1
    assert after["OIL-5"][0] == before["OIL-5"][0] + 1
    for key in ("OIL-4", "OIL-5"):
        versions = oil.ok("source", "show", str(source_ids[key]), "--versions")
        assert re.search(r"^  v1  ", versions, re.MULTILINE) and re.search(r"^  v2  ", versions, re.MULTILINE)
    for key in ("OIL-2", "OIL-3"):
        entries = oil.ok("source", "show", str(source_ids[key]), "--entries")
        assert len(entries.split("\nEntries:\n", 1)[1].splitlines()) == after[key][1]
    diff = oil.ok("source", "show", str(source_ids["OIL-4"]), "--diff", "1", "2")
    assert "+Lead time is 26 weeks. New gasket drawing." in diff

    for word, key in NEW_WORDS.items():
        assert oil.search_keys(f'text ~ "{word}"') == [key], word

    third = oil.ok("sync", "--all")
    assert third.startswith("Sync all: ok — 5 checked, 0 changed, 0 new versions, 0 new entries, 0 errors")

    # 7. Every changed field of an issue shows up in its history, one line per field.
    start = history_of(oil, "OIL-6")
    assert start == [("issue", "create", "-", "OIL-6")]
    oil.ok(
        "issue",
        "edit",
        "OIL-6",
        "--summary",
        "Issue 6 renamed",
        "--priority",
        "P1",
        "--assignee",
        "me",
        "--due",
        "2026-12-01",
        "--add-label",
        "urgent",
        "--status",
        "Blocked",
        "--reason",
        "Waiting for PPAP",
    )
    history = history_of(oil, "OIL-6")[len(start) :]
    assert Counter((field, op) for field, op, _, _ in history) == Counter(
        {
            ("summary", "set"): 1,
            ("priority", "set"): 1,
            ("assignee", "set"): 1,
            ("due_date", "set"): 1,
            ("label", "add"): 1,
            ("status", "set"): 1,
            ("blocked_reason", "set"): 1,
        }
    )
    by_field = {field: (old, new) for field, _, old, new in history}
    assert by_field["summary"] == ("Issue 6 for the supplier review", "Issue 6 renamed")
    assert by_field["priority"] == ("P2", "P1")
    assert by_field["assignee"][0] == "-" and by_field["assignee"][1] != "-"
    assert by_field["due_date"] == ("2026-11-11", "2026-12-01")
    assert by_field["label"] == ("-", "urgent")
    assert by_field["status"] == ("New", "Blocked")
    assert by_field["blocked_reason"] == ("-", "Waiting for PPAP")
    assert oil.search_keys("status = Blocked") == ["OIL-6"]

    # 8. The configuration check passes for all five connectors, the manual one included.
    checked = oil.ok("connector", "check")
    headers = [line for line in checked.splitlines() if not line.startswith(" ")]
    assert headers == [
        "Confluence (confluence)",
        "Jira (jira)",
        "Manual (manual)",
        "Outlook (outlook)",
        "SharePoint (sharepoint)",
    ]
    assert "[FAIL]" not in checked
    assert checked.count("[ok] credential stored") == 2
    assert TOKEN not in oil.transcript
