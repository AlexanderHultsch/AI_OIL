"""Links between issues and source objects: each issue's dictionary of sources (DM-54 .. DM-59)."""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import bindparam, text

from ai_oil.domain import history, issues
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.sources import store
from ai_oil.sources.store import SourceObject
from ai_oil.timeutil import utcnow_iso

ROLES = ("origin", "evidence", "reference")
LINK_METHODS = ("manual", "ai_suggestion")

_SELECT = (
    "SELECT l.id AS link_id, l.issue_id, i.key AS issue_key, l.source_object_id, l.role, l.link_method,"
    " l.proposal_id, l.note, l.created_at, l.removed_at, o.connector_id, c.name AS connector_name,"
    " c.type AS connector_type, o.object_type, o.external_id, o.title, o.display_ref, o.locator,"
    " o.fetch_status, o.fetch_error, o.last_checked_at, o.last_changed_at,"
    " (SELECT COUNT(*) FROM source_version v WHERE v.source_object_id = o.id) AS version_count,"
    " (SELECT COUNT(*) FROM (SELECT DISTINCT e.entry_type, e.external_id FROM source_entry e"
    " WHERE e.source_object_id = o.id)) AS entry_count,"
    " (SELECT COUNT(*) FROM source_object k WHERE k.parent_id = o.id) AS child_count"
    " FROM issue_source_link l"
    " JOIN issue i ON i.id = l.issue_id"
    " JOIN source_object o ON o.id = l.source_object_id"
    " JOIN connector c ON c.id = o.connector_id"
)


@dataclass(frozen=True)
class LinkedSource:
    """One link of an issue together with the linked object's current state and content counts."""

    link_id: int
    issue_id: int
    issue_key: str
    source_object_id: int
    role: str
    link_method: str
    proposal_id: int | None
    note: str | None
    created_at: str
    removed_at: str | None
    connector_id: int
    connector_name: str
    connector_type: str
    object_type: str
    external_id: str
    title: str | None
    display_ref: str | None
    locator: str | None
    fetch_status: str
    fetch_error: str | None
    last_checked_at: str | None
    last_changed_at: str | None
    version_count: int
    entry_count: int
    child_count: int


def source_label(obj: SourceObject) -> str:
    """The text that stands for a source object in issue history (DM-58)."""
    return f"{obj.object_type}: {obj.display_ref or obj.title or obj.external_id}"


