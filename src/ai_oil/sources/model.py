"""The connector plugin contract (spec 6.1). Connectors return these types; the core stores them."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

OBJECT_TYPES = (
    "email_thread",
    "attachment",
    "jira_issue",
    "confluence_page",
    "file",
    "teams_thread",
    "note",
    "web_link",
)
ENTRY_TYPES = (
    "email_message",
    "jira_comment",
    "jira_change",
    "confluence_comment",
    "teams_message",
    "note_text",
)
_IDENTITY_SYSTEMS = ("email", "atlassian", "teams")
_DIRECTIONS = (None, "in", "out")
_FETCH_STATUSES = ("ok", "gone")


def _check_object_type(object_type: str) -> None:
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"unknown object type: {object_type!r}")


@dataclass(frozen=True)
class IdentityRef:
    system: str
    external_id: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.system not in _IDENTITY_SYSTEMS:
            raise ValueError(f"unknown identity system: {self.system!r}")


@dataclass(frozen=True)
class SourceRef:
    """Canonical reference returned by `parse_reference`, before `resolve`."""

    object_type: str
    external_id: str
    display_ref: str | None = None
    locator: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_object_type(self.object_type)


@dataclass(frozen=True)
class ObjectMeta:
    object_type: str
    external_id: str
    title: str | None = None
    display_ref: str | None = None
    locator: str | None = None
    cursor: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_object_type(self.object_type)


@dataclass(frozen=True)
class ObjectState:
    """A stored source object as the sync engine hands it to `fetch`."""

    id: int
    connector_id: int
    object_type: str
    external_id: str
    title: str | None
    display_ref: str | None
    locator: str | None
    cursor: str | None
    parent_id: int | None
    latest_content_hash: str | None


@dataclass(frozen=True)
class VersionData:
    text: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    source_modified_at: str | None = None
    source_version_ref: str | None = None
    raw: bytes | None = None
    media_type: str | None = None


@dataclass(frozen=True)
class EntryData:
    entry_type: str
    external_id: str
    occurred_at: str
    text: str
    new_text: str | None = None
    author: IdentityRef | None = None
    direction: str | None = None
    recipients: tuple[IdentityRef, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    raw: bytes | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if self.entry_type not in ENTRY_TYPES:
            raise ValueError(f"unknown entry type: {self.entry_type!r}")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"unknown direction: {self.direction!r}")


@dataclass(frozen=True)
class ChildData:
    meta: ObjectMeta
    version: VersionData | None = None
    entries: tuple[EntryData, ...] = ()


@dataclass(frozen=True)
class FetchResult:
    """What `fetch` returns; `status` is "ok" or "gone" (deleted or no longer accessible)."""

    status: str = "ok"
    version: VersionData | None = None
    entries: tuple[EntryData, ...] = ()
    children: tuple[ChildData, ...] = ()
    cursor: str | None = None
    title: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _FETCH_STATUSES:
            raise ValueError(f"unknown fetch status: {self.status!r}")


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class HealthReport:
    checks: tuple[HealthCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


@dataclass(frozen=True)
class CandidateQuery:
    text: str
    since: str | None = None
    limit: int = 20


@dataclass(frozen=True)
class ConnectorConfig:
    id: int
    type: str
    name: str
    enabled: bool
    settings: Mapping[str, object]
    credential_ref: str | None


class Connector(Protocol):
    """A connector plugin (CON-1, CON-6).

    Connectors are constructed as `ConnectorClass(config: ConnectorConfig, **backends)`. The backends are
    injectable system-access objects (COM, HTTP, file system) so tests run against fixtures (CON-6).
    Read-only connectors declare `action_class` "A" (CON-1).
    `max_parallel` caps concurrent fetches per connector (SYNC-3); the sync engine treats a missing
    attribute as 1.
    """

    type: ClassVar[str]
    object_types: ClassVar[frozenset[str]]
    action_class: ClassVar[str]
    max_parallel: ClassVar[int]

    def check(self) -> HealthReport: ...

    def parse_reference(self, text: str) -> SourceRef | None: ...

    def resolve(self, ref: SourceRef) -> ObjectMeta: ...

    def fetch(self, state: ObjectState) -> FetchResult: ...

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]: ...
