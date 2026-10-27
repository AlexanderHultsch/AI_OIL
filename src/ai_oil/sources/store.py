"""Stored source content: objects, versions, entries (DM-42 .. DM-53). Nothing fetched is ever overwritten (AP-3)."""  # noqa: E501

import hashlib
import json
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import people
from ai_oil.errors import NotFoundError
from ai_oil.sources.blobs import BlobStore
from ai_oil.sources.model import EntryData, FetchResult, ObjectMeta, ObjectState, VersionData
from ai_oil.timeutil import utcnow_iso

_OBJECT_COLUMNS = (
    "id, connector_id, object_type, external_id, parent_id, title, display_ref, locator, cursor,"
    " first_seen_at, last_checked_at, last_changed_at, fetch_status, fetch_error"
)
_VERSION_COLUMNS = (
    "id, source_object_id, version_no, fetched_at, source_modified_at, source_version_ref, content_hash,"
    " text, metadata_json, blob_hash, sync_run_id"
)
_ENTRY_COLUMNS = (
    "id, source_object_id, entry_type, external_id, revision, author_identity_id, direction, recipients_json,"
    " occurred_at, fetched_at, text, new_text, content_hash, metadata_json, blob_hash, sync_run_id"
)


@dataclass(frozen=True)
class SourceObject:
    id: int
    connector_id: int
    object_type: str
    external_id: str
    parent_id: int | None
    title: str | None
    display_ref: str | None
    locator: str | None
    cursor: str | None
    first_seen_at: str
    last_checked_at: str | None
    last_changed_at: str | None
    fetch_status: str
    fetch_error: str | None


@dataclass(frozen=True)
class SourceVersion:
    id: int
    source_object_id: int
    version_no: int
    fetched_at: str
    source_modified_at: str | None
    source_version_ref: str | None
    content_hash: str
    text: str
    metadata: dict
    blob_hash: str | None
    sync_run_id: int | None


@dataclass(frozen=True)
class SourceEntry:
    id: int
    source_object_id: int
    entry_type: str
    external_id: str
    revision: int
    author_identity_id: int | None
    direction: str | None
    recipients: list[int]
    occurred_at: str
    fetched_at: str
    text: str
    new_text: str | None
    content_hash: str
    metadata: dict
    blob_hash: str | None
    sync_run_id: int | None


@dataclass(frozen=True)
class ApplyOutcome:
    new_version_ids: tuple[int, ...]
    new_entry_ids: tuple[int, ...]
    child_ids: tuple[int, ...]
    gone: bool

    @property
    def changed(self) -> bool:
        return bool(self.new_version_ids or self.new_entry_ids)


