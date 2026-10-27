import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy

from ai_oil import secrets
from ai_oil.config import load_config, write_default_config
from ai_oil.connectors.atlassian import AtlassianClient
from ai_oil.connectors.confluence import ConfluenceConnector, storage_mentions, storage_to_text
from ai_oil.domain import issues, people, projects
from ai_oil.errors import ValidationError
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    EntryData,
    IdentityRef,
    ObjectMeta,
    ObjectState,
    SourceRef,
)
from ai_oil.sources.sync import SyncScope, run_sync

BASE = "https://acme.atlassian.net"
WIKI = BASE + "/wiki"
EMAIL = "alex@example.com"
CRED = "confluence-token"
PAGE_ID = "123456"
PAGE_URL = f"{WIKI}/spaces/ABC/pages/{PAGE_ID}/Supplier+meeting+notes"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, url: str = "") -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self.url = url
        self._payload = payload

    def json(self) -> Any:
        return self._payload


STORAGE_BODY = (
    "<h1>Supplier meeting</h1>"
    '<p>Agreed with <ac:link><ri:user ri:account-id="acc-jane" /></ac:link> to <strong>freeze</strong>'
    ' the part <ac:emoticon ac:name="smile" />.</p>'
    "<ul><li>Lead time 26 weeks</li><li>Tolerance 5 percent</li></ul>"
    "<table><tbody><tr><th>Part</th><th>Qty</th></tr><tr><td>L301</td><td>5</td></tr></tbody></table>"
    '<ac:structured-macro ac:name="code" ac:schema-version="1">'
    '<ac:parameter ac:name="language">python</ac:parameter>'
    "<ac:plain-text-body><![CDATA[limit = 12\nunit = 'Nm']]></ac:plain-text-body></ac:structured-macro>"
    '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Heads up</ac:parameter>'
    "<ac:rich-text-body><p>Read the "
    '<ac:link><ri:page ri:content-title="Torque spec" /><ac:link-body>torque page</ac:link-body></ac:link>'
    " first.</p></ac:rich-text-body></ac:structured-macro>"
    "<ac:task-list><ac:task><ac:task-id>1</ac:task-id><ac:task-status>incomplete</ac:task-status>"
    "<ac:task-body>Order samples</ac:task-body></ac:task>"
    "<ac:task><ac:task-id>2</ac:task-id><ac:task-status>complete</ac:task-status>"
    "<ac:task-body>Book the room</ac:task-body></ac:task></ac:task-list>"
    '<ac:structured-macro ac:name="toc" />'
)
BODY_TEXT = (
    "Supplier meeting\n"
    "Agreed with @acc-jane to freeze the part :smile:.\n"
    "Lead time 26 weeks\n"
    "Tolerance 5 percent\n"
    "Part\tQty\n"
    "L301\t5\n"
    "limit = 12\n"
    "unit = 'Nm'\n"
    "Read the torque page first.\n"
    "[ ] Order samples\n"
    "[x] Book the room"
)
EXPECTED_TEXT = "Supplier meeting notes\n\n" + BODY_TEXT


def page_payload() -> dict:
    return {
        "id": PAGE_ID,
        "status": "current",
        "title": "Supplier meeting notes",
        "spaceId": "98765",
        "authorId": "acc-alex",
        "createdAt": "2026-02-01T08:00:00.000Z",
        "version": {
            "createdAt": "2026-03-05T10:15:30.123Z",
            "message": "",
            "number": 7,
            "minorEdit": False,
            "authorId": "acc-jane",
        },
        "body": {"storage": {"representation": "storage", "value": STORAGE_BODY}},
        "_links": {
            "webui": "/spaces/ABC/pages/123456/Supplier+meeting+notes",
            "editui": "/pages/resumedraft.action?draftId=123456",
            "tinyui": "/x/AbC",
        },
    }


def comment(
    comment_id: str,
    body: str,
    *,
    author: str | None = "acc-jane",
    created: str = "2026-03-02T09:00:00.000+01:00",
    number: int = 1,
    **extra: Any,
) -> dict:
    version: dict[str, Any] = {"createdAt": created, "number": number, "message": ""}
    if author is not None:
        version["authorId"] = author
    return {
        "id": comment_id,
        "status": "current",
        "pageId": PAGE_ID,
        "body": {"storage": {"representation": "storage", "value": body}},
        "version": version,
        **extra,
    }


