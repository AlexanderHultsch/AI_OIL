import copy
from typing import Any

import pytest
import sqlalchemy

from ai_oil import secrets
from ai_oil.config import load_config, write_default_config
from ai_oil.connectors.atlassian import AtlassianClient
from ai_oil.connectors.jira import JiraConnector
from ai_oil.domain import issues, people, projects
from ai_oil.errors import ValidationError
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ChildData,
    ConnectorConfig,
    EntryData,
    IdentityRef,
    ObjectMeta,
    ObjectState,
    SourceRef,
)
from ai_oil.sources.sync import SyncScope, run_sync

BASE = "https://acme.atlassian.net"
EMAIL = "alex@example.com"
CRED = "jira-token"
ISSUE_ID = "10001"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def mention(account_id: str, text: str) -> dict:
    return {"type": "mention", "attrs": {"id": account_id, "text": text}}


def paragraph(*content: dict) -> dict:
    return {"type": "paragraph", "content": list(content)}


def adf(*blocks: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def text_node(text: str) -> dict:
    return {"type": "text", "text": text}


JANE = {"accountId": "acc-jane", "displayName": "Jane Doe"}
ALEX = {"accountId": "acc-alex", "displayName": "Alex"}


def issue_payload() -> dict:
    return {
        "id": ISSUE_ID,
        "key": "ABC-123",
        "fields": {
            "summary": "Inductor lead time",
            "description": adf(
                paragraph(text_node("Lead time is 26 weeks.")),
                paragraph(text_node("Confirm the tolerance.")),
            ),
            "status": {"name": "In Progress"},
            "assignee": JANE,
            "reporter": ALEX,
            "priority": {"name": "High"},
            "labels": ["supplier", "l301"],
            "components": [{"name": "Power"}],
            "fixVersions": [{"name": "B-Sample"}],
            "duedate": "2026-04-15",
            "resolution": None,
            "updated": "2026-03-05T10:15:30.123+0100",
            "attachment": [
                {
                    "id": "40001",
                    "filename": "drawing.pdf",
                    "size": 20480,
                    "mimeType": "application/pdf",
                    "author": JANE,
                    "created": "2026-03-01T09:00:00.000+0100",
                    "content": f"{BASE}/rest/api/3/attachment/content/40001",
                },
                {
                    "id": "40002",
                    "filename": "photo.png",
                    "size": 512,
                    "mimeType": "image/png",
                    "author": ALEX,
                    "created": "2026-03-02T09:00:00.000+0100",
                    "content": f"{BASE}/rest/api/3/attachment/content/40002",
                },
            ],
        },
    }


def comments_payload() -> list[dict]:
    return [
        {
            "id": "20001",
            "author": JANE,
            "body": adf(
                paragraph(
                    text_node("Hi "), mention("acc-alex", "@Alex"), text_node(" please confirm torque 12 Nm.")
                )
            ),
            "created": "2026-03-02T09:00:00.000+0100",
            "updated": "2026-03-02T09:00:00.000+0100",
        },
        {
            "id": "20002",
            "author": ALEX,
            "body": adf(paragraph(text_node("Tolerance is 5 percent."))),
            "created": "2026-03-03T11:30:00.000+0100",
            "updated": "2026-03-03T11:30:00.000+0100",
        },
    ]


def histories_payload() -> list[dict]:
    return [
        {
            "id": "30001",
            "author": JANE,
            "created": "2026-03-01T08:00:00.000+0100",
            "items": [
                {
                    "field": "status",
                    "fromString": "To Do",
                    "toString": "In Progress",
                    "from": "10000",
                    "to": "10001",
                },
                {
                    "field": "assignee",
                    "fromString": None,
                    "toString": "Jane Doe",
                    "from": None,
                    "to": "acc-jane",
                },
            ],
        },
        {
            "id": "30002",
            "author": ALEX,
            "created": "2026-03-04T14:00:00.000+0100",
            "items": [
                {"field": "priority", "fromString": "Medium", "toString": "High", "from": "3", "to": "2"}
            ],
        },
    ]


class FakeSite:
    """A scripted Jira Cloud site; `session` answers REST calls and logs them."""

    def __init__(self) -> None:
        self.issue = issue_payload()
        self.comments = comments_payload()
        self.histories = histories_payload()
        self.projects = {"ABC": {"key": "ABC", "name": "Alpha Bravo Charlie"}}
        self.me = {"accountId": "acc-alex", "displayName": "Alex"}
        self.search_hits: list[dict] = []
        self.missing_issue = False
        self.status_for_all: int | None = None
        self.calls: list[tuple[str, dict]] = []
        self.session = self

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        path = url.removeprefix(BASE)
        params = dict(kwargs.get("params") or {})
        self.calls.append((path, params))
        if self.status_for_all is not None:
            return FakeResponse(self.status_for_all)
        if path == "/rest/api/3/myself":
            return FakeResponse(payload=self.me)
        if path.startswith("/rest/api/3/project/"):
            project = self.projects.get(path.rsplit("/", 1)[1])
            return FakeResponse(payload=project) if project else FakeResponse(404)
        if path == "/rest/api/3/search/jql":
            return FakeResponse(payload={"issues": self.search_hits})
        if self.missing_issue and path.startswith("/rest/api/3/issue/"):
            return FakeResponse(404)
        if path.endswith("/comment"):
            return FakeResponse(payload=self._page("comments", self.comments, params))
        if path.endswith("/changelog"):
            return FakeResponse(payload=self._page("values", self.histories, params))
        if path in (f"/rest/api/3/issue/{ISSUE_ID}", "/rest/api/3/issue/ABC-123"):
            return FakeResponse(payload=copy.deepcopy(self.issue))
        return FakeResponse(404)

    @staticmethod
    def _page(values_key: str, items: list[dict], params: dict) -> dict:
        start = params["startAt"]
        return {"startAt": start, "total": len(items), values_key: copy.deepcopy(items[start : start + 1])}

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def config(settings: dict | None = None, connector_id: int = 1) -> ConnectorConfig:
    values = {"base_url": BASE, "email": EMAIL} if settings is None else settings
    return ConnectorConfig(connector_id, "jira", "Jira", True, values, CRED)


def make(site: FakeSite, settings: dict | None = None) -> JiraConnector:
    client = AtlassianClient(BASE, EMAIL, CRED, session=site.session, sleep=lambda _: None)  # type: ignore[arg-type]
    return JiraConnector(config(settings), client=client)


def state(external_id: str = ISSUE_ID, cursor: str | None = None) -> ObjectState:
    return ObjectState(
        id=1,
        connector_id=1,
        object_type="jira_issue",
        external_id=external_id,
        title=None,
        display_ref="ABC-123",
        locator=f"{BASE}/browse/ABC-123",
        cursor=cursor,
        parent_id=None,
        latest_content_hash=None,
    )


@pytest.fixture(autouse=True)
def stored_token() -> None:
    secrets.set_secret(CRED, "s3cr3t-api-token")


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


EXPECTED_TEXT = (
    "ABC-123: Inductor lead time\n"
    "\n"
    "Status: In Progress\n"
    "Assignee: Jane Doe\n"
    "Reporter: Alex\n"
    "Priority: High\n"
    "Due: 2026-04-15\n"
    "Labels: supplier, l301\n"
    "Components: Power\n"
    "Fix versions: B-Sample\n"
    "Resolution: -\n"
    "\n"
    "Lead time is 26 weeks.\n"
    "\n"
    "Confirm the tolerance."
)


# --- settings --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"email": EMAIL}, "base_url"),
        ({"base_url": BASE}, "email"),
        ({"base_url": "acme", "email": EMAIL}, "base_url"),
        ({"base_url": 5, "email": EMAIL}, "base_url"),
        ({"base_url": BASE, "email": " "}, "email"),
        ({"base_url": BASE, "email": EMAIL, "projects": "ABC"}, "projects"),
        ({"base_url": BASE, "email": EMAIL, "projects": [1]}, "projects"),
        ({"base_url": BASE, "email": EMAIL, "ca_bundle": 3}, "ca_bundle"),
        ({"base_url": BASE, "email": EMAIL, "timeout": 0}, "timeout"),
        ({"base_url": BASE, "email": EMAIL, "timeout": "slow"}, "timeout"),
    ],
)
def test_invalid_settings_are_rejected_naming_the_key(settings: dict, key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        JiraConnector(config(settings))


def test_the_default_client_is_an_atlassian_client_and_does_no_network_work() -> None:
    connector = JiraConnector(config({"base_url": BASE + "/", "email": EMAIL, "timeout": 5}))

    assert isinstance(connector._client, AtlassianClient)
    assert repr(connector._client) == f"AtlassianClient({BASE!r})"


def test_the_connector_declares_its_contract() -> None:
    assert JiraConnector.type == "jira"
    assert JiraConnector.object_types == frozenset({"jira_issue", "attachment"})
    assert JiraConnector.action_class == "A"
    assert JiraConnector.max_parallel == 4


# --- parse_reference -------------------------------------------------------------------------------------


def expected_ref(key: str = "ABC-123") -> SourceRef:
    return SourceRef("jira_issue", key, display_ref=key, locator=f"{BASE}/browse/{key}")


@pytest.mark.parametrize(
    "text",
    [
        "ABC-123",
        "  ABC-123\n",
        f"{BASE}/browse/ABC-123",
        f"{BASE}/browse/ABC-123/",
        "HTTPS://ACME.atlassian.net/browse/ABC-123",
        f"{BASE}/jira/software/projects/ABC/boards/1?selectedIssue=ABC-123",
        f"{BASE}/jira/software/c/projects/ABC/boards/1/backlog?selectedIssue=ABC-123&view=x",
        f"{BASE}/jira/software/projects/ABC/issues/ABC-123",
        f"{BASE}/jira/core/projects/ABC/issues/ABC-123?filter=x",
    ],
)
def test_references_to_one_issue(text: str, site: FakeSite) -> None:
    assert make(site).parse_reference(text) == expected_ref()
    assert site.calls == []


def test_an_underscore_key_is_accepted(site: FakeSite) -> None:
    assert make(site).parse_reference("MB_32829-7") == expected_ref("MB_32829-7")


@pytest.mark.parametrize(
    "text",
    [
        "abc-123",
        "ABC-",
        "A-1",
        "ABC-123 and more",
        "",
        "https://other.atlassian.net/browse/ABC-123",
        "https://other.atlassian.net/jira/software/projects/ABC/boards/1?selectedIssue=ABC-123",
        f"{BASE}/browse/abc-123",
        f"{BASE}/wiki/spaces/ABC/pages/123",
        f"{BASE}/jira/software/projects/ABC/boards/1",
        f"{BASE}/jira/software/projects/ABC/boards/1?selectedIssue=abc-1",
        "ftp://acme.atlassian.net/browse/ABC-123",
        "https://example.com/spec",
    ],
)
def test_other_text_is_not_a_jira_reference(text: str, site: FakeSite) -> None:
    assert make(site).parse_reference(text) is None


# --- resolve ---------------------------------------------------------------------------------------------


def test_resolve_stores_the_numeric_id_not_the_key(site: FakeSite) -> None:
    meta = make(site).resolve(expected_ref())

    assert meta == ObjectMeta(
        "jira_issue",
        ISSUE_ID,
        title="Inductor lead time",
        display_ref="ABC-123",
        locator=f"{BASE}/browse/ABC-123",
    )
    assert site.calls == [("/rest/api/3/issue/ABC-123", {"fields": "summary"})]


def test_resolve_uses_the_key_of_the_response(site: FakeSite) -> None:
    site.issue["key"] = "NEW-9"

    meta = make(site).resolve(expected_ref())

    assert (meta.external_id, meta.display_ref, meta.locator) == (ISSUE_ID, "NEW-9", f"{BASE}/browse/NEW-9")


def test_resolve_of_a_missing_issue_is_rejected(site: FakeSite) -> None:
    site.missing_issue = True

    with pytest.raises(ValidationError, match="Jira issue ABC-123 not found or not visible"):
        make(site).resolve(expected_ref())


def test_resolve_rejects_other_object_types(site: FakeSite) -> None:
    with pytest.raises(ValidationError, match="web_link"):
        make(site).resolve(SourceRef("web_link", "https://example.com"))


# --- fetch -----------------------------------------------------------------------------------------------


def test_fetch_requests_the_issue_with_all_version_fields(site: FakeSite) -> None:
    make(site).fetch(state())

    path, params = site.calls[0]
    assert path == f"/rest/api/3/issue/{ISSUE_ID}"
    assert params["fields"].split(",") == [
        "summary",
        "description",
        "status",
        "assignee",
        "reporter",
        "priority",
        "labels",
        "components",
        "fixVersions",
        "duedate",
        "resolution",
        "updated",
        "attachment",
    ]


def test_fetch_builds_the_version(site: FakeSite) -> None:
    result = make(site).fetch(state())

    assert result.status == "ok"
    version = result.version
    assert version is not None
    assert version.text == EXPECTED_TEXT
    assert version.metadata == {
        "key": "ABC-123",
        "status": "In Progress",
        "assignee": "Jane Doe",
        "assignee_account_id": "acc-jane",
        "reporter": "Alex",
        "priority": "High",
        "due": "2026-04-15",
        "labels": ["supplier", "l301"],
        "components": ["Power"],
        "fix_versions": ["B-Sample"],
        "resolution": None,
    }
    assert "updated" not in version.metadata
    assert version.source_modified_at == "2026-03-05T09:15:30.123Z"
    assert version.source_version_ref == "2026-03-05T10:15:30.123+0100"
    assert (result.cursor, result.title) == ("2026-03-05T10:15:30.123+0100", "Inductor lead time")


def test_fetch_of_an_unassigned_resolved_issue_without_description(site: FakeSite) -> None:
    fields = site.issue["fields"]
    fields.update(
        description=None,
        assignee=None,
        priority=None,
        duedate=None,
        labels=[],
        components=[],
        fixVersions=[],
        resolution={"name": "Done"},
    )

    version = make(site).fetch(state()).version

    assert version is not None
    assert version.text == (
        "ABC-123: Inductor lead time\n"
        "\n"
        "Status: In Progress\n"
        "Assignee: -\n"
        "Reporter: Alex\n"
        "Priority: -\n"
        "Due: -\n"
        "Labels: -\n"
        "Components: -\n"
        "Fix versions: -\n"
        "Resolution: Done"
    )
    assert (version.metadata["assignee"], version.metadata["assignee_account_id"]) == (None, None)
    assert version.metadata["resolution"] == "Done"


def test_fetch_reads_comments_over_all_pages(site: FakeSite) -> None:
    result = make(site).fetch(state())

    comment_calls = [params for path, params in site.calls if path.endswith("/comment")]
    assert [call["startAt"] for call in comment_calls] == [0, 1]
    assert all(call["orderBy"] == "created" for call in comment_calls)
    comments = [entry for entry in result.entries if entry.entry_type == "jira_comment"]
    assert comments == [
        EntryData(
            "jira_comment",
            "20001",
            "2026-03-02T08:00:00.000Z",
            "Hi @Alex please confirm torque 12 Nm.",
            author=IdentityRef("atlassian", "acc-jane", "Jane Doe"),
            metadata={"updated": "2026-03-02T08:00:00.000Z", "mentions": ["acc-alex"]},
        ),
        EntryData(
            "jira_comment",
            "20002",
            "2026-03-03T10:30:00.000Z",
            "Tolerance is 5 percent.",
            author=IdentityRef("atlassian", "acc-alex", "Alex"),
            metadata={"updated": "2026-03-03T10:30:00.000Z", "mentions": []},
        ),
    ]


def test_fetch_reads_one_change_entry_per_changelog_item(site: FakeSite) -> None:
    result = make(site).fetch(state())

    changes = [entry for entry in result.entries if entry.entry_type == "jira_change"]
    assert [(c.external_id, c.occurred_at, c.text) for c in changes] == [
        ("30001:0", "2026-03-01T07:00:00.000Z", "status: To Do → In Progress"),
        ("30001:1", "2026-03-01T07:00:00.000Z", "assignee: - → Jane Doe"),
        ("30002:0", "2026-03-04T13:00:00.000Z", "priority: Medium → High"),
    ]
    assert changes[0].author == IdentityRef("atlassian", "acc-jane", "Jane Doe")
    assert changes[2].author == IdentityRef("atlassian", "acc-alex", "Alex")
    assert changes[0].metadata == {
        "field": "status",
        "from": "To Do",
        "to": "In Progress",
        "from_id": "10000",
        "to_id": "10001",
    }
    assert changes[1].metadata == {
        "field": "assignee",
        "from": None,
        "to": "Jane Doe",
        "from_id": None,
        "to_id": "acc-jane",
    }
    assert ("/rest/api/3/issue/10001/changelog", {"startAt": 0, "maxResults": 100}) in site.calls


def test_an_author_without_an_account_id_has_no_identity(site: FakeSite) -> None:
    site.comments[0]["author"] = {"displayName": "Former user"}
    del site.comments[1]["author"]

    result = make(site).fetch(state())

    assert [e.author for e in result.entries if e.entry_type == "jira_comment"] == [None, None]


def test_fetch_returns_attachments_as_children_with_metadata_only(site: FakeSite) -> None:
    result = make(site).fetch(state())

    assert len(result.children) == 2
    first = result.children[0]
    assert isinstance(first, ChildData)
    assert first.meta == ObjectMeta(
        "attachment",
        f"{ISSUE_ID}#40001",
        title="drawing.pdf",
        display_ref="drawing.pdf",
        locator=f"{BASE}/rest/api/3/attachment/content/40001",
        cursor="40001",
    )
    assert first.version is not None
    assert first.version.text == "drawing.pdf"
    assert first.version.metadata == {
        "filename": "drawing.pdf",
        "size": 20480,
        "mime_type": "application/pdf",
        "author": "Jane Doe",
        "created": "2026-03-01T08:00:00.000Z",
    }
    assert first.version.raw is None
    assert result.children[1].meta.external_id == f"{ISSUE_ID}#40002"
    assert "/rest/api/3/attachment/content/40001" not in site.paths()


def test_fetch_of_a_missing_issue_is_gone(site: FakeSite) -> None:
    site.missing_issue = True

    result = make(site).fetch(state())

    assert result.status == "gone"
    assert result.version is None
    assert result.entries == ()


def test_fetch_always_fetches_even_with_an_unchanged_cursor(site: FakeSite) -> None:
    result = make(site).fetch(state(cursor="2026-03-05T10:15:30.123+0100"))

    assert result.version is not None
    assert len(result.entries) == 5


# --- end to end through the store and the sync engine ----------------------------------------------------


@pytest.fixture
def linked(engine: sqlalchemy.Engine, tmp_path, site: FakeSite):
    home = tmp_path / "sync_home"
    write_default_config(home)
    with engine.begin() as conn:
        me = people.create_person(conn, "Alex", is_me=True)
        people.add_identity(conn, me.id, "email", EMAIL)
        projects.create_project(conn, "OIL", "Open issues")
        issue = issues.create_issue(conn, "OIL", "Inductor delivery", "Chase the supplier", actor_id=me.id)
        row = registry.create_connector(
            conn, "jira", "Jira", settings={"base_url": BASE, "email": EMAIL}, credential_ref=CRED
        )
        connector = make(site)
        ref = connector.parse_reference("ABC-123")
        assert ref is not None
        object_id = store.upsert_object(conn, row.id, connector.resolve(ref))
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)

    def factory(connector_row: registry.ConnectorRow) -> JiraConnector:
        return make(site)

    def sync():
        return run_sync(
            engine, SyncScope("source", object_id), config=load_config(home), connector_factory=factory
        )

    return engine, object_id, sync


