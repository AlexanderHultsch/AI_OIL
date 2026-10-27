"""Saved filters and the built-in filters (SRCH-8)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.search import compiler

COLUMNS: dict[str, str] = {
    "key": "Key",
    "type": "Type",
    "summary": "Summary",
    "status": "Status",
    "priority": "Priority",
    "assignee": "Assignee",
    "reporter": "Reporter",
    "due": "Due",
    "updated": "Updated",
    "created": "Created",
    "labels": "Labels",
    "sources": "Sources",
    "source_changed": "Source changed",
}
DEFAULT_COLUMNS = (
    "key",
    "type",
    "summary",
    "status",
    "priority",
    "assignee",
    "due",
    "updated",
    "sources",
    "source_changed",
)  # UI-10

BUILT_INS: dict[str, str] = {
    "All open issues": "statusCategory != Done ORDER BY updated DESC",
    "My open issues": "assignee = me() AND statusCategory != Done ORDER BY priority, due",
    "Overdue": "due < startOfDay() AND statusCategory != Done ORDER BY due",
    "Blocked": "status = Blocked ORDER BY updated DESC",
    "Updated by sources in the last 24 h": "sourceUpdated >= -1d ORDER BY updated DESC",
    "Due this week": "due >= startOfWeek() AND due <= endOfWeek() AND statusCategory != Done ORDER BY due",
}

_SELECT = "SELECT id, name, query, columns_json, is_favourite FROM saved_filter"


@dataclass(frozen=True)
class SavedFilter:
    id: int
    name: str
    query: str
    columns: tuple[str, ...]
    is_favourite: bool
    built_in: bool


def _filter(row: sqlalchemy.RowMapping) -> SavedFilter:
    return SavedFilter(
        id=row["id"],
        name=row["name"],
        query=row["query"],
        columns=tuple(json.loads(row["columns_json"])),
        is_favourite=bool(row["is_favourite"]),
        built_in=row["name"] in BUILT_INS,
    )


def ensure_built_ins(conn: sqlalchemy.Connection) -> None:
    """Insert every built-in filter that is missing, by name; existing rows are left untouched."""
    existing = set(conn.execute(text("SELECT name FROM saved_filter")).scalars())
    for name, query in BUILT_INS.items():
        if name not in existing:
            conn.execute(
                text(
                    "INSERT INTO saved_filter (name, query, columns_json, is_favourite)"
                    " VALUES (:name, :query, :columns, 1)"
                ),
                {"name": name, "query": query, "columns": json.dumps(list(DEFAULT_COLUMNS))},
            )


def list_filters(conn: sqlalchemy.Connection) -> list[SavedFilter]:
    """The built-in filters in their fixed order, then the others by name (case-insensitive)."""
    ensure_built_ins(conn)
    found = [_filter(row) for row in conn.execute(text(_SELECT)).mappings()]
    order = {name: position for position, name in enumerate(BUILT_INS)}
    return sorted(
        found,
        key=lambda f: (0, order[f.name], "") if f.built_in else (1, 0, f.name.lower()),
    )


def get_filter(conn: sqlalchemy.Connection, ref: int | str) -> SavedFilter:
    """Look a filter up by id (an int or an all-digit string) or by exact name."""
    if isinstance(ref, str) and ref.strip().isdigit():
        ref = int(ref.strip())
    if isinstance(ref, int):
        row = conn.execute(text(f"{_SELECT} WHERE id = :id"), {"id": ref}).mappings().first()
    else:
        row = conn.execute(text(f"{_SELECT} WHERE name = :name"), {"name": ref}).mappings().first()
    if row is None:
        raise NotFoundError(f"saved filter {ref!r} not found")
    return _filter(row)


def _name(conn: sqlalchemy.Connection, value: str, *, own_id: int | None = None) -> str:
    name = value.strip()
    if not name:
        raise ValidationError("filter name must not be blank")
    row = conn.execute(text("SELECT id FROM saved_filter WHERE name = :name"), {"name": name}).first()
    if row is not None and row[0] != own_id:
        raise ConflictError(f"saved filter {name!r} already exists")
    return name


def _query(conn: sqlalchemy.Connection, value: str) -> str:
    compiler.compile_query(conn, value)  # raises OqlSyntaxError with the position
    return value.strip()


def _columns(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_COLUMNS
    unknown = [column for column in value if column not in COLUMNS]
    if unknown:
        raise ValidationError(f"unknown column {unknown[0]!r} (use one of {', '.join(COLUMNS)})")
    return tuple(value)


def create_filter(
    conn: sqlalchemy.Connection, name: str, query: str, columns: Sequence[str] | None = None
) -> SavedFilter:
    """Save a filter; the query must be valid OQL (OqlSyntaxError otherwise)."""
    ensure_built_ins(conn)  # a built-in name must not be taken by a user filter first
    name = _name(conn, name)
    query = _query(conn, query)
    column_ids = _columns(columns)
    filter_id = conn.execute(
        text(
            "INSERT INTO saved_filter (name, query, columns_json) VALUES (:name, :query, :columns)"
            " RETURNING id"
        ),
        {"name": name, "query": query, "columns": json.dumps(list(column_ids))},
    ).scalar_one()
    return get_filter(conn, filter_id)


def update_filter(
    conn: sqlalchemy.Connection,
    ref: int | str,
    *,
    name: str | None = None,
    query: str | None = None,
    columns: Sequence[str] | None = None,
    is_favourite: bool | None = None,
) -> SavedFilter:
    """Change a filter. A built-in filter keeps its name and query; its columns and favourite flag change."""
    current = get_filter(conn, ref)
    changes: dict[str, object] = {}
    if name is not None:
        if current.built_in and name.strip() != current.name:
            raise ValidationError(f"the name of the built-in filter {current.name!r} cannot be changed")
        changes["name"] = _name(conn, name, own_id=current.id)
    if query is not None:
        if current.built_in and query.strip() != current.query:
            raise ValidationError(f"the query of the built-in filter {current.name!r} cannot be changed")
        changes["query"] = _query(conn, query)
    if columns is not None:
        changes["columns_json"] = json.dumps(list(_columns(columns)))
    if is_favourite is not None:
        changes["is_favourite"] = 1 if is_favourite else 0
    if changes:
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        conn.execute(
            text(f"UPDATE saved_filter SET {assignments} WHERE id = :id"), {**changes, "id": current.id}
        )
    return get_filter(conn, current.id)


def delete_filter(conn: sqlalchemy.Connection, ref: int | str) -> None:
    """Delete a saved filter; the built-in filters cannot be deleted."""
    current = get_filter(conn, ref)
    if current.built_in:
        raise ValidationError(f"the built-in filter {current.name!r} cannot be deleted")
    conn.execute(text("DELETE FROM saved_filter WHERE id = :id"), {"id": current.id})