def footer_comments() -> list[dict]:
    mention = '<p>Hi <ac:link><ri:user ri:account-id="acc-alex" /></ac:link> please confirm torque 12 Nm.</p>'
    return [
        comment("5001", mention),
        comment("5010", "<p>Ordered.</p>", created="2026-03-06T09:00:00.000+01:00"),
    ]


def inline_comments() -> list[dict]:
    return [
        comment(
            "6001",
            "<p>Is 26 weeks right?</p>",
            created="2026-03-04T12:00:00.000+01:00",
            resolutionStatus="open",
            properties={"inline-marker-ref": "marker-1", "inline-original-selection": "Lead time 26 weeks"},
        )
    ]


def replies() -> dict[tuple[str, str], list[dict]]:
    return {
        ("footer-comments", "5001"): [
            comment("5002", "<p>Confirmed.</p>", author="acc-alex", created="2026-03-03T11:30:00.000+01:00")
        ],
        ("footer-comments", "5002"): [
            comment("5003", "<p>Thanks, noted.</p>", created="2026-03-03T12:00:00.000+01:00")
        ],
        ("inline-comments", "6001"): [
            comment(
                "6002", "<p>Yes, per quote.</p>", author="acc-alex", created="2026-03-04T13:00:00.000+01:00"
            )
        ],
    }


class FakeSite:
    """A scripted Confluence Cloud site; `session` answers REST calls and logs them."""

    def __init__(self) -> None:
        self.page = page_payload()
        self.footer = footer_comments()
        self.inline = inline_comments()
        self.children = replies()
        self.spaces = {"ABC": {"id": "98765", "key": "ABC", "name": "Alpha Bravo Charlie"}}
        self.me = {"accountId": "acc-alex", "displayName": "Alex"}
        self.tiny = {"AbC": PAGE_URL}
        self.search_hits: list[dict] = []
        self.missing_page = False
        self.status_for_all: int | None = None
        self.calls: list[tuple[str, dict]] = []
        self.session = self

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        parts = urlsplit(url.removeprefix(BASE))
        params = {**{key: values[0] for key, values in parse_qs(parts.query).items()}}
        params.update(kwargs.get("params") or {})
        path = parts.path
        self.calls.append((path, params))
        if self.status_for_all is not None:
            return FakeResponse(self.status_for_all)
        if path == "/wiki/rest/api/user/current":
            return FakeResponse(payload=self.me)
        if path == "/wiki/api/v2/spaces":
            found = [space for key, space in self.spaces.items() if key == params["keys"]]
            return FakeResponse(payload={"results": found})
        if path == "/wiki/rest/api/content/search":
            return FakeResponse(payload={"results": self.search_hits, "size": len(self.search_hits)})
        if path.startswith("/wiki/x/"):
            final = self.tiny.get(path.rsplit("/", 1)[1])
            return FakeResponse(url=final) if final else FakeResponse(404)
        if path == f"/wiki/api/v2/pages/{PAGE_ID}":
            return FakeResponse(404) if self.missing_page else FakeResponse(payload=copy.deepcopy(self.page))
        if path == f"/wiki/api/v2/pages/{PAGE_ID}/footer-comments":
            return FakeResponse(payload=self._page(path, self.footer, params))
        if path == f"/wiki/api/v2/pages/{PAGE_ID}/inline-comments":
            return FakeResponse(payload=self._page(path, self.inline, params))
        if path.endswith("/children"):
            *_, collection, comment_id, _ = path.split("/")
            return FakeResponse(
                payload=self._page(path, self.children.get((collection, comment_id), []), params)
            )
        return FakeResponse(404)

    @staticmethod
    def _page(path: str, items: list[dict], params: dict) -> dict:
        assert params["body-format"] == "storage"
        start = int(params.get("cursor", 0))
        page: dict[str, Any] = {"results": copy.deepcopy(items[start : start + 1]), "_links": {}}
        if start + 1 < len(items):
            page["_links"]["next"] = f"{path}?body-format=storage&limit=100&cursor={start + 1}"
        return page

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def config(settings: dict | None = None, connector_id: int = 1) -> ConnectorConfig:
    values = {"base_url": WIKI, "email": EMAIL} if settings is None else settings
    return ConnectorConfig(connector_id, "confluence", "Confluence", True, values, CRED)


