"""Append-only change history (DM-30 .. DM-33)."""

from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import ValidationError
from ai_oil.timeutil import utcnow_iso

ACTOR_TYPES = ("user", "sync", "system")
OPERATIONS = ("create", "set", "add", "remove", "delete")


@dataclass(frozen=True)
class HistoryEntry:
    """One change event together with the change set it belongs to."""

    change_set_id: int
    at: str
    actor_type: str
    actor_id: int | None
    proposal_id: int | None
    sync_run_id: int | None
    note: str | None
    field: str
    op: str
    old_value: str | None
    new_value: str | None


def start_change_set(
    conn: sqlalchemy.Connection,
    actor_type: str,
    *,
    actor_id: int | None = None,
    proposal_id: int | None = None,
    sync_run_id: int | None = None,
    note: str | None = None,
) -> int:
    """Open a change set, one per save (DM-30, DM-32). Returns its id."""
    if actor_type not in ACTOR_TYPES:
        raise ValidationError(f"actor_type must be one of {', '.join(ACTOR_TYPES)}, got {actor_type!r}")
    result = conn.execute(
        text(
            "INSERT INTO change_set (at, actor_type, actor_id, proposal_id, sync_run_id, note)"
            " VALUES (:at, :actor_type, :actor_id, :proposal_id, :sync_run_id, :note) RETURNING id"
        ),
        {
            "at": utcnow_iso(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "proposal_id": proposal_id,
            "sync_run_id": sync_run_id,
            "note": note,
        },
    )
    return result.scalar_one()


def _raw(value: object) -> str | None:
    return None if value is None else str(value)


def record(
    conn: sqlalchemy.Connection,
    change_set_id: int,
    issue_id: int,
    field: str,
    op: str,
    old_value: object = None,
    new_value: object = None,
) -> None:
    """Append one change event; values are stored raw as text, None as NULL (DM-30, DM-33)."""
    if op not in OPERATIONS:
        raise ValidationError(f"op must be one of {', '.join(OPERATIONS)}, got {op!r}")
    conn.execute(
        text(
            "INSERT INTO change_event (change_set_id, issue_id, field, op, old_value, new_value)"
            " VALUES (:change_set_id, :issue_id, :field, :op, :old_value, :new_value)"
        ),
        {
            "change_set_id": change_set_id,
            "issue_id": issue_id,
            "field": field,
            "op": op,
            "old_value": _raw(old_value),
            "new_value": _raw(new_value),
        },
    )


def issue_history(conn: sqlalchemy.Connection, issue_id: int) -> list[HistoryEntry]:
    """All change events of an issue in the order they were recorded."""
    rows = conn.execute(
        text(
            "SELECT cs.id AS change_set_id, cs.at, cs.actor_type, cs.actor_id, cs.proposal_id,"
            " cs.sync_run_id, cs.note, ce.field, ce.op, ce.old_value, ce.new_value"
            " FROM change_event ce JOIN change_set cs ON cs.id = ce.change_set_id"
            " WHERE ce.issue_id = :issue_id ORDER BY ce.id"
        ),
        {"issue_id": issue_id},
    ).mappings()
    return [HistoryEntry(**row) for row in rows]