def test_the_first_sync_stores_version_entries_and_children(linked) -> None:
    engine, object_id, sync = linked

    report = sync()

    assert report.status == "ok"
    # one issue version and two attachment versions
    assert (report.stats["new_versions"], report.stats["new_entries"]) == (3, 5)
    with engine.connect() as conn:
        obj = store.get_object(conn, object_id)
        [version] = store.list_versions(conn, object_id)
        entries = store.list_entries(conn, object_id)
        children = store.list_children(conn, object_id)
    assert (obj.external_id, obj.display_ref, obj.title, obj.fetch_status) == (
        ISSUE_ID,
        "ABC-123",
        "Inductor lead time",
        "ok",
    )
    assert obj.cursor == "2026-03-05T10:15:30.123+0100"
    assert version.text == EXPECTED_TEXT
    assert sorted((e.entry_type, e.external_id) for e in entries) == [
        ("jira_change", "30001:0"),
        ("jira_change", "30001:1"),
        ("jira_change", "30002:0"),
        ("jira_comment", "20001"),
        ("jira_comment", "20002"),
    ]
    assert sorted(child.external_id for child in children) == [f"{ISSUE_ID}#40001", f"{ISSUE_ID}#40002"]


def test_a_new_comment_adds_an_entry_but_no_version(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.comments.append(
        {
            "id": "20003",
            "author": JANE,
            "body": adf(paragraph(text_node("Ordered."))),
            "created": "2026-03-06T09:00:00.000+0100",
            "updated": "2026-03-06T09:00:00.000+0100",
        }
    )
    site.issue["fields"]["updated"] = "2026-03-06T09:00:00.000+0100"

    report = sync()

    assert report.status == "ok"
    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 1)
    with engine.connect() as conn:
        assert len(store.list_versions(conn, object_id)) == 1
        assert store.get_object(conn, object_id).cursor == "2026-03-06T09:00:00.000+0100"
        assert len(store.list_entries(conn, object_id)) == 6