def make(site: FakeSite, settings: dict | None = None) -> ConfluenceConnector:
    client = AtlassianClient(WIKI, EMAIL, CRED, session=site.session, sleep=lambda _: None)  # type: ignore[arg-type]
    return ConfluenceConnector(config(settings), client=client)


def state(external_id: str = PAGE_ID, cursor: str | None = None) -> ObjectState:
    return ObjectState(
        id=1,
        connector_id=1,
        object_type="confluence_page",
        external_id=external_id,
        title=None,
        display_ref="Supplier meeting notes",
        locator=PAGE_URL,
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


# --- settings --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"email": EMAIL}, "base_url"),
        ({"base_url": WIKI}, "email"),
        ({"base_url": "acme", "email": EMAIL}, "base_url"),
        ({"base_url": 5, "email": EMAIL}, "base_url"),
        ({"base_url": BASE, "email": EMAIL}, "base_url"),
        ({"base_url": BASE + "/", "email": EMAIL}, "base_url"),
        ({"base_url": BASE + "/jira", "email": EMAIL}, "base_url"),
        ({"base_url": WIKI, "email": " "}, "email"),
        ({"base_url": WIKI, "email": EMAIL, "spaces": "ABC"}, "spaces"),
        ({"base_url": WIKI, "email": EMAIL, "spaces": [1]}, "spaces"),
        ({"base_url": WIKI, "email": EMAIL, "ca_bundle": 3}, "ca_bundle"),
        ({"base_url": WIKI, "email": EMAIL, "timeout": 0}, "timeout"),
        ({"base_url": WIKI, "email": EMAIL, "timeout": "slow"}, "timeout"),
    ],
)
def test_invalid_settings_are_rejected_naming_the_key(settings: dict, key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        ConfluenceConnector(config(settings))


def test_the_default_client_is_an_atlassian_client_and_does_no_network_work() -> None:
    connector = ConfluenceConnector(config({"base_url": WIKI + "/", "email": EMAIL, "timeout": 5}))

    assert isinstance(connector._client, AtlassianClient)
    assert repr(connector._client) == f"AtlassianClient({WIKI!r})"


def test_the_connector_declares_its_contract() -> None:
    assert ConfluenceConnector.type == "confluence"
    assert ConfluenceConnector.object_types == frozenset({"confluence_page"})
    assert ConfluenceConnector.action_class == "A"
    assert ConfluenceConnector.max_parallel == 4


# --- storage_to_text and storage_mentions ----------------------------------------------------------------


def test_a_realistic_page_body_is_rendered_readably() -> None:
    assert storage_to_text(STORAGE_BODY) == BODY_TEXT


@pytest.mark.parametrize(
    ("xhtml", "expected"),
    [
        ("", ""),
        ("<h1>Title</h1><p>Some <strong>bold</strong> text &amp; more.</p>", "Title\nSome bold text & more."),
        ("<ul><li>one</li><li>two</li></ul>", "one\ntwo"),
        (
            "<table><tbody><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></tbody></table>",
            "A\tB\n1\t2",
        ),
        (
            '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql</ac:parameter>'
            "<ac:plain-text-body><![CDATA[select 1 < 2;\nselect 3;]]></ac:plain-text-body>"
            "</ac:structured-macro>",
            "select 1 < 2;\nselect 3;",
        ),
        (
            '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Heads up</ac:parameter>'
            "<ac:rich-text-body><p>First.</p><p>Second.</p></ac:rich-text-body></ac:structured-macro>",
            "First.\nSecond.",
        ),
        (
            '<ac:structured-macro ac:name="toc"><ac:parameter ac:name="minLevel">2</ac:parameter>'
            "</ac:structured-macro>",
            "",
        ),
        (
            '<ac:structured-macro ac:name="panel"><ac:rich-text-body><p>Before</p>'
            '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body>'
            "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>",
            "Before\nx = 1",
        ),
        (
            '<p>See <ac:image ac:width="300"><ri:attachment ri:filename="a.png" /></ac:image></p>',
            "See [image]",
        ),
        ('<p>Nice <ac:emoticon ac:name="smile" ac:emoji-shortname=":smile:" /></p>', "Nice :smile:"),
        ('<p>Ask <ac:link><ri:user ri:account-id="acc-jane" /></ac:link>.</p>', "Ask @acc-jane."),
        (
            '<p>See <ac:link><ri:page ri:content-title="Torque spec" /><ac:link-body>the spec</ac:link-body>'
            "</ac:link>.</p>",
            "See the spec.",
        ),
        (
            '<p>See <ac:link><ri:page ri:content-title="Torque spec" '
            'ri:space-key="ABC" /><ac:plain-text-link-body><![CDATA[plain]]></ac:plain-text-link-body>'
            "</ac:link>.</p>",
            "See plain.",
        ),
        ('<p>See <ac:link><ri:page ri:content-title="Torque spec" /></ac:link>.</p>', "See Torque spec."),
        (
            "<ac:task-list>"
            "<ac:task><ac:task-id>1</ac:task-id><ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Open <strong>one</strong></ac:task-body></ac:task>"
            "<ac:task><ac:task-id>2</ac:task-id><ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Done one</ac:task-body></ac:task></ac:task-list>",
            "[ ] Open one\n[x] Done one",
        ),
    ],
)
def test_storage_to_text(xhtml: str, expected: str) -> None:
    assert storage_to_text(xhtml) == expected


def test_storage_mentions_are_in_document_order_without_duplicates() -> None:
    xhtml = (
        '<p><ac:link><ri:user ri:account-id="acc-b" /></ac:link>'
        '<ac:link><ri:user ri:account-id="acc-a" /></ac:link>'
        '<ac:link><ri:user ri:account-id="acc-b" /></ac:link>'
        '<ac:link><ri:page ri:content-title="Page" /></ac:link></p>'
    )

    assert storage_mentions(xhtml) == ["acc-b", "acc-a"]
    assert storage_mentions("<p>no mentions</p>") == []
    assert storage_mentions("") == []


# --- parse_reference -------------------------------------------------------------------------------------


def page_ref(locator: str, page_id: str = PAGE_ID) -> SourceRef:
    return SourceRef("confluence_page", page_id, locator=locator)


@pytest.mark.parametrize(
    "text",
    [
        f"{WIKI}/spaces/ABC/pages/{PAGE_ID}",
        f"{WIKI}/spaces/ABC/pages/{PAGE_ID}/",
        f"{WIKI}/spaces/ABC/pages/{PAGE_ID}/Supplier+meeting+notes",
        f"{WIKI}/spaces/~712020abc/pages/{PAGE_ID}/Supplier%20notes?focusedCommentId=99#comment-99",
        f"{WIKI}/spaces/ABC/pages/edit-v2/{PAGE_ID}",
        f"{WIKI}/spaces/ABC/pages/edit-v2/{PAGE_ID}?draftShareId=x",
        f"{WIKI}/pages/viewpage.action?pageId={PAGE_ID}",
        f"{WIKI}/pages/viewpage.action?spaceKey=ABC&pageId={PAGE_ID}",
        f"HTTPS://ACME.atlassian.net/wiki/spaces/ABC/pages/{PAGE_ID}",
    ],
)
def test_references_to_one_page(text: str, site: FakeSite) -> None:
    assert make(site).parse_reference(text) == page_ref(text)
    assert site.calls == []


def test_the_locator_is_the_stripped_url(site: FakeSite) -> None:
    assert make(site).parse_reference(f"  {PAGE_URL}\n") == page_ref(PAGE_URL)


def test_a_tiny_link_is_kept_unresolved(site: FakeSite) -> None:
    text = f"{WIKI}/x/AbC-d_1"

    assert make(site).parse_reference(text) == page_ref(text, "tiny:AbC-d_1")
    assert site.calls == []


@pytest.mark.parametrize(
    "text",
    [
        PAGE_ID,
        f"  {PAGE_ID} ",
        "",
        f"https://other.atlassian.net/wiki/spaces/ABC/pages/{PAGE_ID}",
        "https://other.atlassian.net/wiki/x/AbC",
        f"{WIKI}/spaces/ABC/overview",
        f"{WIKI}/spaces/ABC/pages",
        f"{WIKI}/spaces/ABC/pages/notanumber",
        f"{WIKI}/pages/viewpage.action",
        f"{WIKI}/pages/viewpage.action?pageId=abc",
        f"{WIKI}/pages/viewpage.action?spaceKey=ABC",
        f"{WIKI}/x/",
        f"{BASE}/browse/ABC-123",
        f"{BASE}/spaces/ABC/pages/{PAGE_ID}",
        f"{PAGE_URL} and more",
        f"ftp://acme.atlassian.net/wiki/spaces/ABC/pages/{PAGE_ID}",
        "https://example.com/spec",
    ],
)
def test_other_text_is_not_a_confluence_reference(text: str, site: FakeSite) -> None:
    assert make(site).parse_reference(text) is None
    assert site.calls == []


# --- resolve ---------------------------------------------------------------------------------------------


EXPECTED_META = ObjectMeta(
    "confluence_page",
    PAGE_ID,
    title="Supplier meeting notes",
    display_ref="Supplier meeting notes",
    locator=PAGE_URL,
)


def test_resolve_a_page_url(site: FakeSite) -> None:
    meta = make(site).resolve(page_ref(PAGE_URL))

    assert meta == EXPECTED_META
    assert site.calls == [(f"/wiki/api/v2/pages/{PAGE_ID}", {})]


def test_resolve_a_tiny_link_follows_the_redirect(site: FakeSite) -> None:
    meta = make(site).resolve(page_ref(f"{WIKI}/x/AbC", "tiny:AbC"))

    assert meta == EXPECTED_META
    assert site.calls == [("/wiki/x/AbC", {}), (f"/wiki/api/v2/pages/{PAGE_ID}", {})]


def test_resolve_a_tiny_link_that_does_not_end_at_a_page(site: FakeSite) -> None:
    site.tiny["AbC"] = f"{WIKI}/login.action"

    with pytest.raises(ValidationError, match=f"cannot resolve Confluence short link {WIKI}/x/AbC"):
        make(site).resolve(page_ref(f"{WIKI}/x/AbC", "tiny:AbC"))


def test_resolve_an_unknown_tiny_link(site: FakeSite) -> None:
    with pytest.raises(ValidationError, match="cannot resolve Confluence short link"):
        make(site).resolve(page_ref(f"{WIKI}/x/Nope", "tiny:Nope"))


def test_resolve_of_a_missing_page_is_rejected(site: FakeSite) -> None:
    site.missing_page = True

    with pytest.raises(ValidationError, match=f"Confluence page {PAGE_ID} not found or not visible"):
        make(site).resolve(page_ref(PAGE_URL))


def test_resolve_rejects_other_object_types(site: FakeSite) -> None:
    with pytest.raises(ValidationError, match="web_link"):
        make(site).resolve(SourceRef("web_link", "https://example.com"))


# --- fetch -----------------------------------------------------------------------------------------------


def test_fetch_requests_the_page_with_its_storage_body(site: FakeSite) -> None:
    make(site).fetch(state())

    assert site.calls[0] == (f"/wiki/api/v2/pages/{PAGE_ID}", {"body-format": "storage"})


def test_fetch_builds_the_version(site: FakeSite) -> None:
    result = make(site).fetch(state())

    assert result.status == "ok"
    version = result.version
    assert version is not None
    assert version.text == EXPECTED_TEXT
    assert version.metadata == {
        "title": "Supplier meeting notes",
        "space_id": "98765",
        "status": "current",
        "mentions": ["acc-jane"],
    }
    assert version.source_modified_at == "2026-03-05T10:15:30.123Z"
    assert version.source_version_ref == "7"
    assert (result.cursor, result.title) == ("7", "Supplier meeting notes")
    assert result.children == ()


def test_a_page_without_a_body_has_just_its_title_as_text(site: FakeSite) -> None:
    site.page["body"] = {"storage": {"representation": "storage", "value": ""}}

    version = make(site).fetch(state()).version

    assert version is not None
    assert version.text == "Supplier meeting notes"
    assert version.metadata["mentions"] == []


def test_fetch_reads_footer_and_inline_comments_with_their_replies(site: FakeSite) -> None:
    result = make(site).fetch(state())

    assert [(e.external_id, e.text) for e in result.entries] == [
        ("footer:5001", "Hi @acc-alex please confirm torque 12 Nm."),
        ("footer:5002", "Confirmed."),
        ("footer:5003", "Thanks, noted."),
        ("footer:5010", "Ordered."),
        ("inline:6001", "Is 26 weeks right?"),
        ("inline:6002", "Yes, per quote."),
    ]
    assert all(e.entry_type == "confluence_comment" for e in result.entries)
    first, reply, nested = result.entries[:3]
    assert first == EntryData(
        "confluence_comment",
        "footer:5001",
        "2026-03-02T08:00:00.000Z",
        "Hi @acc-alex please confirm torque 12 Nm.",
        author=IdentityRef("atlassian", "acc-jane"),
        metadata={
            "kind": "footer",
            "parent_comment_id": None,
            "comment_version": 1,
            "mentions": ["acc-alex"],
            "resolution_status": None,
            "highlighted_text": None,
        },
    )
    assert reply.author == IdentityRef("atlassian", "acc-alex")
    assert reply.occurred_at == "2026-03-03T10:30:00.000Z"
    assert (reply.metadata["parent_comment_id"], nested.metadata["parent_comment_id"]) == ("5001", "5002")


def test_inline_comments_keep_the_highlighted_text_and_resolution(site: FakeSite) -> None:
    result = make(site).fetch(state())

    inline, inline_reply = (e for e in result.entries if e.external_id.startswith("inline:"))
    assert inline.metadata == {
        "kind": "inline",
        "parent_comment_id": None,
        "comment_version": 1,
        "mentions": [],
        "resolution_status": "open",
        "highlighted_text": "Lead time 26 weeks",
    }
    assert inline_reply.metadata["kind"] == "inline"
    assert inline_reply.metadata["parent_comment_id"] == "6001"
    assert inline_reply.metadata["resolution_status"] is None
    assert inline_reply.metadata["highlighted_text"] is None


def test_an_inline_comment_without_a_selection_property(site: FakeSite) -> None:
    del site.inline[0]["properties"]
    del site.inline[0]["resolutionStatus"]

    inline = next(e for e in make(site).fetch(state()).entries if e.external_id == "inline:6001")

    assert (inline.metadata["highlighted_text"], inline.metadata["resolution_status"]) == (None, None)


def test_fetch_follows_the_cursor_of_every_comment_collection(site: FakeSite) -> None:
    make(site).fetch(state())

    footer_calls = [params for path, params in site.calls if path.endswith("/pages/123456/footer-comments")]
    assert footer_calls == [
        {"body-format": "storage", "limit": 100},
        {"body-format": "storage", "limit": "100", "cursor": "1"},
    ]
    assert "/wiki/api/v2/footer-comments/5001/children" in site.paths()
    assert "/wiki/api/v2/footer-comments/5010/children" in site.paths()
    assert "/wiki/api/v2/inline-comments/6001/children" in site.paths()
    assert all(params["body-format"] == "storage" for path, params in site.calls if "comments" in path)


def test_replies_are_read_down_to_five_levels_only(site: FakeSite) -> None:
    site.footer = [comment("7000", "<p>level 0</p>")]
    site.inline = []
    site.children = {
        ("footer-comments", str(7000 + level)): [comment(str(7001 + level), f"<p>level {level + 1}</p>")]
        for level in range(8)
    }

    result = make(site).fetch(state())

    assert [e.external_id for e in result.entries] == [f"footer:{7000 + level}" for level in range(6)]
    assert "/wiki/api/v2/footer-comments/7004/children" in site.paths()
    assert "/wiki/api/v2/footer-comments/7005/children" not in site.paths()


def test_an_author_without_an_account_id_has_no_identity(site: FakeSite) -> None:
    site.footer = [comment("5001", "<p>Old.</p>", author=None)]

    [first, *_] = make(site).fetch(state()).entries

    assert first.author is None


def test_fetch_of_a_missing_page_is_gone(site: FakeSite) -> None:
    site.missing_page = True

    result = make(site).fetch(state())

    assert result.status == "gone"
    assert result.version is None
    assert result.entries == ()


def test_fetch_always_fetches_even_with_an_unchanged_cursor(site: FakeSite) -> None:
    result = make(site).fetch(state(cursor="7"))

    assert result.version is not None
    assert len(result.entries) == 6


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
            conn, "confluence", "Confluence", settings={"base_url": WIKI, "email": EMAIL}, credential_ref=CRED
        )
        connector = make(site)
        ref = connector.parse_reference(PAGE_URL)
        assert ref is not None
        object_id = store.upsert_object(conn, row.id, connector.resolve(ref))
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)

    def factory(connector_row: registry.ConnectorRow) -> ConfluenceConnector:
        return make(site)

    def sync():
        return run_sync(
            engine, SyncScope("source", object_id), config=load_config(home), connector_factory=factory
        )

    return engine, object_id, sync