def version_hash(data: VersionData) -> str:
    """sha256 over the text plus the metadata exactly as the connector delivered it (DM-48)."""
    metadata = json.dumps(dict(data.metadata), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((data.text + "\n" + metadata).encode("utf-8")).hexdigest()


def entry_hash(data: EntryData) -> str:
    """sha256 over the entry text; a changed text is a new revision (DM-51)."""
    return hashlib.sha256(data.text.encode("utf-8")).hexdigest()


def _object(row: sqlalchemy.RowMapping) -> SourceObject:
    return SourceObject(**row)


def _version(row: sqlalchemy.RowMapping) -> SourceVersion:
    values = dict(row)
    values["metadata"] = json.loads(values.pop("metadata_json"))
    return SourceVersion(**values)


def _entry(row: sqlalchemy.RowMapping) -> SourceEntry:
    values = dict(row)
    values["metadata"] = json.loads(values.pop("metadata_json"))
    recipients_json = values.pop("recipients_json")
    values["recipients"] = json.loads(recipients_json) if recipients_json is not None else []
    return SourceEntry(**values)


def upsert_object(
    conn: sqlalchemy.Connection, connector_id: int, meta: ObjectMeta, *, parent_id: int | None = None
) -> int:
    """Find or create a source object (DM-42). The cursor is only ever set by `apply_fetch` (DM-45)."""
    existing = find_object(conn, connector_id, meta.object_type, meta.external_id)
    if existing is None:
        return conn.execute(
            text(
                "INSERT INTO source_object"
                " (connector_id, object_type, external_id, parent_id, title, display_ref, locator,"
                " first_seen_at, fetch_status)"
                " VALUES (:connector_id, :object_type, :external_id, :parent_id, :title, :display_ref,"
                " :locator, :now, 'pending') RETURNING id"
            ),
            {
                "connector_id": connector_id,
                "object_type": meta.object_type,
                "external_id": meta.external_id,
                "parent_id": parent_id,
                "title": meta.title,
                "display_ref": meta.display_ref,
                "locator": meta.locator,
                "now": utcnow_iso(),
            },
        ).scalar_one()
    updates = {
        column: value
        for column, value in (
            ("title", meta.title),
            ("display_ref", meta.display_ref),
            ("locator", meta.locator),
            ("parent_id", parent_id),
        )
        if value is not None
    }
    if updates:
        assignments = ", ".join(f"{column} = :{column}" for column in updates)
        conn.execute(
            text(f"UPDATE source_object SET {assignments} WHERE id = :id"), {**updates, "id": existing.id}
        )
    return existing.id


def get_object(conn: sqlalchemy.Connection, object_id: int) -> SourceObject:
    row = (
        conn.execute(text(f"SELECT {_OBJECT_COLUMNS} FROM source_object WHERE id = :id"), {"id": object_id})
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"source object {object_id} not found")
    return _object(row)


def find_object(
    conn: sqlalchemy.Connection, connector_id: int, object_type: str, external_id: str
) -> SourceObject | None:
    row = (
        conn.execute(
            text(
                f"SELECT {_OBJECT_COLUMNS} FROM source_object"
                " WHERE connector_id = :connector_id AND object_type = :object_type"
                " AND external_id = :external_id"
            ),
            {"connector_id": connector_id, "object_type": object_type, "external_id": external_id},
        )
        .mappings()
        .first()
    )
    return _object(row) if row is not None else None


def list_children(conn: sqlalchemy.Connection, object_id: int) -> list[SourceObject]:
    rows = conn.execute(
        text(f"SELECT {_OBJECT_COLUMNS} FROM source_object WHERE parent_id = :id ORDER BY id"),
        {"id": object_id},
    ).mappings()
    return [_object(row) for row in rows]


def object_state(conn: sqlalchemy.Connection, object_id: int) -> ObjectState:
    """The stored object as the sync engine hands it to `fetch`."""
    source = get_object(conn, object_id)
    latest = latest_version(conn, object_id)
    return ObjectState(
        id=source.id,
        connector_id=source.connector_id,
        object_type=source.object_type,
        external_id=source.external_id,
        title=source.title,
        display_ref=source.display_ref,
        locator=source.locator,
        cursor=source.cursor,
        parent_id=source.parent_id,
        latest_content_hash=latest.content_hash if latest is not None else None,
    )


def latest_version(conn: sqlalchemy.Connection, object_id: int) -> SourceVersion | None:
    row = (
        conn.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM source_version"
                " WHERE source_object_id = :id ORDER BY version_no DESC LIMIT 1"
            ),
            {"id": object_id},
        )
        .mappings()
        .first()
    )
    return _version(row) if row is not None else None


def list_versions(conn: sqlalchemy.Connection, object_id: int) -> list[SourceVersion]:
    rows = conn.execute(
        text(
            f"SELECT {_VERSION_COLUMNS} FROM source_version WHERE source_object_id = :id ORDER BY version_no"
        ),
        {"id": object_id},
    ).mappings()
    return [_version(row) for row in rows]


