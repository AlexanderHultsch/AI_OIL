from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy

from ai_oil import secrets
from ai_oil.config import load_config
from ai_oil.connectors.atlassian import AtlassianClient
from ai_oil.connectors.jira import JiraConnector
from ai_oil.db.engine import create_db_engine
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import ConnectorConfig

BASE = "https://acme.atlassian.net"
CRED = "jira-cli-token"
PAYLOADS: dict[str, Any] = {
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
            "attachment": [
                {
                    "id": "40001",
                    "filename": "drawing.pdf",
                    "size": 20480,
                    "mimeType": "application/pdf",
                    "author": {"accountId": "acc-alex", "displayName": "Alex"},
                    "created": "2026-03-01T09:00:00.000+0100",
                    "content": f"{BASE}/rest/api/3/attachment/content/40001",
                }
            ],
        },
    },
    "/rest/api/3/issue/10001/comment": {
        "total": 2,
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
            },
            {
                "id": "20002",
                "author": {"accountId": "acc-alex", "displayName": "Alex"},
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Confirmed."}]}],
                },
                "created": "2026-03-03T09:00:00.000+0100",
                "updated": "2026-03-03T09:00:00.000+0100",
            },
        ],
    },
    "/rest/api/3/issue/10001/changelog": {
        "total": 1,
        "values": [
            {
                "id": "30001",
                "author": {"accountId": "acc-jane", "displayName": "Jane Doe"},
                "created": "2026-03-01T08:00:00.000+0100",
                "items": [
                    {
                        "field": "status",
                        "fromString": "To Do",
                        "toString": "In Progress",
                        "from": "10000",
                        "to": "10001",
                    }
                ],
            }
        ],
    },
}


PAYLOADS["/rest/api/3/issue/10001"] = PAYLOADS["/rest/api/3/issue/ABC-123"]


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        payload = PAYLOADS.get(url.removeprefix(BASE))
        return FakeResponse(404) if payload is None else FakeResponse(200, payload)


class FixtureJira(JiraConnector):
    """The Jira plugin with a REST client that talks to the fixture site instead of the network."""

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        client = AtlassianClient(
            BASE,
            "alex@example.com",
            config.credential_ref,
            session=FakeSession(),  # type: ignore[arg-type]
        )
        super().__init__(config, client=client)


@pytest.fixture(autouse=True)
def fixture_jira_registered() -> Iterator[None]:
    registry.register_connector_type(FixtureJira)
    yield
    registry.unregister_connector_type("jira")


@pytest.fixture
def jira_connector(home) -> None:
    secrets.set_secret(CRED, "s3cr3t-api-token")
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.begin() as conn:
            registry.create_connector(
                conn,
                "jira",
                "Jira",
                settings={"base_url": BASE, "email": "alex@example.com"},
                credential_ref=CRED,
            )
    finally:
        engine.dispose()


@pytest.fixture
def issue(oil, capsys: pytest.CaptureFixture[str]) -> str:
    assert oil("project", "add", "OIL", "Open issues") == 0
    assert oil("issue", "create", "-p", "OIL", "-s", "Fit the filter", "-d", "Needs a part") == 0
    capsys.readouterr()
    return "OIL-1"


def run(oil, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = oil(*argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_link_a_jira_key_stores_the_issue_and_finds_it_by_comment_text(
    oil, db, capsys, issue, jira_connector
) -> None:
    code, out, err = run(oil, capsys, "link", issue, "ABC-123")

    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "Linked jira_issue: ABC-123 to OIL-1"
    with db() as conn:
        [obj] = conn.execute(
            sqlalchemy.text(
                "SELECT id, external_id, display_ref, fetch_status FROM source_object"
                " WHERE object_type = 'jira_issue'"
            )
        ).all()
        versions = store.list_versions(conn, obj.id)
        entries = store.list_entries(conn, obj.id)
        children = store.list_children(conn, obj.id)
        assert linking.linked_object_ids(conn, issue) == [obj.id]
    assert (obj.external_id, obj.display_ref, obj.fetch_status) == ("10001", "ABC-123", "ok")
    assert f"Sync source:{obj.id}: ok" in out
    assert "2 new versions, 3 new entries" in out
    assert [v.text.splitlines()[0] for v in versions] == ["ABC-123: Inductor lead time"]
    assert sorted(e.entry_type for e in entries) == ["jira_change", "jira_comment", "jira_comment"]
    assert [c.external_id for c in children] == ["10001#40001"]

    code, found, _ = run(oil, capsys, "search", 'source = jira AND text ~ "torque"')
    assert code == 0
    assert "OIL-1" in found


def test_link_a_browse_url_finds_the_same_issue(oil, db, capsys, issue, jira_connector) -> None:
    code, out, _ = run(oil, capsys, "link", issue, f"{BASE}/browse/ABC-123")

    assert code == 0
    assert out.splitlines()[0] == "Linked jira_issue: ABC-123 to OIL-1"
