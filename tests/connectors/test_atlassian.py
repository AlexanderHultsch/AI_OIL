from typing import Any

import pytest
import requests

from ai_oil import secrets
from ai_oil.connectors import atlassian
from ai_oil.connectors.atlassian import (
    AtlassianClient,
    AtlassianError,
    AtlassianNotFound,
    adf_mentions,
    adf_to_text,
    to_utc_iso,
)

BASE = "https://x.atlassian.net"
REF = "jira-token"
TOKEN = "s3cr3t-api-token"


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
        invalid_json: bool = False,
        url: str = "",
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self) -> Any:
        if self._invalid_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(*responses: FakeResponse | Exception, **kwargs: Any) -> tuple[AtlassianClient, FakeSession]:
    session = FakeSession(*responses)
    sleeps: list[float] = kwargs.setdefault("sleeps", [])
    client = AtlassianClient(
        kwargs.pop("base_url", BASE),
        "me@example.com",
        kwargs.pop("credential_ref", REF),
        session=session,  # type: ignore[arg-type]
        sleep=sleeps.append,
        **{key: value for key, value in kwargs.items() if key != "sleeps"},
    )
    return client, session


@pytest.fixture(autouse=True)
def stored_token() -> None:
    secrets.set_secret(REF, TOKEN)


def error_of(client: AtlassianClient, path: str = "/rest/x") -> AtlassianError:
    with pytest.raises(AtlassianError) as excinfo:
        client.get_json(path)
    return excinfo.value


# --- token handling -------------------------------------------------------------------------------


def test_constructor_does_not_read_the_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(ref: str) -> str:
        raise AssertionError("keyring read in the constructor")

    monkeypatch.setattr("ai_oil.connectors.atlassian.get_secret", fail)
    make_client()


def test_token_is_read_on_first_request_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []

    def fake_get_secret(ref: str) -> str:
        reads.append(ref)
        return TOKEN

    monkeypatch.setattr("ai_oil.connectors.atlassian.get_secret", fake_get_secret)
    client, _ = make_client(FakeResponse(payload={}), FakeResponse(payload={}))
    assert reads == []
    client.get_json("/a")
    client.get_json("/b")
    assert reads == [REF]


def test_missing_secret_is_reported_with_governance_hint() -> None:
    secrets.delete_secret(REF)
    client, session = make_client()
    message = str(error_of(client))
    assert "GOV-8" in message
    assert REF in message
    assert session.calls == []


def test_missing_credential_ref_is_reported() -> None:
    client, _ = make_client(credential_ref=None)
    message = str(error_of(client))
    assert "this connector" in message
    assert "GOV-8" in message


def test_token_never_appears_in_errors_or_repr() -> None:
    echo = f"bad request for {TOKEN} and more"
    cases: list[tuple[FakeResponse | Exception, int]] = [
        (FakeResponse(404), 1),
        (FakeResponse(401), 1),
        (FakeResponse(403), 1),
        (FakeResponse(500, text=echo), 1),
        (FakeResponse(429), 4),
        (FakeResponse(200, invalid_json=True), 1),
        (requests.ConnectionError(f"proxy rejected {TOKEN}"), 1),
    ]
    for response, count in cases:
        client, _ = make_client(*([response] * count))
        assert TOKEN not in str(error_of(client))
        assert TOKEN not in repr(client)
        assert TOKEN not in repr(error_of(make_client(*([response] * count))[0]))


# --- request shape ---------------------------------------------------------------------------------


def test_request_carries_auth_headers_timeout_and_default_verify() -> None:
    client, session = make_client(FakeResponse(payload={"ok": True}), timeout=12.5)
    assert client.get_json("/rest/api/3/myself", {"expand": "x"}) == {"ok": True}
    (call,) = session.calls
    assert call["url"] == f"{BASE}/rest/api/3/myself"
    assert call["params"] == {"expand": "x"}
    assert call["auth"] == ("me@example.com", TOKEN)
    assert call["headers"] == {"Accept": "application/json"}
    assert call["timeout"] == 12.5
    assert call["verify"] is True