def get_version(conn: sqlalchemy.Connection, version_id: int) -> SourceVersion:
    row = (
        conn.execute(
            text(f"SELECT {_VERSION_COLUMNS} FROM source_version WHERE id = :id"), {"id": version_id}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"source version {version_id} not found")
    return _version(row)


def list_entries(
    conn: sqlalchemy.Connection, object_id: int, *, all_revisions: bool = False
) -> list[SourceEntry]:
    """Entries of an object by time; by default only the latest revision of each entry (DM-51)."""
    latest_only = (
        ""
        if all_revisions
        else " AND revision = (SELECT MAX(other.revision) FROM source_entry other"
        " WHERE other.source_object_id = source_entry.source_object_id"
        " AND other.entry_type = source_entry.entry_type AND other.external_id = source_entry.external_id)"
    )
    rows = conn.execute(
        text(
            f"SELECT {_ENTRY_COLUMNS} FROM source_entry WHERE source_object_id = :id{latest_only}"
            " ORDER BY occurred_at, id"
        ),
        {"id": object_id},
    ).mappings()
    return [_entry(row) for row in rows]


def _truncate(value: str | None, max_text_chars: int | None) -> tuple[str | None, bool]:
    if value is None or max_text_chars is None or len(value) <= max_text_chars:
        return value, False
    return value[:max_text_chars], True


def _store_raw(
    conn: sqlalchemy.Connection,
    raw: bytes | None,
    media_type: str | None,
    blobs: BlobStore | None,
    metadata: dict,
) -> str | None:
    if not raw or blobs is None:
        return None
    blob_hash = blobs.put(conn, raw, media_type)
    if blob_hash is None:
        metadata["raw_skipped"] = "too large"
    return blob_hash


def write_version(
    conn: sqlalchemy.Connection,
    object_id: int,
    data: VersionData,
    *,
    sync_run_id: int | None = None,
    blobs: BlobStore | None = None,
    max_text_chars: int | None = None,
) -> int | None:
    """Append a version unless the content equals the latest one (DM-47 .. DM-49); returns its id or None."""
    content_hash = version_hash(data)
    latest = conn.execute(
        text(
            "SELECT version_no, content_hash FROM source_version"
            " WHERE source_object_id = :id ORDER BY version_no DESC LIMIT 1"
        ),
        {"id": object_id},
    ).first()
    if latest is not None and latest.content_hash == content_hash:
        return None
    stored_text, truncated = _truncate(data.text, max_text_chars)
    metadata = dict(data.metadata)
    if truncated:
        metadata["truncated"] = True
    blob_hash = _store_raw(conn, data.raw, data.media_type, blobs, metadata)
    return conn.execute(
        text(
            "INSERT INTO source_version (source_object_id, version_no, fetched_at, source_modified_at,"
            " source_version_ref, content_hash, text, metadata_json, blob_hash, sync_run_id)"
            " VALUES (:object_id, :version_no, :now, :source_modified_at, :source_version_ref,"
            " :content_hash, :text, :metadata_json, :blob_hash, :sync_run_id) RETURNING id"
        ),
        {
            "object_id": object_id,
            "version_no": 1 if latest is None else latest.version_no + 1,
            "now": utcnow_iso(),
            "source_modified_at": data.source_modified_at,
            "source_version_ref": data.source_version_ref,
            "content_hash": content_hash,
            "text": stored_text,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, default=str),
            "blob_hash": blob_hash,
            "sync_run_id": sync_run_id,
        },
    ).scalar_one()


def write_entry(
    conn: sqlalchemy.Connection,
    object_id: int,
    data: EntryData,
    *,
    sync_run_id: int | None = None,
    blobs: BlobStore | None = None,
    max_text_chars: int | None = None,
) -> int | None:
    """Append an entry, or a new revision of an edited one; unchanged entries are skipped (DM-50 .. DM-53)."""
    content_hash = entry_hash(data)
    latest = conn.execute(
        text(
            "SELECT revision, content_hash FROM source_entry"
            " WHERE source_object_id = :id AND entry_type = :entry_type AND external_id = :external_id"
            " ORDER BY revision DESC LIMIT 1"
        ),
        {"id": object_id, "entry_type": data.entry_type, "external_id": data.external_id},
    ).first()
    if latest is not None and latest.content_hash == content_hash:
        return None

    author_identity_id: int | None = None
    direction = data.direction
    if data.author is not None:
        author = data.author
        identity = people.resolve_identity(conn, author.system, author.external_id, author.display_name)
        author_identity_id = identity.id
        if direction is None:
            person = people.find_person_by_identity(conn, author.system, author.external_id)
            direction = "out" if person is not None and person.is_me else "in"
    recipient_ids = [
        people.resolve_identity(conn, ref.system, ref.external_id, ref.display_name).id
        for ref in data.recipients
    ]

    stored_text, truncated = _truncate(data.text, max_text_chars)
    stored_new_text, _ = _truncate(data.new_text, max_text_chars)
    metadata = dict(data.metadata)
    if truncated:
        metadata["truncated"] = True
    blob_hash = _store_raw(conn, data.raw, data.media_type, blobs, metadata)
    return conn.execute(
        text(
            "INSERT INTO source_entry (source_object_id, entry_type, external_id, revision,"
            " author_identity_id, direction, recipients_json, occurred_at, fetched_at, text, new_text,"
            " content_hash, metadata_json, blob_hash, sync_run_id)"
            " VALUES (:object_id, :entry_type, :external_id, :revision, :author_identity_id, :direction,"
            " :recipients_json, :occurred_at, :now, :text, :new_text, :content_hash, :metadata_json,"
            " :blob_hash, :sync_run_id) RETURNING id"
        ),
        {
            "object_id": object_id,
            "entry_type": data.entry_type,
            "external_id": data.external_id,
            "revision": 1 if latest is None else latest.revision + 1,
            "author_identity_id": author_identity_id,
            "direction": direction,
            "recipients_json": json.dumps(recipient_ids) if recipient_ids else None,
            "occurred_at": data.occurred_at,
            "now": utcnow_iso(),
            "text": stored_text,
            "new_text": stored_new_text,
            "content_hash": content_hash,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, default=str),
            "blob_hash": blob_hash,
            "sync_run_id": sync_run_id,
        },
    ).scalar_one()


