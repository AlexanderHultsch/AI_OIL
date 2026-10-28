"""Comments: the user's own notes on an issue, with soft delete and full history (DM-19, DM-30)."""

from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import history, issues, people
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.timeutil import utcnow_iso

_SELECT = (
    "SELECT c.id, c.issue_id, i.key AS issue_key, c.author_id, c.body, c.proposal_id,"
    " c.created_at, c.edited_at, c.deleted_at FROM comment c JOIN issue i ON i.id = c.issue_id"
)


@dataclass(frozen=True)
class Comment:
    id: int
    issue_id: int
    issue_key: str
    author_id: int
    body: str
    proposal_id: int | None
    created_at: str
    edited_at: str | None
    deleted_at: str | None


def _body(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("comment body must not be blank (DM-19)")
    return value.strip()


def get_comment(conn: sqlalchemy.Connection, comment_id: int) -> Comment:
    row = conn.execute(text(f"{_SELECT} WHERE c.id = :id"), {"id": comment_id}).mappings().first()
    if row is None:
        raise NotFoundError(f"comment {comment_id} not found")
    return Comment(**row)


def list_comments(conn: sqlalchemy.Connection, key: str, *, include_deleted: bool = False) -> list[Comment]:
    """The comments of an issue, oldest first; deleted ones only on request (DM-19)."""
    issue = issues.get_issue(conn, key)
    where = "c.issue_id = :issue_id" + ("" if include_deleted else " AND c.deleted_at IS NULL")
    rows = conn.execute(
        text(f"{_SELECT} WHERE {where} ORDER BY c.created_at, c.id"), {"issue_id": issue.id}
    ).mappings()
    return [Comment(**row) for row in rows.all()]


def add_comment(
    conn: sqlalchemy.Connection,
    key: str,
    body: str,
    *,
    author_id: int,
    proposal_id: int | None = None,
) -> Comment:
    """Add a comment and record its text in history (DM-19, DM-30)."""
    issue = issues.get_issue(conn, key)
    body = _body(body)
    author = people.surviving_person(conn, author_id)
    now = utcnow_iso()
    comment_id = conn.execute(
        text(
            "INSERT INTO comment (issue_id, author_id, body, proposal_id, created_at)"
            " VALUES (:issue_id, :author_id, :body, :proposal_id, :now) RETURNING id"
        ),
        {"issue_id": issue.id, "author_id": author.id, "body": body, "proposal_id": proposal_id, "now": now},
    ).scalar_one()
    change_set_id = history.start_change_set(
        conn, "user", actor_id=author.id, proposal_id=proposal_id, note=f"comment {comment_id}"
    )
    history.record(conn, change_set_id, issue.id, "comment", "add", None, body)
    issues._touch(conn, issue.id, now)
    return get_comment(conn, comment_id)


def edit_comment(conn: sqlalchemy.Connection, comment_id: int, body: str, *, actor_id: int) -> Comment:
    """Replace the body of a live comment; an unchanged body is a no-op (DM-19, DM-30)."""
    comment = get_comment(conn, comment_id)
    if comment.deleted_at is not None:
        raise ValidationError(f"comment {comment_id} is deleted and cannot be edited (DM-19)")
    body = _body(body)
    if body == comment.body:
        return comment
    now = utcnow_iso()
    conn.execute(
        text("UPDATE comment SET body = :body, edited_at = :now WHERE id = :id"),
        {"body": body, "now": now, "id": comment_id},
    )
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id, note=f"comment {comment_id}")
    history.record(conn, change_set_id, comment.issue_id, "comment", "set", comment.body, body)
    issues._touch(conn, comment.issue_id, now)
    return get_comment(conn, comment_id)


def delete_comment(conn: sqlalchemy.Connection, comment_id: int, *, actor_id: int) -> None:
    """Soft-delete a comment; the row and its body stay. Deleting twice is a no-op (DM-19, DM-30)."""
    comment = get_comment(conn, comment_id)
    if comment.deleted_at is not None:
        return
    now = utcnow_iso()
    conn.execute(text("UPDATE comment SET deleted_at = :now WHERE id = :id"), {"now": now, "id": comment_id})
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id, note=f"comment {comment_id}")
    history.record(conn, change_set_id, comment.issue_id, "comment", "delete", comment.body, None)
    issues._touch(conn, comment.issue_id, now)
