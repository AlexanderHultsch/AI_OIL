"""Manual connector: notes and web links (CON-10, CON-11)."""

import uuid
from typing import ClassVar
from urllib.parse import urlsplit

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import ValidationError
from ai_oil.sources import registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    FetchResult,
    HealthCheck,
    HealthReport,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)
from ai_oil.sources.registry import ConnectorRow
from ai_oil.timeutil import utcnow_iso

MANUAL_CONNECTOR_NAME = "Manual"
_SCHEMES = ("http://", "https://")
_DISPLAY_MAX = 80


def _normalise_url(value: str) -> tuple[str, str] | None:
    """The normalised URL (lower-case scheme and host, rest unchanged) and its display text, or None."""
    candidate = value.strip()
    scheme = next((s for s in _SCHEMES if candidate[: len(s)].lower() == s), None)
    if scheme is None:
        return None
    remainder = candidate[len(scheme) :]
    authority_end = min((i for i in (remainder.find(c) for c in "/?#") if i >= 0), default=len(remainder))
    userinfo, at, hostport = remainder[:authority_end].rpartition("@")
    normalised = f"{scheme}{userinfo}{at}{hostport.lower()}{remainder[authority_end:]}"
    try:
        parts = urlsplit(normalised)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return normalised, f"{host}{parts.path}"[:_DISPLAY_MAX]


class ManualConnector:
    """Notes are edited locally and web links carry metadata only, so nothing is ever fetched."""

    type: ClassVar[str] = "manual"
    object_types: ClassVar[frozenset[str]] = frozenset({"note", "web_link"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 4

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("manual", True, "always available"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        normalised = _normalise_url(text)
        if normalised is None:
            return None
        url, display = normalised
        return SourceRef("web_link", url, display_ref=display, locator=url)

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        if ref.object_type != "web_link":
            raise ValidationError(f"the manual connector cannot resolve a {ref.object_type} reference")
        return ObjectMeta(
            "web_link",
            ref.external_id,
            title=ref.external_id,
            display_ref=ref.display_ref,
            locator=ref.locator,
        )

    def fetch(self, state: ObjectState) -> FetchResult:
        return FetchResult()

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


def ensure_manual_connector(conn: sqlalchemy.Connection) -> ConnectorRow:
    """The manual connector instance, created on first use."""
    for row in registry.list_connectors(conn, type_name=ManualConnector.type):
        if row.name == MANUAL_CONNECTOR_NAME:
            return row
    return registry.create_connector(conn, ManualConnector.type, MANUAL_CONNECTOR_NAME)


def _mark_written(conn: sqlalchemy.Connection, object_id: int, *, checked: bool) -> None:
    now = utcnow_iso()
    if checked:
        conn.execute(
            text(
                "UPDATE source_object SET fetch_status = 'ok', fetch_error = NULL, last_checked_at = :now,"
                " last_changed_at = :now WHERE id = :id"
            ),
            {"now": now, "id": object_id},
        )
    else:
        conn.execute(
            text("UPDATE source_object SET last_changed_at = :now WHERE id = :id"),
            {"now": now, "id": object_id},
        )


def create_note(conn: sqlalchemy.Connection, title: str, text: str) -> int:
    """Create a note with its first version and return the object id (CON-10)."""
    title = title.strip()
    if not title:
        raise ValidationError("a note needs a title")
    if not text.strip():
        raise ValidationError("a note needs text")
    manual = ensure_manual_connector(conn)
    object_id = store.upsert_object(
        conn, manual.id, ObjectMeta("note", uuid.uuid4().hex, title=title, display_ref=title)
    )
    store.write_version(conn, object_id, VersionData(text=text))
    _mark_written(conn, object_id, checked=True)
    return object_id


def edit_note(
    conn: sqlalchemy.Connection, object_id: int, text: str, *, title: str | None = None
) -> int | None:
    """Store an edit of a note as a new version; returns its id, or None if the text is unchanged (CON-10)."""
    obj = store.get_object(conn, object_id)
    if obj.object_type != "note":
        raise ValidationError(f"source object {object_id} is a {obj.object_type}, not a note")
    if not text.strip():
        raise ValidationError("a note needs text")
    if title is not None:
        title = title.strip()
        if not title:
            raise ValidationError("a note needs a title")
        store.upsert_object(
            conn, obj.connector_id, ObjectMeta("note", obj.external_id, title=title, display_ref=title)
        )
    version_id = store.write_version(conn, object_id, VersionData(text=text))
    if version_id is not None:
        _mark_written(conn, object_id, checked=False)
    return version_id