def test_a_status_change_creates_a_new_version(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.issue["fields"]["status"] = {"name": "Done"}
    site.issue["fields"]["updated"] = "2026-03-07T09:00:00.000+0100"
    site.histories.append(
        {
            "id": "30003",
            "author": JANE,
            "created": "2026-03-07T09:00:00.000+0100",
            "items": [
                {
                    "field": "status",
                    "fromString": "In Progress",
                    "toString": "Done",
                    "from": "10001",
                    "to": "10002",
                }
            ],
        }
    )

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (1, 1)
    with engine.connect() as conn:
        first, second = store.list_versions(conn, object_id)
    assert (first.metadata["status"], second.metadata["status"]) == ("In Progress", "Done")
    assert "Status: Done" in second.text
    assert second.source_modified_at == "2026-03-07T08:00:00.000Z"


def test_an_unchanged_issue_stores_nothing_new(linked) -> None:
    _, _, sync = linked
    sync()

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 0)


def test_an_edited_comment_is_stored_as_a_new_revision(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.comments[1]["body"] = adf(paragraph(text_node("Tolerance is 3 percent.")))
    site.comments[1]["updated"] = "2026-03-08T09:00:00.000+0100"
    site.issue["fields"]["updated"] = "2026-03-08T09:00:00.000+0100"

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 1)
    with engine.connect() as conn:
        revisions = [
            (e.revision, e.text)
            for e in store.list_entries(conn, object_id, all_revisions=True)
            if e.external_id == "20002"
        ]
        latest = [e.text for e in store.list_entries(conn, object_id) if e.external_id == "20002"]
    assert revisions == [(1, "Tolerance is 5 percent."), (2, "Tolerance is 3 percent.")]
    assert latest == ["Tolerance is 3 percent."]


