"""Confluence Cloud connector: pages and their comments (CON-40 .. CON-43)."""

import re
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, NavigableString

from ai_oil.connectors.atlassian import (
    AtlassianClient,
    AtlassianError,
    AtlassianNotFound,
    to_utc_iso,
)
from ai_oil.connectors.email_text import html_to_text
from ai_oil.errors import ValidationError
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    IdentityRef,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)

_PAGE_PATH = re.compile(r"^/wiki/spaces/[^/]+/pages/(\d+)(?:/[^/\s]*)?/?$")
_EDIT_PATH = re.compile(r"^/wiki/spaces/[^/]+/pages/edit-v2/(\d+)/?$")
_TINY_PATH = re.compile(r"^/wiki/x/([A-Za-z0-9_-]+)/?$")
_VIEW_PATH = "/wiki/pages/viewpage.action"
_PAGE_ID = re.compile(r"^\d+$")
_TINY_PREFIX = "tiny:"
_STORAGE = {"body-format": "storage"}
_MAX_REPLY_DEPTH = 5
_COMMENT_COLLECTIONS = (("footer", "footer-comments"), ("inline", "inline-comments"))
_LINK_BODIES = ("ac:link-body", "ac:plain-text-link-body")


def _text_setting(settings: Mapping[str, object], key: str, *, required: bool) -> str | None:
    value = settings.get(key)
    if value is None:
        if required:
            raise ValidationError(f"Confluence setting {key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Confluence setting {key} must be non-blank text")
    return value.strip()


