"""Atlassian Cloud access shared by the Jira and Confluence connectors: HTTP client and ADF to text (CON-5, CON-34)."""  # noqa: E501

import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from ai_oil.errors import AiOilError
from ai_oil.secrets import get_secret

_RETRY_STATUSES = (429, 503)
_BODY_EXCERPT = 200
_MILLIS_PER_SECOND = 1000


class AtlassianError(AiOilError):
    """Atlassian Cloud is not reachable, rejected the credentials, or answered unexpectedly."""


class AtlassianNotFound(AtlassianError):
    """The requested resource does not exist, or is not visible to this account."""


class AtlassianClient:
    """Read-only JSON access to one Atlassian Cloud site (CON-5, CON-6).

    The API token is looked up on the first request, kept in memory only and never put into a message (GOV-8).
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        credential_ref: str | None,
        *,
        ca_bundle: str | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._credential_ref = credential_ref
        self._ca_bundle = ca_bundle
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._max_retries = max_retries
        self._token: str | None = None

    def __repr__(self) -> str:
        return f"AtlassianClient({self._base_url!r})"

    def get_json(self, path: str, params: Mapping[str, object] | None = None) -> Any:
        """GET `path` below the base URL and return the decoded JSON body."""
        return self._get(self._base_url + path, path, params)

    def final_url(self, path: str) -> str:
        """The URL a GET of `path` on the site root ends up at after following redirects (CON-41)."""
        return self._send(self._origin() + path, path, None).url

    def get_paged(
        self,
        path: str,
        params: Mapping[str, object] | None,
        *,
        values_key: str,
        page_size: int = 100,
    ) -> list[Any]:
        """All items under `values_key` of a Jira-style offset-paged endpoint."""
        collected: list[Any] = []
        start_at = 0
        while True:
            page = self.get_json(path, {**(params or {}), "startAt": start_at, "maxResults": page_size})
            items = page.get(values_key) if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise AtlassianError(f"unexpected response from {path}: no '{values_key}' list")
            collected.extend(items)
            start_at += len(items)
            total = page.get("total")
            if not items or page.get("isLast") is True:
                return collected
            if isinstance(total, int):
                if start_at >= total:
                    return collected
            elif len(items) < page_size:
                return collected

    def get_cursor_paged(
        self,
        path: str,
        params: Mapping[str, object] | None,
        *,
        page_size: int = 100,
    ) -> list[Any]:
        """All `results` of a Confluence v2-style cursor-paged endpoint, following `_links.next`."""
        collected: list[Any] = []
        origin = self._origin()
        url, label = self._base_url + path, path
        query: Mapping[str, object] | None = {**(params or {}), "limit": page_size}
        while True:
            page = self._get(url, label, query)
            results = page.get("results") if isinstance(page, dict) else None
            if not isinstance(results, list):
                raise AtlassianError(f"unexpected response from {label}: no 'results' list")
            collected.extend(results)
            links = page.get("_links")
            next_link = links.get("next") if isinstance(links, dict) else None
            if not next_link:
                return collected
            label = str(next_link)
            url = origin + (label if label.startswith("/") else "/" + label)
            query = None

    def _origin(self) -> str:
        parts = urlsplit(self._base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def _api_token(self) -> str:
        token = get_secret(self._credential_ref) if self._credential_ref else None
        if not token:
            raise AtlassianError(
                f"no API token stored for {self._credential_ref or 'this connector'}"
                " — store it with `oil connector add` (GOV-8)"
            )
        return token

    def _get(self, url: str, label: str, params: Mapping[str, object] | None) -> Any:
        response = self._send(url, label, params)
        try:
            return response.json()
        except ValueError:
            raise AtlassianError(f"invalid JSON from {label}") from None

    def _send(self, url: str, label: str, params: Mapping[str, object] | None) -> requests.Response:
        if self._token is None:
            self._token = self._api_token()
        token = self._token
        attempt = 0
        while True:
            try:
                response = self._session.get(
                    url,
                    params=params,
                    auth=(self._email, token),
                    headers={"Accept": "application/json"},
                    timeout=self._timeout,
                    verify=self._ca_bundle if self._ca_bundle else True,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise AtlassianError(
                    f"cannot reach {self._base_url}: {str(exc).replace(token, '***')}"
                ) from None
            status = response.status_code
            if status in _RETRY_STATUSES:
                if attempt >= self._max_retries:
                    raise AtlassianError(f"rate limited by Atlassian ({status})")
                self._sleep(_retry_delay(response.headers.get("Retry-After"), attempt))
                attempt += 1
                continue
            if status == 200:
                return response
            if status == 404:
                raise AtlassianNotFound(f"not found: {label}")
            if status in (401, 403):
                raise AtlassianError(
                    f"authentication failed ({status}) — check the e-mail address and API token"
                )
            excerpt = response.text[:_BODY_EXCERPT].replace(token, "***")
            raise AtlassianError(f"HTTP {status} for {label}: {excerpt}")


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    try:
        return int(str(retry_after).strip()) if retry_after is not None else 2**attempt
    except ValueError:
        return 2**attempt


def to_utc_iso(value: str | None) -> str | None:
    """An Atlassian timestamp as UTC `YYYY-MM-DDTHH:MM:SS.fffZ`; None if empty, ValueError if unparseable."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a UTC offset: {value!r}")
    utc = parsed.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


