from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy

from ai_oil import secrets
from ai_oil.config import load_config
from ai_oil.connectors import manual
from ai_oil.connectors.atlassian import AtlassianClient
from ai_oil.connectors.confluence import ConfluenceConnector
from ai_oil.db.engine import create_db_engine
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import ConnectorConfig

BASE = "https://acme.atlassian.net"
WIKI = BASE + "/wiki"
CRED = "confluence-cli-token"
PAGE_URL = f"{WIKI}/spaces/ABC/pages/123456/Supplier+meeting+notes"


def _comment(comment_id: str, text: str, author: str) -> dict:
    return {
        "id": comment_id,
        "body": {"storage": {"representation": "storage", "value": f"<p>{text}</p>"}},
        "version": {"createdAt": "2026-03-02T09:00:00.000+01:00", "number": 1, "authorId": author},
    }


PAYLOADS: dict[str, Any] = {
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
    "/wiki/api/v2/pages/123456/footer-comments": {
        "results": [_comment("5001", "Please confirm torque.", "acc-jane")],
        "_links": {},
    },
    "/wiki/api/v2/pages/123456/inline-comments": {"results": [], "_links": {}},
    "/wiki/api/v2/footer-comments/5001/children": {
        "results": [_comment("5002", "Confirmed.", "acc-alex")],
        "_links": {},
    },
    "/wiki/api/v2/footer-comments/5002/children": {"results": [], "_links": {}},
}
TINY_LINKS = {"/wiki/x/AbC": PAGE_URL}


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, url: str = "") -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self.url = url
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        path = url.removeprefix(BASE)
        if path in TINY_LINKS:
            return FakeResponse(200, url=TINY_LINKS[path])
        payload = PAYLOADS.get(path)
        return FakeResponse(404) if payload is None else FakeResponse(200, payload)


class FixtureConfluence(ConfluenceConnector):
    """The Confluence plugin with a REST client that talks to the fixture site instead of the network."""

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        client = AtlassianClient(
            WIKI,
            "alex@example.com",
            config.credential_ref,
            session=FakeSession(),  # type: ignore[arg-type]
        )
        super().__init__(config, client=client)


@pytest.fixture(autouse=True)
def fixture_confluence_registered() -> Iterator[None]:
    registry.register_connector_type(FixtureConfluence)
    yield
    registry.unregister_connector_type("confluence")


@pytest.fixture
def confluence_connector(home) -> None:
    secrets.set_secret(CRED, "s3cr3t-api-token")
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.begin() as conn:
            registry.create_connector(
                conn,
                "confluence",
                "Confluence",
                settings={"base_url": WIKI, "email": "alex@example.com"},
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


def page_objects(conn: sqlalchemy.Connection) -> list[sqlalchemy.Row]:
    return list(
        conn.execute(
            sqlalchemy.text(
                "SELECT id, object_type, external_id, display_ref, fetch_status FROM source_object"
                " WHERE object_type IN ('confluence_page', 'web_link')"
            )
        ).all()
    )


def test_link_a_page_url_stores_the_page_and_finds_it_by_comment_text(
    oil, db, capsys, issue, confluence_connector
) -> None:
    code, out, err = run(oil, capsys, "link", issue, PAGE_URL)

    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "Linked confluence_page: Supplier meeting notes to OIL-1"
    with db() as conn:
        [obj] = page_objects(conn)
        versions = store.list_versions(conn, obj.id)
        entries = store.list_entries(conn, obj.id)
        assert linking.linked_object_ids(conn, issue) == [obj.id]
    assert (obj.object_type, obj.external_id, obj.display_ref, obj.fetch_status) == (
        "confluence_page",
        "123456",
        "Supplier meeting notes",
        "ok",
    )
    assert f"Sync source:{obj.id}: ok" in out
    assert "1 new versions, 2 new entries" in out
    assert [v.text for v in versions] == [
        "Supplier meeting notes\n\nSupplier meeting\nLead time is 26 weeks."
    ]
    assert sorted((e.entry_type, e.external_id) for e in entries) == [
        ("confluence_comment", "footer:5001"),
        ("confluence_comment", "footer:5002"),
    ]

    code, found, _ = run(oil, capsys, "search", 'source = confluence AND text ~ "torque"')
    assert code == 0
    assert "OIL-1" in found


def test_the_confluence_connector_wins_over_the_manual_one_for_the_same_url(
    oil, db, capsys, issue, confluence_connector
) -> None:
    with db() as conn:
        manual_row = manual.ensure_manual_connector(conn)
        assert registry.instantiate(manual_row).parse_reference(PAGE_URL) is not None

    code, out, _ = run(oil, capsys, "link", issue, PAGE_URL)

    assert code == 0
    with db() as conn:
        [obj] = page_objects(conn)
    assert obj.object_type == "confluence_page"
    assert "web_link" not in out


def test_link_a_tiny_link_resolves_it_to_the_page(oil, db, capsys, issue, confluence_connector) -> None:
    code, out, _ = run(oil, capsys, "link", issue, f"{WIKI}/x/AbC")

    assert code == 0
    assert out.splitlines()[0] == "Linked confluence_page: Supplier meeting notes to OIL-1"
    with db() as conn:
        [obj] = page_objects(conn)
    assert (obj.object_type, obj.external_id) == ("confluence_page", "123456")