def _choice(field: str, value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _get_link(conn: sqlalchemy.Connection, link_id: int) -> LinkedSource:
    row = conn.execute(text(f"{_SELECT} WHERE l.id = :id"), {"id": link_id}).mappings().one()
    return LinkedSource(**row)


def _active_link_id(conn: sqlalchemy.Connection, issue_id: int, source_object_id: int) -> int | None:
    return conn.execute(
        text(
            "SELECT id FROM issue_source_link"
            " WHERE issue_id = :issue_id AND source_object_id = :source_object_id AND removed_at IS NULL"
        ),
        {"issue_id": issue_id, "source_object_id": source_object_id},
    ).scalar_one_or_none()


def link_source(
    conn: sqlalchemy.Connection,
    issue_key: str,
    source_object_id: int,
    *,
    actor_id: int | None,
    role: str = "reference",
    link_method: str = "manual",
    proposal_id: int | None = None,
    note: str | None = None,
    actor_type: str = "user",
) -> LinkedSource:
    """Add a source object to an issue's dictionary of sources and record it in history (DM-54 .. DM-59)."""
    role = _choice("role", role, ROLES)
    link_method = _choice("link_method", link_method, LINK_METHODS)
    if link_method == "ai_suggestion" and proposal_id is None:
        raise ValidationError("an AI-suggested link must reference the accepted proposal (DM-56)")
    issue = issues.get_issue(conn, issue_key)
    obj = store.get_object(conn, source_object_id)
    if obj.parent_id is not None:
        parent = store.get_object(conn, obj.parent_id)
        raise ValidationError(
            f"{source_label(obj)} is part of {source_label(parent)} — link its parent instead (DM-44)"
        )
    if _active_link_id(conn, issue.id, obj.id) is not None:
        raise ConflictError(f"{issue.key} is already linked to {source_label(obj)}")
    if role == "origin":
        origin = conn.execute(
            text(
                "SELECT 1 FROM issue_source_link"
                " WHERE issue_id = :issue_id AND role = 'origin' AND removed_at IS NULL"
            ),
            {"issue_id": issue.id},
        ).first()
        if origin is not None:
            raise ConflictError(f"{issue.key} already has an origin source (DM-55)")

    now = utcnow_iso()
    link_id = conn.execute(
        text(
            "INSERT INTO issue_source_link"
            " (issue_id, source_object_id, role, link_method, proposal_id, note, created_at)"
            " VALUES (:issue_id, :source_object_id, :role, :link_method, :proposal_id, :note, :now)"
            " RETURNING id"
        ),
        {
            "issue_id": issue.id,
            "source_object_id": obj.id,
            "role": role,
            "link_method": link_method,
            "proposal_id": proposal_id,
            "note": note,
            "now": now,
        },
    ).scalar_one()
    change_set_id = history.start_change_set(conn, actor_type, actor_id=actor_id, proposal_id=proposal_id)
    history.record(conn, change_set_id, issue.id, "source_link", "add", None, source_label(obj))
    issues._touch(conn, issue.id, now)
    return _get_link(conn, link_id)


def unlink_source(
    conn: sqlalchemy.Connection,
    issue_key: str,
    source_object_id: int,
    *,
    actor_id: int | None,
    actor_type: str = "user",
) -> None:
    """Remove a source from an issue's dictionary; the link row stays with `removed_at` set (DM-57, DM-58)."""
    issue = issues.get_issue(conn, issue_key)
    link_id = _active_link_id(conn, issue.id, source_object_id)
    if link_id is None:
        raise NotFoundError(f"{issue.key} has no active link to source object {source_object_id}")
    obj = store.get_object(conn, source_object_id)
    now = utcnow_iso()
    conn.execute(
        text("UPDATE issue_source_link SET removed_at = :now WHERE id = :id"), {"now": now, "id": link_id}
    )
    change_set_id = history.start_change_set(conn, actor_type, actor_id=actor_id)
    history.record(conn, change_set_id, issue.id, "source_link", "remove", source_label(obj), None)
    issues._touch(conn, issue.id, now)


def list_issue_sources(
    conn: sqlalchemy.Connection, issue_key: str, *, include_removed: bool = False
) -> list[LinkedSource]:
    """The issue's dictionary of sources, ordered by connector type, then link time (DM-54, DM-57)."""
    issue = issues.get_issue(conn, issue_key)
    removed = "" if include_removed else " AND l.removed_at IS NULL"
    rows = conn.execute(
        text(f"{_SELECT} WHERE l.issue_id = :issue_id{removed} ORDER BY c.type, l.created_at, l.id"),
        {"issue_id": issue.id},
    ).mappings()
    return [LinkedSource(**row) for row in rows]


def linked_object_ids(conn: sqlalchemy.Connection, issue_key: str) -> list[int]:
    """Ids of the objects actively linked to an issue, ascending."""
    issue = issues.get_issue(conn, issue_key)
    return list(
        conn.execute(
            text(
                "SELECT source_object_id FROM issue_source_link"
                " WHERE issue_id = :issue_id AND removed_at IS NULL ORDER BY source_object_id"
            ),
            {"issue_id": issue.id},
        ).scalars()
    )


def issues_for_source(conn: sqlalchemy.Connection, source_object_id: int) -> list[str]:
    """Keys of the issues an object belongs to; a child belongs to the issues of its parent (DM-44)."""
    obj = store.get_object(conn, source_object_id)
    target_id = obj.parent_id if obj.parent_id is not None else obj.id
    return list(
        conn.execute(
            text(
                "SELECT i.key FROM issue_source_link l"
                " JOIN issue i ON i.id = l.issue_id JOIN project p ON p.id = i.project_id"
                " WHERE l.source_object_id = :id AND l.removed_at IS NULL ORDER BY p.key, i.number"
            ),
            {"id": target_id},
        ).scalars()
    )


def all_linked_object_ids(
    conn: sqlalchemy.Connection,
    *,
    statuses: Sequence[str] | None = None,
    resolved_after: str | None = None,
) -> list[int]:
    """Objects linked to an issue in `statuses` (any if None) or resolved at/after `resolved_after`."""
    where = ""
    params: dict[str, object] = {}
    if statuses is not None:
        conditions = ["i.status IN :statuses"]
        params["statuses"] = list(statuses)
        if resolved_after is not None:
            conditions.append("i.resolved_at >= :resolved_after")
            params["resolved_after"] = resolved_after
        where = f" AND ({' OR '.join(conditions)})"
    statement = text(
        "SELECT DISTINCT l.source_object_id FROM issue_source_link l JOIN issue i ON i.id = l.issue_id"
        f" WHERE l.removed_at IS NULL{where} ORDER BY l.source_object_id"
    )
    if statuses is not None:
        statement = statement.bindparams(bindparam("statuses", expanding=True))
    return list(conn.execute(statement, params).scalars())