def _spaces_setting(settings: Mapping[str, object]) -> list[str]:
    value = settings.get("spaces", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError("Confluence setting spaces must be a list of space keys")
    return [item.strip() for item in value]


def _timeout_setting(settings: Mapping[str, object]) -> float:
    value = settings.get("timeout", 30)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValidationError("Confluence setting timeout must be a positive number of seconds")
    return float(value)


def _base_url_setting(settings: Mapping[str, object]) -> str:
    value = _text_setting(settings, "base_url", required=True)
    assert value is not None
    parts = urlsplit(value)
    example = "https://acme.atlassian.net/wiki"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError(f"Confluence setting base_url must be a URL such as {example}")
    base = value.rstrip("/")
    if not base.endswith("/wiki"):
        raise ValidationError(f"Confluence setting base_url must be the wiki root, such as {example}")
    return base


def _cql_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def storage_to_text(xhtml: str) -> str:
    """Readable plain text of a Confluence storage-format body (CON-42)."""
    soup = BeautifulSoup(xhtml, "html.parser")
    for parameter in soup.find_all("ac:parameter"):
        parameter.decompose()
    for macro in reversed(soup.find_all("ac:structured-macro")):
        if macro.get("ac:name") == "code":
            body = macro.find("ac:plain-text-body")
            pre = soup.new_tag("pre")
            pre.append(NavigableString(body.get_text() if body is not None else ""))
            macro.replace_with(pre)
            continue
        body = macro.find("ac:rich-text-body")
        if body is None:
            macro.decompose()
            continue
        macro.replace_with(body)
        body.unwrap()
    for image in soup.find_all("ac:image"):
        image.replace_with(NavigableString("[image]"))
    for emoticon in soup.find_all("ac:emoticon"):
        emoticon.replace_with(NavigableString(f":{emoticon.get('ac:name') or ''}:"))
    for link in soup.find_all("ac:link"):
        replacement = _link_text(link)
        if replacement is not None:
            link.replace_with(NavigableString(replacement))
    for task in soup.find_all("ac:task"):
        status = task.find("ac:task-status")
        body = task.find("ac:task-body")
        done = status is not None and status.get_text().strip() == "complete"
        line = soup.new_tag("div")
        line.append(NavigableString(("[x] " if done else "[ ] ") + (body.get_text() if body else "")))
        task.replace_with(line)
    return html_to_text(str(soup))


def _link_text(link: Any) -> str | None:
    user = link.find("ri:user")
    if user is not None:
        return "@" + str(user.get("ri:account-id") or "")
    page = link.find("ri:page")
    if page is None:
        return None
    for name in _LINK_BODIES:
        body = link.find(name)
        if body is not None and body.get_text().strip():
            return body.get_text().strip()
    return str(page.get("ri:content-title") or "")


def storage_mentions(xhtml: str) -> list[str]:
    """accountIds of all `ri:user` elements in document order, de-duplicated (CON-43)."""
    found: dict[str, None] = {}
    for user in BeautifulSoup(xhtml, "html.parser").find_all("ri:user"):
        account_id = user.get("ri:account-id")
        if account_id:
            found.setdefault(str(account_id), None)
    return list(found)


def _storage_value(node: Mapping[str, Any]) -> str:
    body = node.get("body")
    storage = body.get("storage") if isinstance(body, Mapping) else None
    value = storage.get("value") if isinstance(storage, Mapping) else None
    return str(value) if value else ""


def _version_of(node: Mapping[str, Any]) -> Mapping[str, Any]:
    version = node.get("version")
    return version if isinstance(version, Mapping) else {}


class ConfluenceConnector:
    """Pages of one Confluence Cloud site, read through an injectable REST client (CON-40)."""

    type: ClassVar[str] = "confluence"
    object_types: ClassVar[frozenset[str]] = frozenset({"confluence_page"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 4

    def __init__(self, config: ConnectorConfig, *, client: AtlassianClient | None = None) -> None:
        self.config = config
        settings = config.settings
        self._base_url = _base_url_setting(settings)
        email = _text_setting(settings, "email", required=True)
        assert email is not None
        self._spaces = _spaces_setting(settings)
        ca_bundle = _text_setting(settings, "ca_bundle", required=False)
        timeout = _timeout_setting(settings)
        parts = urlsplit(self._base_url)
        self._host = (parts.hostname or "").lower()
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._client: AtlassianClient = (
            client
            if client is not None
            else AtlassianClient(
                self._base_url, email, config.credential_ref, ca_bundle=ca_bundle, timeout=timeout
            )
        )

    def _locator(self, node: Mapping[str, Any]) -> str | None:
        links = node.get("_links")
        webui = links.get("webui") if isinstance(links, Mapping) else None
        return f"{self._origin}/wiki{webui}" if webui else None

    def check(self) -> HealthReport:
        try:
            me = self._client.get_json("/rest/api/user/current")
        except AtlassianError as exc:
            return HealthReport((HealthCheck("auth", False, str(exc)),))
        name = me.get("displayName") if isinstance(me, Mapping) else None
        checks = [HealthCheck("auth", True, f"signed in as {name or '-'}")]
        for key in self._spaces:
            label = f"space {key}"
            try:
                found = self._client.get_json("/api/v2/spaces", {"keys": key})
            except AtlassianError as exc:
                checks.append(HealthCheck(label, False, str(exc)))
                continue
            results = found.get("results") if isinstance(found, Mapping) else None
            if results:
                checks.append(HealthCheck(label, True, str(results[0].get("name") or key)))
            else:
                checks.append(HealthCheck(label, False, "not found or not visible"))
        return HealthReport(tuple(checks))

    def parse_reference(self, text: str) -> SourceRef | None:
        candidate = text.strip()
        found = self._classify(candidate)
        if found is None:
            return None
        kind, value = found
        external_id = value if kind == "page" else _TINY_PREFIX + value
        return SourceRef("confluence_page", external_id, locator=candidate)

    def _classify(self, text: str) -> tuple[str, str] | None:
        try:
            parts = urlsplit(text)
            hostname = parts.hostname
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or (hostname or "").lower() != self._host:
            return None
        for pattern in (_PAGE_PATH, _EDIT_PATH):
            match = pattern.match(parts.path)
            if match is not None:
                return "page", match.group(1)
        if parts.path == _VIEW_PATH:
            for page_id in parse_qs(parts.query).get("pageId", []):
                if _PAGE_ID.match(page_id):
                    return "page", page_id
            return None
        match = _TINY_PATH.match(parts.path)
        if match is not None:
            return "tiny", match.group(1)
        return None

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        if ref.object_type != "confluence_page":
            raise ValidationError(f"the Confluence connector cannot resolve a {ref.object_type} reference")
        page_id = ref.external_id
        if page_id.startswith(_TINY_PREFIX):
            page_id = self._resolve_short_link(page_id.removeprefix(_TINY_PREFIX), ref.locator)
        try:
            page = self._client.get_json(f"/api/v2/pages/{page_id}")
        except AtlassianNotFound:
            raise ValidationError(f"Confluence page {page_id} not found or not visible") from None
        title = page.get("title")
        return ObjectMeta(
            "confluence_page",
            str(page.get("id") or page_id),
            title=title,
            display_ref=title,
            locator=self._locator(page),
        )

    def _resolve_short_link(self, code: str, locator: str | None) -> str:
        shown = locator or f"{self._origin}/wiki/x/{code}"
        try:
            final = self._client.final_url(f"/wiki/x/{code}")
        except AtlassianNotFound:
            raise ValidationError(f"cannot resolve Confluence short link {shown}") from None
        found = self._classify(final)
        if found is None or found[0] != "page":
            raise ValidationError(f"cannot resolve Confluence short link {shown}")
        return found[1]

    def fetch(self, state: ObjectState) -> FetchResult:
        page_id = state.external_id
        try:
            page = self._client.get_json(f"/api/v2/pages/{page_id}", _STORAGE)
        except AtlassianNotFound:
            return FetchResult(status="gone")
        version = _version_of(page)
        number = version.get("number")
        return FetchResult(
            version=self._version(page, version),
            entries=tuple(self._comments(page_id)),
            cursor=str(number) if number is not None else None,
            title=page.get("title"),
        )

    @staticmethod
    def _version(page: Mapping[str, Any], version: Mapping[str, Any]) -> VersionData:
        title = str(page.get("title") or "")
        xhtml = _storage_value(page)
        body = storage_to_text(xhtml)
        number = version.get("number")
        return VersionData(
            text=f"{title}\n\n{body}" if body else title,
            metadata={
                "title": page.get("title"),
                "space_id": page.get("spaceId"),
                "status": page.get("status"),
                "mentions": storage_mentions(xhtml),
            },
            source_modified_at=to_utc_iso(version.get("createdAt")),
            source_version_ref=str(number) if number is not None else None,
        )

    def _comments(self, page_id: str) -> list[EntryData]:
        entries: list[EntryData] = []
        for kind, collection in _COMMENT_COLLECTIONS:
            comments = self._client.get_cursor_paged(f"/api/v2/pages/{page_id}/{collection}", _STORAGE)
            for comment in comments:
                entries.append(self._entry(kind, comment, None))
                entries.extend(self._replies(kind, collection, comment, 1))
        return entries

    def _replies(self, kind: str, collection: str, parent: Mapping[str, Any], depth: int) -> list[EntryData]:
        if depth > _MAX_REPLY_DEPTH:
            return []
        parent_id = str(parent["id"])
        replies = self._client.get_cursor_paged(f"/api/v2/{collection}/{parent_id}/children", _STORAGE)
        entries: list[EntryData] = []
        for reply in replies:
            entries.append(self._entry(kind, reply, parent_id))
            entries.extend(self._replies(kind, collection, reply, depth + 1))
        return entries

    @staticmethod
    def _entry(kind: str, comment: Mapping[str, Any], parent_id: str | None) -> EntryData:
        comment_id = str(comment["id"])
        version = _version_of(comment)
        xhtml = _storage_value(comment)
        author_id = version.get("authorId")
        inline = kind == "inline"
        properties = comment.get("properties")
        return EntryData(
            "confluence_comment",
            f"{kind}:{comment_id}",
            to_utc_iso(version.get("createdAt")) or "",
            storage_to_text(xhtml),
            author=IdentityRef("atlassian", str(author_id)) if author_id else None,
            metadata={
                "kind": kind,
                "parent_comment_id": parent_id,
                "comment_version": version.get("number"),
                "mentions": storage_mentions(xhtml),
                "resolution_status": comment.get("resolutionStatus") if inline else None,
                "highlighted_text": (
                    properties.get("inline-original-selection")
                    if inline and isinstance(properties, Mapping)
                    else None
                ),
            },
        )

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        cql = f'type = page AND text ~ "{_cql_text(query.text)}"'
        if self._spaces:
            cql += " AND space in (" + ", ".join(f'"{_cql_text(key)}"' for key in self._spaces) + ")"
        cql += (
            f' AND lastmodified >= "{query.since[:10]}"'
            if query.since
            else ' AND lastmodified >= now("-30d")'
        )
        cql += " ORDER BY lastmodified DESC"
        found = self._client.get_json("/rest/api/content/search", {"cql": cql, "limit": query.limit})
        return [
            ObjectMeta(
                "confluence_page",
                str(page["id"]),
                title=page.get("title"),
                display_ref=page.get("title"),
                locator=self._locator(page),
            )
            for page in found.get("results") or []
        ]