def test_an_issue_that_disappears_becomes_gone_and_keeps_its_versions(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.missing_issue = True

    report = sync()

    assert report.status == "ok"
    with engine.connect() as conn:
        assert store.get_object(conn, object_id).fetch_status == "gone"
        assert len(store.list_versions(conn, object_id)) == 1


# --- check -----------------------------------------------------------------------------------------------


def test_check_reports_the_signed_in_user_and_the_projects(site: FakeSite) -> None:
    site.projects["XYZ"] = {"key": "XYZ", "name": "Hidden"}
    del site.projects["XYZ"]

    report = make(site, {"base_url": BASE, "email": EMAIL, "projects": ["ABC", "XYZ"]}).check()

    assert [(c.name, c.ok, c.detail) for c in report.checks] == [
        ("auth", True, "signed in as Alex"),
        ("project ABC", True, "Alpha Bravo Charlie"),
        ("project XYZ", False, "not found or not visible"),
    ]
    assert not report.ok


def test_check_without_projects_only_tests_the_login(site: FakeSite) -> None:
    report = make(site).check()

    assert [(c.name, c.ok) for c in report.checks] == [("auth", True)]
    assert report.ok
    assert site.paths() == ["/rest/api/3/myself"]


def test_check_stops_at_an_authentication_failure(site: FakeSite) -> None:
    site.status_for_all = 401

    report = make(site, {"base_url": BASE, "email": EMAIL, "projects": ["ABC"]}).check()

    [check] = report.checks
    assert (check.name, check.ok) == ("auth", False)
    assert "authentication failed (401)" in check.detail
    assert site.paths() == ["/rest/api/3/myself"]


def test_check_reports_a_missing_token(site: FakeSite) -> None:
    secrets.delete_secret(CRED)

    [check] = make(site).check().checks

    assert (check.name, check.ok) == ("auth", False)
    assert "no API token stored" in check.detail
    assert "s3cr3t-api-token" not in check.detail


# --- search_candidates -----------------------------------------------------------------------------------


def hits() -> list[dict]:
    return [
        {"id": "10001", "key": "ABC-123", "fields": {"summary": "Inductor lead time", "updated": "x"}},
        {"id": "10007", "key": "ABC-7", "fields": {"summary": "Capacitor"}},
    ]


def test_search_candidates_default_window(site: FakeSite) -> None:
    site.search_hits = hits()

    found = make(site).search_candidates(CandidateQuery("inductor"))

    assert site.calls == [
        (
            "/rest/api/3/search/jql",
            {
                "jql": 'text ~ "inductor" AND updated >= -30d ORDER BY updated DESC',
                "maxResults": 20,
                "fields": "summary,updated",
            },
        )
    ]
    assert found == [
        ObjectMeta(
            "jira_issue",
            "10001",
            title="Inductor lead time",
            display_ref="ABC-123",
            locator=f"{BASE}/browse/ABC-123",
        ),
        ObjectMeta(
            "jira_issue", "10007", title="Capacitor", display_ref="ABC-7", locator=f"{BASE}/browse/ABC-7"
        ),
    ]


def test_search_candidates_with_projects_since_and_limit(site: FakeSite) -> None:
    make(site, {"base_url": BASE, "email": EMAIL, "projects": ["ABC", "MB"]}).search_candidates(
        CandidateQuery("lead time", since="2026-02-01T10:00:00.000Z", limit=5)
    )

    [(_, params)] = site.calls
    assert params["jql"] == (
        'text ~ "lead time" AND project in (ABC, MB) AND updated >= "2026-02-01" ORDER BY updated DESC'
    )
    assert params["maxResults"] == 5


def test_search_candidates_escapes_quotes_and_backslashes(site: FakeSite) -> None:
    make(site).search_candidates(CandidateQuery('say "hi" \\ there'))

    [(_, params)] = site.calls
    assert params["jql"] == 'text ~ "say \\"hi\\" \\\\ there" AND updated >= -30d ORDER BY updated DESC'


def test_search_candidates_without_hits(site: FakeSite) -> None:
    assert make(site).search_candidates(CandidateQuery("nothing")) == []


# --- registration ----------------------------------------------------------------------------------------


def test_the_connector_is_registered_through_its_entry_point() -> None:
    assert registry.available_connector_types()["jira"] is JiraConnector