def test_ca_bundle_is_passed_as_verify() -> None:
    client, session = make_client(FakeResponse(payload={}), ca_bundle="C:/certs/corp.pem")
    client.get_json("/x")
    assert session.calls[0]["verify"] == "C:/certs/corp.pem"


class SessionProbe:
    """Stands in for both session factories and records which one built the client's session."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.built: list[str] = []
        self.plain = FakeSession(FakeResponse(payload={}))
        self.system = FakeSession(FakeResponse(payload={}))
        monkeypatch.setattr(atlassian.requests, "Session", self._plain_session)
        monkeypatch.setattr(atlassian, "system_certificate_session", self._system_session)

    def _plain_session(self) -> FakeSession:
        self.built.append("plain")
        return self.plain

    def _system_session(self) -> FakeSession:
        self.built.append("system")
        return self.system


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> SessionProbe:
    return SessionProbe(monkeypatch)


def test_the_system_certificate_store_is_the_default(probe: SessionProbe) -> None:
    client = AtlassianClient(BASE, "me@example.com", REF)
    client.get_json("/x")

    assert probe.built == ["system"]
    assert probe.system.calls[0]["verify"] is True
    assert probe.plain.calls == []


def test_an_explicit_ca_bundle_overrides_the_system_certificate_store(probe: SessionProbe) -> None:
    client = AtlassianClient(BASE, "me@example.com", REF, ca_bundle="C:/certs/corp.pem")
    client.get_json("/x")

    assert probe.built == ["plain"]
    assert probe.plain.calls[0]["verify"] == "C:/certs/corp.pem"


def test_an_explicit_ca_bundle_wins_even_when_system_certificates_are_on(probe: SessionProbe) -> None:
    AtlassianClient(BASE, "me@example.com", REF, ca_bundle="C:/certs/corp.pem", use_system_certificates=True)

    assert probe.built == ["plain"]


def test_switching_system_certificates_off_uses_a_plain_session(probe: SessionProbe) -> None:
    client = AtlassianClient(BASE, "me@example.com", REF, use_system_certificates=False)
    client.get_json("/x")

    assert probe.built == ["plain"]
    assert probe.plain.calls[0]["verify"] is True


def test_an_injected_session_is_used_as_given(probe: SessionProbe) -> None:
    client, session = make_client(FakeResponse(payload={}), use_system_certificates=True)
    client.get_json("/x")

    assert probe.built == []
    assert session.calls[0]["verify"] is True


def test_trailing_slash_of_base_url_is_stripped() -> None:
    client, session = make_client(FakeResponse(payload={}), base_url=BASE + "/wiki/")
    client.get_json("/api/v2/pages")
    assert session.calls[0]["url"] == f"{BASE}/wiki/api/v2/pages"


# --- status handling -------------------------------------------------------------------------------


def test_404_is_not_found() -> None:
    client, _ = make_client(FakeResponse(404))
    with pytest.raises(AtlassianNotFound, match="not found: /rest/x"):
        client.get_json("/rest/x")


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_message(status: int) -> None:
    client, _ = make_client(FakeResponse(status))
    message = str(error_of(client))
    assert f"authentication failed ({status})" in message
    assert "e-mail address and API token" in message


def test_other_status_includes_truncated_body() -> None:
    client, _ = make_client(FakeResponse(500, text="e" * 500))
    message = str(error_of(client))
    assert message == "HTTP 500 for /rest/x: " + "e" * 200


def test_request_exception_is_reported_as_unreachable() -> None:
    client, _ = make_client(requests.ConnectionError("dns failure"))
    message = str(error_of(client))
    assert message.startswith(f"cannot reach {BASE}:")
    assert "dns failure" in message


def test_invalid_json() -> None:
    client, _ = make_client(FakeResponse(200, invalid_json=True))
    assert str(error_of(client)) == "invalid JSON from /rest/x"


def test_not_found_is_an_atlassian_error() -> None:
    assert issubclass(AtlassianNotFound, AtlassianError)


# --- rate limiting ---------------------------------------------------------------------------------


def test_429_waits_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    client, session = make_client(
        FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(payload={"ok": 1}), sleeps=sleeps
    )
    assert client.get_json("/x") == {"ok": 1}
    assert sleeps == [7]
    assert len(session.calls) == 2


def test_exhausted_retries_report_rate_limit() -> None:
    sleeps: list[float] = []
    client, session = make_client(*[FakeResponse(429, headers={"Retry-After": "1"})] * 4, sleeps=sleeps)
    assert str(error_of(client)) == "rate limited by Atlassian (429)"
    assert sleeps == [1, 1, 1]
    assert len(session.calls) == 4


def test_503_without_header_backs_off_exponentially() -> None:
    sleeps: list[float] = []
    client, _ = make_client(*[FakeResponse(503)] * 3, FakeResponse(payload={}), sleeps=sleeps)
    assert client.get_json("/x") == {}
    assert sleeps == [1, 2, 4]


def test_non_numeric_retry_after_falls_back_to_backoff() -> None:
    sleeps: list[float] = []
    client, _ = make_client(
        FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(payload={}),
        sleeps=sleeps,
    )
    client.get_json("/x")
    assert sleeps == [1]


def test_max_retries_zero_does_not_retry() -> None:
    sleeps: list[float] = []
    client, session = make_client(FakeResponse(429), max_retries=0, sleeps=sleeps)
    assert "rate limited" in str(error_of(client))
    assert sleeps == []
    assert len(session.calls) == 1


# --- paging ----------------------------------------------------------------------------------------


def test_get_paged_with_total_over_three_pages() -> None:
    client, session = make_client(
        FakeResponse(payload={"values": [1, 2], "total": 5}),
        FakeResponse(payload={"values": [3, 4], "total": 5}),
        FakeResponse(payload={"values": [5], "total": 5}),
    )
    assert client.get_paged("/rest/p", {"jql": "x"}, values_key="values", page_size=2) == [1, 2, 3, 4, 5]
    assert [call["params"] for call in session.calls] == [
        {"jql": "x", "startAt": 0, "maxResults": 2},
        {"jql": "x", "startAt": 2, "maxResults": 2},
        {"jql": "x", "startAt": 4, "maxResults": 2},
    ]


def test_get_paged_continues_when_server_caps_page_size_below_total() -> None:
    client, session = make_client(
        FakeResponse(payload={"values": [1, 2], "total": 4}),
        FakeResponse(payload={"values": [3, 4], "total": 4}),
    )
    assert client.get_paged("/rest/p", None, values_key="values", page_size=100) == [1, 2, 3, 4]
    assert len(session.calls) == 2


def test_get_paged_stops_on_is_last() -> None:
    client, session = make_client(
        FakeResponse(payload={"values": [1, 2], "isLast": False}),
        FakeResponse(payload={"values": [3, 4], "isLast": True}),
    )
    assert client.get_paged("/rest/p", None, values_key="values", page_size=2) == [1, 2, 3, 4]
    assert len(session.calls) == 2


def test_get_paged_stops_on_short_page_without_total() -> None:
    client, session = make_client(
        FakeResponse(payload={"values": [1, 2]}),
        FakeResponse(payload={"values": [3]}),
    )
    assert client.get_paged("/rest/p", None, values_key="values", page_size=2) == [1, 2, 3]
    assert len(session.calls) == 2


def test_get_paged_stops_on_empty_page() -> None:
    client, session = make_client(FakeResponse(payload={"values": []}))
    assert client.get_paged("/rest/p", None, values_key="values", page_size=2) == []
    assert len(session.calls) == 1


def test_get_paged_rejects_unexpected_payload() -> None:
    client, _ = make_client(FakeResponse(payload={"other": []}))
    with pytest.raises(AtlassianError, match="no 'values' list"):
        client.get_paged("/rest/p", None, values_key="values")


def test_get_cursor_paged_follows_next_link_against_site_root() -> None:
    client, session = make_client(
        FakeResponse(payload={"results": [1, 2], "_links": {"next": "/wiki/api/v2/pages?cursor=abc"}}),
        FakeResponse(payload={"results": [3], "_links": {}}),
        base_url=BASE + "/wiki",
    )
    assert client.get_cursor_paged("/api/v2/pages", {"status": "current"}, page_size=2) == [1, 2, 3]
    first, second = session.calls
    assert first["url"] == f"{BASE}/wiki/api/v2/pages"
    assert first["params"] == {"status": "current", "limit": 2}
    assert second["url"] == f"{BASE}/wiki/api/v2/pages?cursor=abc"
    assert second["params"] is None


def test_get_cursor_paged_stops_without_next() -> None:
    client, session = make_client(FakeResponse(payload={"results": [1]}), base_url=BASE + "/wiki")
    assert client.get_cursor_paged("/api/v2/pages", None) == [1]
    assert len(session.calls) == 1
    assert session.calls[0]["params"] == {"limit": 100}


def test_final_url_follows_redirects_on_the_site_root_and_returns_the_last_url() -> None:
    final = f"{BASE}/wiki/spaces/ABC/pages/123/Title"
    client, session = make_client(FakeResponse(text="<html>", url=final), base_url=BASE + "/wiki")
    assert client.final_url("/wiki/x/AbC") == final
    (call,) = session.calls
    assert call["url"] == f"{BASE}/wiki/x/AbC"
    assert call["params"] is None
    assert call["allow_redirects"] is True
    assert call["auth"] == ("me@example.com", TOKEN)
    assert call["headers"] == {"Accept": "application/json"}
    assert call["timeout"] == 30.0
    assert call["verify"] is True


def test_final_url_does_not_need_a_json_body_and_passes_the_ca_bundle() -> None:
    client, session = make_client(
        FakeResponse(invalid_json=True, url=f"{BASE}/wiki/p"), ca_bundle="C:/certs/corp.pem"
    )
    assert client.final_url("/wiki/x/AbC") == f"{BASE}/wiki/p"
    assert session.calls[0]["verify"] == "C:/certs/corp.pem"


def test_final_url_maps_statuses_like_get_json() -> None:
    client, _ = make_client(FakeResponse(404))
    with pytest.raises(AtlassianNotFound):
        client.final_url("/wiki/x/AbC")
    client, _ = make_client(FakeResponse(401))
    with pytest.raises(AtlassianError, match="authentication failed"):
        client.final_url("/wiki/x/AbC")
    client, session = make_client(FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(url="u"))
    assert client.final_url("/wiki/x/AbC") == "u"
    assert len(session.calls) == 2


# --- timestamps ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-24T16:02:11.123+0200", "2026-10-24T14:02:11.123Z"),
        ("2026-10-24T14:02:11.123Z", "2026-10-24T14:02:11.123Z"),
        ("2026-10-24T14:02:11Z", "2026-10-24T14:02:11.000Z"),
        ("2026-10-24T16:02:11.123+02:00", "2026-10-24T14:02:11.123Z"),
        ("2026-10-24T23:30:00.500-0500", "2026-10-25T04:30:00.500Z"),
    ],
)
def test_to_utc_iso(value: str, expected: str) -> None:
    assert to_utc_iso(value) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_to_utc_iso_empty(value: str | None) -> None:
    assert to_utc_iso(value) is None


@pytest.mark.parametrize("value", ["garbage", "2026-10-24T14:02:11"])
def test_to_utc_iso_rejects_unparseable_or_offsetless(value: str) -> None:
    with pytest.raises(ValueError):
        to_utc_iso(value)


# --- ADF -------------------------------------------------------------------------------------------


def text(value: str, *marks: str) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def paragraph(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def item(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "listItem", "content": list(content)}


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def test_realistic_document() -> None:
    document = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("Status")]},
        paragraph(
            text("Hello ", "strong"),
            {"type": "mention", "attrs": {"id": "acc-1", "text": "@Alex"}},
            text(", please check"),
            {"type": "hardBreak"},
            text("the second line"),
        ),
        {
            "type": "bulletList",
            "content": [
                item(
                    paragraph(text("first")),
                    {"type": "bulletList", "content": [item(paragraph(text("nested")))]},
                ),
                item(paragraph(text("second"))),
            ],
        },
        {
            "type": "orderedList",
            "attrs": {"order": 3},
            "content": [item(paragraph(text("three"))), item(paragraph(text("four")))],
        },
        {"type": "codeBlock", "content": [text("def f():\n    return 1")]},
        {"type": "blockquote", "content": [paragraph(text("quoted")), paragraph(text("again"))]},
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableHeader", "content": [paragraph(text("Item"))]},
                        {"type": "tableHeader", "content": [paragraph(text("Due"))]},
                    ],
                },
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableCell",
                            "content": [paragraph(text("PPAP")), paragraph(text("sample"))],
                        },
                        {
                            "type": "tableCell",
                            "content": [paragraph({"type": "date", "attrs": {"timestamp": "1792000000000"}})],
                        },
                    ],
                },
            ],
        },
        {"type": "rule"},
        paragraph(
            {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}},
            text(" "),
            {"type": "emoji", "attrs": {"shortName": ":x:"}},
            text(" "),
            {"type": "inlineCard", "attrs": {"url": "https://x.atlassian.net/browse/ABC-1"}},
            text(" "),
            {"type": "status", "attrs": {"text": "IN PROGRESS", "color": "blue"}},
        ),
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m1", "type": "file"}}]},
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [paragraph(text("panel text"))]},
        {"type": "somethingNew", "content": [paragraph(text("unknown but readable"))]},
    )
    expected = "\n".join(
        [
            "## Status",
            "",
            "Hello @Alex, please check",
            "the second line",
            "",
            "- first",
            "  - nested",
            "- second",
            "",
            "3. three",
            "4. four",
            "",
            "def f():",
            "    return 1",
            "",
            "> quoted",
            ">",
            "> again",
            "",
            "Item\tDue",
            "PPAP sample\t2026-10-14",
            "",
            "---",
            "",
            "😄 :x: https://x.atlassian.net/browse/ABC-1 [IN PROGRESS]",
            "",
            "[attachment]",
            "",
            "panel text",
            "",
            "unknown but readable",
        ]
    )
    assert adf_to_text(document) == expected


def test_mention_without_text_uses_id() -> None:
    assert adf_to_text(doc(paragraph({"type": "mention", "attrs": {"id": "acc-9"}}))) == "@acc-9"


def test_date_accepts_integer_milliseconds() -> None:
    assert (
        adf_to_text(doc(paragraph({"type": "date", "attrs": {"timestamp": 1792000000000}}))) == "2026-10-14"
    )


def test_media_group_and_cards_and_layout() -> None:
    document = doc(
        {"type": "mediaGroup", "content": [{"type": "media", "attrs": {"id": "a"}}]},
        {"type": "blockCard", "attrs": {"url": "https://example.com/doc"}},
        {
            "type": "layoutSection",
            "content": [
                {"type": "layoutColumn", "content": [paragraph(text("left"))]},
                {"type": "layoutColumn", "content": [paragraph(text("right"))]},
            ],
        },
        {"type": "expand", "attrs": {"title": "More"}, "content": [paragraph(text("hidden"))]},
    )
    assert adf_to_text(document) == "[attachment]\n\nhttps://example.com/doc\n\nleft\n\nright\n\nhidden"


def test_blank_runs_and_trailing_whitespace_are_normalised() -> None:
    document = doc(paragraph(text("a   ")), paragraph(), paragraph(), paragraph(text("b")))
    assert adf_to_text(document) == "a\n\nb"


def test_string_none_and_empty_input() -> None:
    assert adf_to_text("  plain v2 body \n") == "plain v2 body"
    assert adf_to_text(None) == ""
    assert adf_to_text({}) == ""
    assert adf_to_text(doc()) == ""


def test_adf_mentions_in_document_order_without_duplicates() -> None:
    document = doc(
        paragraph(
            {"type": "mention", "attrs": {"id": "b"}},
            text(" and "),
            {"type": "mention", "attrs": {"id": "a"}},
        ),
        {
            "type": "bulletList",
            "content": [
                item(paragraph({"type": "mention", "attrs": {"id": "b"}})),
                item(paragraph({"type": "mention", "attrs": {"id": "c", "text": "@C"}})),
            ],
        },
        paragraph({"type": "mention", "attrs": {}}),
    )
    assert adf_mentions(document) == ["b", "a", "c"]
    assert adf_mentions(None) == []
    assert adf_mentions(doc()) == []
