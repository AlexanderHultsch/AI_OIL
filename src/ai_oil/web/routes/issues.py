"""Issue navigator and saved filters: chips, sorting, inline edits, filter management (UI-10 .. UI-14)."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated
from urllib.parse import urlencode

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import bindparam, text
from starlette.responses import RedirectResponse, Response

from ai_oil import timeutil
from ai_oil.domain import issues, people, projects
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.governance import audit
from ai_oil.search import oql, saved_filters
from ai_oil.search.compiler import search_issues
from ai_oil.search.saved_filters import COLUMNS, DEFAULT_COLUMNS, SavedFilter
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import is_htmx, render

router = APIRouter()

MAX_ROWS = 500
DEFAULT_FILTER = "All open issues"

SORTABLE_COLUMNS = (
    "key",
    "type",
    "summary",
    "status",
    "priority",
    "assignee",
    "reporter",
    "due",
    "updated",
    "created",
)  # UI-13: the columns whose id is also an OQL ORDER BY field
INLINE_FIELDS = ("status", "priority", "assignee", "due")  # UI-12
ME_CHIP = "@me"
UNASSIGNED_CHIP = "@unassigned"

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
    return timeutil.local_display(value)


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


def _split_columns(values: Iterable[str] | None) -> list[str]:
    """Column ids from repeated and/or comma-separated form or query values."""
    parts: list[str] = []
    for value in values or ():
        parts.extend(part.strip() for part in value.split(",") if part.strip())
    return parts


def _sort_links(query: str, columns: Sequence[str]) -> dict[str, dict[str, str | None]]:
    """Per sortable shown column: the link that sorts by it and the arrow of the current sort (UI-13)."""
    try:
        parsed = oql.parse(query)
    except oql.OqlSyntaxError:
        return {}
    first = parsed.order_by[0] if parsed.order_by else None
    links: dict[str, dict[str, str | None]] = {}
    for column in columns:
        if column not in SORTABLE_COLUMNS:
            continue
        current = first.direction if first is not None and first.field.lower() == column else None
        direction = "desc" if current == "asc" else "asc"
        params = {"q": oql.to_text(oql.with_order(parsed, column, direction))}
        if tuple(columns) != DEFAULT_COLUMNS:
            params["columns"] = ",".join(columns)
        arrow = {"asc": "▲", "desc": "▼"}.get(current or "")
        links[column] = {"href": "/issues?" + urlencode(params), "arrow": arrow}
    return links


def _edit_context(conn: sqlalchemy.Connection, columns: Sequence[str]) -> dict[str, object]:
    """What `issues/_row.html` needs to render the inline controls (UI-12)."""
    return {
        "columns": columns,
        "persons": people.list_persons(conn),
        "transitions": issues.TRANSITIONS,
        "priorities": issues.PRIORITIES,
    }


@router.get("/issues")
def issue_list(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    q: Annotated[str | None, Query()] = None,
    filter_ref: Annotated[str | None, Query(alias="filter")] = None,
    columns: Annotated[list[str] | None, Query()] = None,
):
    saved_filters.ensure_built_ins(conn)
    requested = ",".join(_split_columns(columns)) or None
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
    shown_columns = _columns(requested, active.columns if active else DEFAULT_COLUMNS)
    context = {
        "query": query,
        "active_filter": active,
        **_edit_context(conn, shown_columns),
        "column_labels": COLUMNS,
        "all_columns": COLUMNS,
        "sort": _sort_links(query, shown_columns),
        "chips": {
            "projects": projects.list_projects(conn),
            "statuses": issues.STATUSES,
            "persons": people.list_persons(conn),
            "priorities": issues.PRIORITIES,
            "labels": projects.list_labels(conn),
        },
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


def _value(kind: str, text_: str) -> oql.Value:
    return oql.Value(kind, text_, 0)


def _membership(field: str, values: list[oql.Value]) -> oql.Clause:
    if len(values) == 1:
        return oql.Clause(field, "=", (values[0],), 0)
    return oql.Clause(field, "in", tuple(values), 0)


def compose_chip_query(
    project: str | None,
    statuses: Iterable[str],
    assignee: str | None,
    priorities: Iterable[str],
    label: str | None,
) -> str:
    """The OQL the quick-filter chips stand for (UI-11); no chip chosen gives the default filter."""
    clauses: list[oql.Clause] = []
    if project and project.strip():
        clauses.append(oql.Clause("project", "=", (_value("word", project.strip()),), 0))
    chosen = list(dict.fromkeys(s.strip() for s in statuses if s.strip()))
    if chosen:
        clauses.append(_membership("status", [_value("string", s) for s in chosen]))
    if assignee and assignee.strip():
        who = assignee.strip()
        if who == ME_CHIP:
            clauses.append(oql.Clause("assignee", "=", (_value("function", "me"),), 0))
        elif who == UNASSIGNED_CHIP:
            clauses.append(oql.Clause("assignee", "is empty", (), 0))
        else:  # a person id; anything else is taken as a name or e-mail address
            kind = "word" if who.isdigit() else "string"
            clauses.append(oql.Clause("assignee", "=", (_value(kind, who),), 0))
    ranked = list(dict.fromkeys(p.strip() for p in priorities if p.strip()))
    if ranked:
        clauses.append(_membership("priority", [_value("word", p) for p in ranked]))
    if label and label.strip():
        clauses.append(oql.Clause("label", "=", (_value("string", label.strip()),), 0))
    if not clauses:
        return saved_filters.BUILT_INS[DEFAULT_FILTER]
    where: oql.Expr = clauses[0] if len(clauses) == 1 else oql.And(tuple(clauses))
    return oql.to_text(oql.Query(where, (oql.OrderItem("updated", "desc", 0),)))


@router.get("/issues/build")
def build_query(
    project: Annotated[str | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    assignee: Annotated[str | None, Query()] = None,
    priority: Annotated[list[str] | None, Query()] = None,
    label: Annotated[str | None, Query()] = None,
) -> Response:
    """Turn the quick-filter chips into OQL and show it in the navigator (UI-11)."""
    query = compose_chip_query(project, status or (), assignee, priority or (), label)
    return RedirectResponse("/issues?" + urlencode({"q": query}), status_code=303)


def _assignee_id(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValidationError("assignee must be a person id")
    return int(value)


def _due_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"due must be a date (YYYY-MM-DD), got {value!r}") from None


def _row_response(
    request: Request,
    conn: sqlalchemy.Connection,
    issue: issues.Issue,
    columns: str | None,
    *,
    reason_for: str | None = None,
) -> Response:
    context = {
        **_edit_context(conn, _columns(columns, DEFAULT_COLUMNS)),
        "row": _rows(conn, [issue])[0],
        "reason_for": reason_for,
    }
    return render(request, "issues/_row.html", context)


@router.post("/issues/{key}/inline/{field}")
def inline_edit(
    request: Request,
    key: str,
    field: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    value: Annotated[str, Form()] = "",
    reason: Annotated[str | None, Form()] = None,
    columns: Annotated[str | None, Form()] = None,
) -> Response:
    """Change status, priority, assignee or due date from the table and return the new row (UI-12)."""
    if field not in INLINE_FIELDS:
        raise NotFoundError(f"unknown inline field {field!r}")
    me = people.get_me(conn)
    before = issues.get_issue(conn, key)
    reason_text = (reason or "").strip()
    detail: dict[str, object] = {field: value.strip() or None, "via": "web"}
    if field == "status":
        needs_reason = value in ("Blocked", "Cancelled") and value in issues.TRANSITIONS[before.status]
        if needs_reason and not reason_text:
            return _row_response(request, conn, before, columns, reason_for=value)
        after = issues.transition_issue(conn, key, value, actor_id=me.id, reason=reason_text or None)
        if reason_text:
            detail["reason"] = reason_text
    elif field == "priority":
        after = issues.update_issue(conn, key, {"priority": value}, actor_id=me.id)
    elif field == "assignee":
        after = issues.update_issue(conn, key, {"assignee_id": _assignee_id(value)}, actor_id=me.id)
    else:
        after = issues.update_issue(conn, key, {"due_date": _due_date(value)}, actor_id=me.id)
    if after != before:
        audit.log(conn, "issue.edit", "B", actor_type="user", target=before.key, detail=detail)
    return _row_response(request, conn, after, columns)


@router.post("/filters")
def filter_create(
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
    query: Annotated[str, Form()] = "",
    columns: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Save the current query as a filter (UI-13, SRCH-8)."""
    chosen = _split_columns(columns)
    created = saved_filters.create_filter(conn, name, query, chosen or None)
    audit.log(
        conn,
        "filter.create",
        "B",
        actor_type="user",
        target=str(created.id),
        detail={"name": created.name, "query": created.query, "columns": list(created.columns), "via": "web"},
    )
    return RedirectResponse(f"/issues?filter={created.id}", status_code=303)


@router.post("/filters/{filter_id}/delete")
def filter_delete(
    filter_id: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
) -> Response:
    current = saved_filters.get_filter(conn, filter_id)
    saved_filters.delete_filter(conn, current.id)
    audit.log(
        conn,
        "filter.delete",
        "B",
        actor_type="user",
        target=str(current.id),
        detail={"name": current.name, "via": "web"},
    )
    return RedirectResponse("/filters", status_code=303)


@router.post("/filters/{filter_id}/columns")
def filter_columns(
    request: Request,
    filter_id: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    columns: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Store the chosen column set in the filter (SRCH-8)."""
    chosen = _split_columns(columns)
    if not chosen:
        raise ValidationError("choose at least one column")
    current = saved_filters.get_filter(conn, filter_id)
    updated = saved_filters.update_filter(conn, current.id, columns=chosen)
    audit.log(
        conn,
        "filter.update",
        "B",
        actor_type="user",
        target=str(updated.id),
        detail={"name": updated.name, "columns": list(updated.columns), "via": "web"},
    )
    url = f"/issues?filter={updated.id}"
    if is_htmx(request):  # an XHR would swallow a 303, so htmx is sent on by header
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
