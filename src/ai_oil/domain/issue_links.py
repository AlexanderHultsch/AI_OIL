"""Typed links between issues, recorded in history on both sides (DM-14, DM-30)."""

from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import history, issues
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.timeutil import utcnow_iso

LINK_TYPES = ("relates_to", "blocks", "duplicates")

_OUTWARD_LABEL = {"relates_to": "relates to", "blocks": "blocks", "duplicates": "duplicates"}
_INWARD_LABEL = {"relates_to": "relates to", "blocks": "is blocked by", "duplicates": "is duplicated by"}


@dataclass(frozen=True)
class IssueLinkView:
    """One link as seen from one of its two issues; `direction` is outward when that issue is `from_issue`."""

    id: int
    link_type: str
    direction: str
    label: str
    other_key: str
    other_summary: str
    other_status: str


def _link_type(value: object) -> str:
    if not isinstance(value, str) or value not in LINK_TYPES:
        raise ValidationError(f"link_type must be one of {', '.join(LINK_TYPES)}, got {value!r}")
    return value


def _resolve(
    conn: sqlalchemy.Connection, from_key: str, link_type: object, to_key: str
) -> tuple[str, issues.Issue, issues.Issue]:
    """Validate the type and both issues; return the pair in stored order (DM-14)."""
    link_type = _link_type(link_type)
    source = issues.get_issue(conn, from_key)
    target = issues.get_issue(conn, to_key)
    if source.id == target.id:
        raise ValidationError(f"{source.key} cannot be linked to itself")
    if link_type == "relates_to" and source.id > target.id:
        source, target = target, source
    return link_type, source, target


def _texts(link_type: str, source: issues.Issue, target: issues.Issue) -> tuple[str, str]:
    """History texts for the from-issue and the to-issue."""
    return (
        f"{_OUTWARD_LABEL[link_type]} {target.key}",
        f"{_INWARD_LABEL[link_type]} {source.key}",
    )


def link_issues(
    conn: sqlalchemy.Connection, from_key: str, link_type: str, to_key: str, *, actor_id: int
) -> int:
    """Link two issues and record the link on both; returns the link id (DM-14, DM-30)."""
    link_type, source, target = _resolve(conn, from_key, link_type, to_key)
    existing = conn.execute(
        text(
            "SELECT from_issue_id FROM issue_link WHERE link_type = :link_type"
            " AND ((from_issue_id = :source AND to_issue_id = :target)"
            " OR (from_issue_id = :target AND to_issue_id = :source))"
        ),
        {"link_type": link_type, "source": source.id, "target": target.id},
    ).first()
    if existing is not None:
        if existing.from_issue_id == source.id:
            raise ConflictError(f"{source.key} {_OUTWARD_LABEL[link_type]} {target.key} already exists")
        raise ValidationError(
            f"{target.key} {_OUTWARD_LABEL[link_type]} {source.key} already exists; "
            f"the reverse link would contradict it"
        )

    now = utcnow_iso()
    link_id = conn.execute(
        text(
            "INSERT INTO issue_link (link_type, from_issue_id, to_issue_id, created_at)"
            " VALUES (:link_type, :source, :target, :now) RETURNING id"
        ),
        {"link_type": link_type, "source": source.id, "target": target.id, "now": now},
    ).scalar_one()
    outward, inward = _texts(link_type, source, target)
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, source.id, "issue_link", "add", None, outward)
    history.record(conn, change_set_id, target.id, "issue_link", "add", None, inward)
    issues._touch(conn, source.id, now)
    issues._touch(conn, target.id, now)
    return link_id


def unlink_issues(
    conn: sqlalchemy.Connection, from_key: str, link_type: str, to_key: str, *, actor_id: int
) -> None:
    """Remove a link and record the removal on both issues; relates_to matches either order (DM-14, DM-30)."""
    link_type, source, target = _resolve(conn, from_key, link_type, to_key)
    removed = conn.execute(
        text(
            "DELETE FROM issue_link WHERE link_type = :link_type"
            " AND from_issue_id = :source AND to_issue_id = :target"
        ),
        {"link_type": link_type, "source": source.id, "target": target.id},
    ).rowcount
    if not removed:
        raise NotFoundError(f"no {link_type} link from {source.key} to {target.key}")

    now = utcnow_iso()
    outward, inward = _texts(link_type, source, target)
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, source.id, "issue_link", "remove", outward, None)
    history.record(conn, change_set_id, target.id, "issue_link", "remove", inward, None)
    issues._touch(conn, source.id, now)
    issues._touch(conn, target.id, now)


def list_issue_links(conn: sqlalchemy.Connection, key: str) -> list[IssueLinkView]:
    """All links of an issue as seen from it, ordered by link type, then the other issue's number (DM-14)."""
    issue = issues.get_issue(conn, key)
    rows = conn.execute(
        text(
            "SELECT l.id, l.link_type, (l.from_issue_id = :id) AS outward, o.key AS other_key,"
            " o.summary AS other_summary, o.status AS other_status"
            " FROM issue_link l"
            " JOIN issue o ON o.id ="
            " CASE WHEN l.from_issue_id = :id THEN l.to_issue_id ELSE l.from_issue_id END"
            " WHERE l.from_issue_id = :id OR l.to_issue_id = :id"
            " ORDER BY l.link_type, o.number, o.key, l.id"
        ),
        {"id": issue.id},
    ).mappings()
    return [
        IssueLinkView(
            id=row["id"],
            link_type=row["link_type"],
            direction="outward" if row["outward"] else "inward",
            label=(_OUTWARD_LABEL if row["outward"] else _INWARD_LABEL)[row["link_type"]],
            other_key=row["other_key"],
            other_summary=row["other_summary"],
            other_status=row["other_status"],
        )
        for row in rows.all()
    ]