_INLINE_TYPES = frozenset(
    {"text", "hardBreak", "mention", "emoji", "inlineCard", "status", "date", "media", "inlineExtension"}
)
_CARD_TYPES = frozenset({"inlineCard", "blockCard", "embedCard"})
_ATTACHMENT_TYPES = frozenset({"media", "mediaSingle", "mediaGroup"})
_CELL_TYPES = frozenset({"tableCell", "tableHeader"})
_LIST_INDENT = "  "
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")


def adf_to_text(node: Mapping | None) -> str:
    """Readable plain text of an ADF document; a plain string (Jira API v2 body) is returned stripped."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    text = _TRAILING_SPACE.sub("", _render(node))
    return _BLANK_RUNS.sub("\n\n", text).strip()


def adf_mentions(node: Mapping | None) -> list[str]:
    """accountIds of all `mention` nodes in document order, de-duplicated (CON-34)."""
    found: dict[str, None] = {}

    def visit(current: Any) -> None:
        if not isinstance(current, Mapping):
            return
        if current.get("type") == "mention":
            account_id = _attrs(current).get("id")
            if account_id:
                found.setdefault(str(account_id), None)
        for child in current.get("content") or []:
            visit(child)

    visit(node)
    return list(found)


def _attrs(node: Mapping) -> Mapping:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, Mapping) else {}


def _children(node: Mapping) -> list[Mapping]:
    return [child for child in node.get("content") or [] if isinstance(child, Mapping)]


def _content(node: Mapping) -> str:
    """Children of a container: concatenated when all are inline, else one blank line between blocks."""
    children = _children(node)
    if all(child.get("type") in _INLINE_TYPES for child in children):
        return "".join(_render(child) for child in children)
    rendered = (_render(child) for child in children)
    return "\n\n".join(block for block in rendered if block)


def _render(node: Mapping) -> str:
    kind = node.get("type")
    attrs = _attrs(node)
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "heading":
        return "#" * int(attrs.get("level") or 1) + " " + _content(node)
    if kind == "codeBlock":
        return "".join(str(child.get("text") or "") for child in _children(node))
    if kind == "blockquote":
        return "\n".join("> " + line for line in _content(node).split("\n"))
    if kind == "rule":
        return "---"
    if kind in ("bulletList", "orderedList"):
        return _list(node)
    if kind == "table":
        return "\n".join(_row(row) for row in _children(node))
    if kind == "mention":
        return str(attrs.get("text") or "@" + str(attrs.get("id") or ""))
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind in _CARD_TYPES:
        return str(attrs.get("url") or "")
    if kind == "status":
        return "[" + str(attrs.get("text") or "") + "]"
    if kind == "date":
        return _date(attrs.get("timestamp"))
    if kind in _ATTACHMENT_TYPES:
        return "[attachment]"
    return _content(node)


def _list(node: Mapping) -> str:
    ordered = node.get("type") == "orderedList"
    number = _start_number(_attrs(node).get("order"))
    lines: list[str] = []
    for item in _children(node):
        marker = f"{number}. " if ordered else "- "
        number += 1
        body = "\n".join(block for block in (_render(child) for child in _children(item)) if block)
        first, *rest = body.split("\n")
        lines.append(marker + first)
        lines.extend(_LIST_INDENT + line for line in rest)
    return "\n".join(lines)


def _start_number(order: Any) -> int:
    try:
        return int(order)
    except (TypeError, ValueError):
        return 1


def _row(row: Mapping) -> str:
    cells = [child for child in _children(row) if child.get("type") in _CELL_TYPES]
    return "\t".join(" ".join(_content(cell).split()) for cell in cells)


def _date(timestamp: Any) -> str:
    try:
        moment = datetime.fromtimestamp(int(timestamp) / _MILLIS_PER_SECOND, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d")
