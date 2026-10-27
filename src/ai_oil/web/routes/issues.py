"""Issue navigator and saved filters, read-only (UI-4, UI-10, UI-14)."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import bindparam, text

from ai_oil import timeutil
from ai_oil.domain import issues
from ai_oil.search import oql, saved_filters
from ai_oil.search.compiler import search_issues
from ai_oil.search.saved_filters import COLUMNS, DEFAULT_COLUMNS, SavedFilter
from ai_oil.web.deps import db_read
from ai_oil.web.rendering import render

router = APIRouter()

MAX_ROWS = 500
DEFAULT_FILTER = "All open issues"

_ID_CHUNK = 500
_OQL_CHARACTERS = re.compile(r"[=~<>!]")
_OQL_WORDS = re.compile(r"\b(?:AND|OR|NOT|IN|IS|EMPTY|ORDER|BY)\b", re.IGNORECASE)


@dataclass(frozen=True)
class IssueRow:
    issue: issues.Issue
    assignee: str | None
    reporter: str | None
    due: str | None
    overdue: bool
    updated: str
    created: str
    sources: int
    source_changed: str | None
    source_errors: int


def _local_time(value: str) -> str:
    return timeutil.parse_iso(value).astimezone().strftime("%Y-%m-%d %H:%M")


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[start : start + _ID_CHUNK] for start in range(0, len(ids), _ID_CHUNK)]


def _person_names(conn: sqlalchemy.Connection, found: list[issues.Issue]) -> dict[int, str]:
    ids = sorted({i for issue in found for i in (issue.assignee_id, issue.reporter_id) if i is not None})
    names: dict[int, str] = {}
    statement = text("SELECT id, display_name FROM person WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    for chunk in _chunks(ids):
        names.update(conn.execute(statement, {"ids": chunk}).all())
    return names


def _source_statistics(
    conn: sqlalchemy.Connection, found: list[issues.Issue]
) -> dict[int, tuple[int, str | None, int]]:
    """Per issue id: active linked objects, newest change over them and their children, objects in error."""
    statement = text(
        "SELECT l.issue_id, COUNT(DISTINCT l.source_object_id), MAX(o.last_changed_at),"
        " COUNT(DISTINCT CASE WHEN o.fetch_status = 'error' THEN o.id END)"
        " FROM issue_source_link l"
        " JOIN source_object o ON (o.id = l.source_object_id OR o.parent_id = l.source_object_id)"
        " WHERE l.removed_at IS NULL AND l.issue_id IN :ids GROUP BY l.issue_id"
    ).bindparams(bindparam("ids", expanding=True))
    statistics: dict[int, tuple[int, str | None, int]] = {}
    for chunk in _chunks([issue.id for issue in found]):
        for issue_id, count, newest, errors in conn.execute(statement, {"ids": chunk}):
            statistics[issue_id] = (count, newest, errors)
    return statistics


def _rows(conn: sqlalchemy.Connection, found: list[issues.Issue]) -> list[IssueRow]:
    names = _person_names(conn, found)
    statistics = _source_statistics(conn, found)
    today = timeutil.today()
    rows = []
    for issue in found:
        count, newest, errors = statistics.get(issue.id, (0, None, 0))
        due: date | None = issue.revised_due_date
        rows.append(
            IssueRow(
                issue=issue,
                assignee=names.get(issue.assignee_id) if issue.assignee_id is not None else None,
                reporter=names.get(issue.reporter_id),
                due=due.isoformat() if due else None,
                overdue=due is not None and due < today and issue.status_category != "Done",
                updated=_local_time(issue.updated_at),
                created=_local_time(issue.created_at),
                sources=count,
                source_changed=_local_time(newest) if newest else None,
                source_errors=errors,
            )
        )
    return rows


def _looks_like_plain_text(text_: str) -> bool:
    return _OQL_CHARACTERS.search(text_) is None and _OQL_WORDS.search(text_) is None


def _text_query(plain: str) -> str:
    escaped = plain.replace("\\", "\\\\").replace('"', '\\"')
    return f'text ~ "{escaped}"'


def _columns(requested: str | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if requested:
        chosen = tuple(
            dict.fromkeys(c for c in (part.strip() for part in requested.split(",")) if c in COLUMNS)
        )
        if chosen:
            return chosen
    return tuple(c for c in fallback if c in COLUMNS) or DEFAULT_COLUMNS


def _run(conn: sqlalchemy.Connection, query: str) -> list[issues.Issue]:
    return search_issues(conn, query, limit=MAX_ROWS + 1)


@router.get("/issues")
def issue_list(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    q: Annotated[str | None, Query()] = None,
    filter_ref: Annotated[str | None, Query(alias="filter")] = None,
    columns: Annotated[str | None, Query()] = None,
):
    saved_filters.ensure_built_ins(conn)
    active: SavedFilter | None = None
    plain = (q or "").strip()
    found: list[issues.Issue] = []
    error: str | None = None
    if filter_ref and filter_ref.strip():
        active = saved_filters.get_filter(conn, filter_ref)
        query = active.query
    elif plain:
        query = plain
    else:
        active = saved_filters.get_filter(conn, DEFAULT_FILTER)
        query = active.query
    try:
        found = _run(conn, query)
    except oql.OqlSyntaxError as exc:
        if active is None and _looks_like_plain_text(plain):
            query = _text_query(plain)
            try:
                found = _run(conn, query)
            except oql.OqlSyntaxError as text_exc:
                error = oql.format_error(query, text_exc)
        else:
            error = oql.format_error(query, exc)
    shown = found[:MAX_ROWS]
    context = {
        "query": query,
        "active_filter": active,
        "columns": _columns(columns, active.columns if active else DEFAULT_COLUMNS),
        "column_labels": COLUMNS,
        "rows": _rows(conn, shown),
        "count": len(shown),
        "truncated": len(found) > MAX_ROWS,
        "overflow_note": f"Showing the first {MAX_ROWS} issues — refine the query.",
        "error": error,
        "nav_active": "issues",
    }
    return render(request, "issues/list.html", context)


@router.get("/filters")
def filter_list(request: Request, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    return render(
        request,
        "filters/list.html",
        {"filters": saved_filters.list_filters(conn), "nav_active": "filters"},
    )