def test_the_first_sync_stores_version_and_entries(linked) -> None:
    engine, object_id, sync = linked

    report = sync()

    assert report.status == "ok"
    assert (report.stats["new_versions"], report.stats["new_entries"]) == (1, 6)
    with engine.connect() as conn:
        obj = store.get_object(conn, object_id)
        [version] = store.list_versions(conn, object_id)
        entries = store.list_entries(conn, object_id)
    assert (obj.external_id, obj.display_ref, obj.title, obj.fetch_status) == (
        PAGE_ID,
        "Supplier meeting notes",
        "Supplier meeting notes",
        "ok",
    )
    assert obj.cursor == "7"
    assert version.text == EXPECTED_TEXT
    assert sorted((e.entry_type, e.external_id) for e in entries) == [
        ("confluence_comment", "footer:5001"),
        ("confluence_comment", "footer:5002"),
        ("confluence_comment", "footer:5003"),
        ("confluence_comment", "footer:5010"),
        ("confluence_comment", "inline:6001"),
        ("confluence_comment", "inline:6002"),
    ]


def test_an_unchanged_page_stores_nothing_new(linked) -> None:
    _, _, sync = linked
    sync()

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 0)


def test_a_new_confluence_version_with_an_unchanged_body_is_not_a_new_version(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.page["version"].update(number=8, createdAt="2026-03-09T10:00:00.000Z", authorId="acc-alex")

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 0)
    with engine.connect() as conn:
        assert len(store.list_versions(conn, object_id)) == 1
        assert store.get_object(conn, object_id).cursor == "8"