def mark_error(conn: sqlalchemy.Connection, object_id: int, message: str) -> None:
    """Record a failed fetch (DM-46); stored versions and entries stay untouched."""
    conn.execute(
        text(
            "UPDATE source_object SET fetch_status = 'error', fetch_error = :message,"
            " last_checked_at = :now WHERE id = :id"
        ),
        {"message": message, "now": utcnow_iso(), "id": object_id},
    )


def _mark_fetched(
    conn: sqlalchemy.Connection,
    object_id: int,
    *,
    now: str,
    cursor: str | None,
    title: str | None,
    changed: bool,
) -> None:
    conn.execute(
        text(
            "UPDATE source_object SET fetch_status = 'ok', fetch_error = NULL, last_checked_at = :now,"
            " cursor = COALESCE(:cursor, cursor), title = COALESCE(:title, title),"
            " last_changed_at = CASE WHEN :changed THEN :now ELSE last_changed_at END WHERE id = :id"
        ),
        {"now": now, "cursor": cursor, "title": title, "changed": int(changed), "id": object_id},
    )


def apply_fetch(
    conn: sqlalchemy.Connection,
    state: ObjectState,
    result: FetchResult,
    *,
    sync_run_id: int | None = None,
    blobs: BlobStore | None = None,
    max_text_chars: int | None = None,
) -> ApplyOutcome:
    """Store what `fetch` returned for one object and its children (SYNC-2, DM-45, DM-46)."""
    now = utcnow_iso()
    if result.status == "gone":
        conn.execute(
            text(
                "UPDATE source_object SET fetch_status = 'gone', fetch_error = NULL, last_checked_at = :now"
                " WHERE id = :id"
            ),
            {"now": now, "id": state.id},
        )
        return ApplyOutcome((), (), (), True)

    options = {"sync_run_id": sync_run_id, "blobs": blobs, "max_text_chars": max_text_chars}

    def store_content(
        object_id: int, version: VersionData | None, entries: tuple[EntryData, ...]
    ) -> tuple[list[int], list[int]]:
        version_ids = []
        if version is not None:
            version_id = write_version(conn, object_id, version, **options)
            if version_id is not None:
                version_ids.append(version_id)
        entry_ids = [
            entry_id
            for entry_id in (write_entry(conn, object_id, entry, **options) for entry in entries)
            if entry_id is not None
        ]
        return version_ids, entry_ids

    new_version_ids, new_entry_ids = store_content(state.id, result.version, result.entries)
    changed = bool(new_version_ids or new_entry_ids)
    child_ids: list[int] = []
    for child in result.children:
        child_id = upsert_object(conn, state.connector_id, child.meta, parent_id=state.id)
        child_versions, child_entries = store_content(child_id, child.version, child.entries)
        child_changed = bool(child_versions or child_entries)
        _mark_fetched(conn, child_id, now=now, cursor=child.meta.cursor, title=None, changed=child_changed)
        child_ids.append(child_id)
        new_version_ids += child_versions
        new_entry_ids += child_entries
        changed = changed or child_changed
    _mark_fetched(conn, state.id, now=now, cursor=result.cursor, title=result.title, changed=changed)
    return ApplyOutcome(tuple(new_version_ids), tuple(new_entry_ids), tuple(child_ids), False)