def test_a_new_page_version_with_a_changed_body_is_a_new_version(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.page["body"]["storage"]["value"] = "<p>Rewritten after the call.</p>"
    site.page["version"].update(number=8, createdAt="2026-03-09T10:00:00.000Z", authorId="acc-alex")

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (1, 0)
    with engine.connect() as conn:
        first, second = store.list_versions(conn, object_id)
    assert first.text == EXPECTED_TEXT
    assert second.text == "Supplier meeting notes\n\nRewritten after the call."
    assert second.source_version_ref == "8"
    assert second.source_modified_at == "2026-03-09T10:00:00.000Z"


def test_a_new_comment_adds_an_entry_but_no_version(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.footer.append(comment("5011", "<p>Late news.</p>", created="2026-03-10T09:00:00.000+01:00"))

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 1)
    with engine.connect() as conn:
        assert len(store.list_entries(conn, object_id)) == 7


def test_an_edited_comment_is_stored_as_a_new_revision(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.footer[1] = comment(
        "5010", "<p>Ordered twice.</p>", created="2026-03-08T09:00:00.000+01:00", number=2
    )

    report = sync()

    assert (report.stats["new_versions"], report.stats["new_entries"]) == (0, 1)
    with engine.connect() as conn:
        revisions = [
            (e.revision, e.text)
            for e in store.list_entries(conn, object_id, all_revisions=True)
            if e.external_id == "footer:5010"
        ]
        latest = [e.text for e in store.list_entries(conn, object_id) if e.external_id == "footer:5010"]
    assert revisions == [(1, "Ordered."), (2, "Ordered twice.")]
    assert latest == ["Ordered twice."]


def test_a_page_that_disappears_becomes_gone_and_keeps_its_versions(linked, site: FakeSite) -> None:
    engine, object_id, sync = linked
    sync()
    site.missing_page = True

    report = sync()

    assert report.status == "ok"
    with engine.connect() as conn:
        assert store.get_object(conn, object_id).fetch_status == "gone"
        assert len(store.list_versions(conn, object_id)) == 1


# --- check -----------------------------------------------------------------------------------------------


def test_check_reports_the_signed_in_user_and_the_spaces(site: FakeSite) -> None:
    report = make(site, {"base_url": WIKI, "email": EMAIL, "spaces": ["ABC", "XYZ"]}).check()

    assert [(c.name, c.ok, c.detail) for c in report.checks] == [
        ("auth", True, "signed in as Alex"),
        ("space ABC", True, "Alpha Bravo Charlie"),
        ("space XYZ", False, "not found or not visible"),
    ]
    assert not report.ok
    assert site.calls == [
        ("/wiki/rest/api/user/current", {}),
        ("/wiki/api/v2/spaces", {"keys": "ABC"}),
        ("/wiki/api/v2/spaces", {"keys": "XYZ"}),
    ]


def test_check_without_spaces_only_tests_the_login(site: FakeSite) -> None:
    report = make(site).check()

    assert [(c.name, c.ok) for c in report.checks] == [("auth", True)]
    assert report.ok
    assert site.paths() == ["/wiki/rest/api/user/current"]


def test_check_stops_at_an_authentication_failure(site: FakeSite) -> None:
    site.status_for_all = 401

    report = make(site, {"base_url": WIKI, "email": EMAIL, "spaces": ["ABC"]}).check()

    [check] = report.checks
    assert (check.name, check.ok) == ("auth", False)
    assert "authentication failed (401)" in check.detail
    assert site.paths() == ["/wiki/rest/api/user/current"]


def test_check_reports_a_missing_token(site: FakeSite) -> None:
    secrets.delete_secret(CRED)

    [check] = make(site).check().checks

    assert (check.name, check.ok) == ("auth", False)
    assert "no API token stored" in check.detail
    assert "s3cr3t-api-token" not in check.detail


# --- search_candidates -----------------------------------------------------------------------------------


def hits() -> list[dict]:
    return [
        {
            "id": "123456",
            "type": "page",
            "title": "Supplier meeting notes",
            "_links": {"webui": "/spaces/ABC/pages/123456/Supplier+meeting+notes"},
        },
        {
            "id": "777",
            "type": "page",
            "title": "Capacitor",
            "_links": {"webui": "/spaces/ABC/pages/777/Capacitor"},
        },
    ]


def test_search_candidates_default_window(site: FakeSite) -> None:
    site.search_hits = hits()

    found = make(site).search_candidates(CandidateQuery("inductor"))

    assert site.calls == [
        (
            "/wiki/rest/api/content/search",
            {
                "cql": 'type = page AND text ~ "inductor" AND lastmodified >= now("-30d")'
                " ORDER BY lastmodified DESC",
                "limit": 20,
            },
        )
    ]
    assert found == [
        EXPECTED_META,
        ObjectMeta(
            "confluence_page",
            "777",
            title="Capacitor",
            display_ref="Capacitor",
            locator=f"{WIKI}/spaces/ABC/pages/777/Capacitor",
        ),
    ]


def test_search_candidates_with_spaces_since_and_limit(site: FakeSite) -> None:
    make(site, {"base_url": WIKI, "email": EMAIL, "spaces": ["ABC", "MB"]}).search_candidates(
        CandidateQuery("lead time", since="2026-02-01T10:00:00.000Z", limit=5)
    )

    [(_, params)] = site.calls
    assert params["cql"] == (
        'type = page AND text ~ "lead time" AND space in ("ABC", "MB") AND lastmodified >= "2026-02-01"'
        " ORDER BY lastmodified DESC"
    )
    assert params["limit"] == 5


def test_search_candidates_escapes_quotes_and_backslashes(site: FakeSite) -> None:
    make(site).search_candidates(CandidateQuery('say "hi" \\ there'))

    [(_, params)] = site.calls
    assert params["cql"] == (
        'type = page AND text ~ "say \\"hi\\" \\\\ there" AND lastmodified >= now("-30d")'
        " ORDER BY lastmodified DESC"
    )


def test_search_candidates_without_hits(site: FakeSite) -> None:
    assert make(site).search_candidates(CandidateQuery("nothing")) == []


# --- registration ----------------------------------------------------------------------------------------


def test_the_connector_is_registered_through_its_entry_point() -> None:
    assert registry.available_connector_types()["confluence"] is ConfluenceConnector
