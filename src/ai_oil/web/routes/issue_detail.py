"""Issue detail page: in-place field editing and status transitions (UI-20, UI-26, DM-16)."""

from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from ai_oil import timeutil
from ai_oil.domain import issues, people
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.governance import audit
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _due_date, _local_time

router = APIRouter()

MAIN_FIELDS = ("summary", "description", "acceptance_criteria")
SIDE_FIELDS = ("priority", "issue_type", "assignee", "reporter", "due_date", "parent")
FIELD_LABELS = {
    "summary": "Summary",
    "description": "Description",
    "acceptance_criteria": "Acceptance criteria",
    "priority": "Priority",
    "issue_type": "Type",
    "assignee": "Assignee",
    "reporter": "Reporter",
    "due_date": "Due",
    "parent": "Parent",
}
DOMAIN_FIELDS = {
    "summary": "summary",
    "description": "description",
    "acceptance_criteria": "acceptance_criteria",
    "priority": "priority",
    "issue_type": "issue_type",
    "assignee": "assignee_id",
    "reporter": "reporter_id",
    "due_date": "due_date",
    "parent": "parent",
}  # UI-26: the field name in the URL to the name `issues.update_issue` takes


@dataclass(frozen=True)
class DetailValues:
    """The display values of an issue that are not plain attributes of it."""

    assignee: str | None
    reporter: str
    due: str | None
    original_due: str | None
    overdue: bool
    created: str
    updated: str
    resolved: str | None


def _values(conn: sqlalchemy.Connection, issue: issues.Issue) -> DetailValues:
    names = {person.id: person.display_name for person in people.list_persons(conn, include_merged=True)}
    due = issue.revised_due_date
    original = issue.original_due_date
    return DetailValues(
        assignee=names.get(issue.assignee_id) if issue.assignee_id is not None else None,
        reporter=names[issue.reporter_id],
        due=due.isoformat() if due else None,
        original_due=original.isoformat() if original and original != due else None,
        overdue=due is not None and due < timeutil.today() and issue.status_category != "Done",
        created=_local_time(issue.created_at),
        updated=_local_time(issue.updated_at),
        resolved=_local_time(issue.resolved_at) if issue.resolved_at else None,
    )


def _context(
    conn: sqlalchemy.Connection, issue: issues.Issue, *, reason_for: str | None = None
) -> dict[str, object]:
    return {
        "issue": issue,
        "values": _values(conn, issue),
        "transitions": issues.TRANSITIONS,
        "reason_for": reason_for,
        "field_labels": FIELD_LABELS,
        "main_fields": MAIN_FIELDS,
        "side_fields": SIDE_FIELDS,
    }


def _check_field(field: str) -> None:
    if field not in DOMAIN_FIELDS:
        raise NotFoundError(f"unknown issue field {field!r}")


def _side_response(
    request: Request, conn: sqlalchemy.Connection, issue: issues.Issue, *, reason_for: str | None = None
) -> Response:
    return render(request, "issue/_side.html", _context(conn, issue, reason_for=reason_for))


def _person_value(field: str, value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValidationError(f"{field} must be a person id")
    return int(value)


def _parsed(field: str, value: str) -> object:
    if field in ("assignee", "reporter"):
        return _person_value(field, value)
    if field == "due_date":
        return _due_date(value)
    if field == "parent":
        return value.strip() or None
    return value


@router.get("/issues/{key}")
def issue_detail(request: Request, key: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    issue = issues.get_issue(conn, key)
    return render(request, "issue/detail.html", {**_context(conn, issue), "nav_active": "issues"})


@router.get("/issues/{key}/side")
def side_panel(request: Request, key: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    """The side panel as it is stored, e.g. after cancelling the reason form."""
    return _side_response(request, conn, issues.get_issue(conn, key))


@router.get("/issues/{key}/view/{field}")
def field_view(
    request: Request, key: str, field: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]
):
    """The display fragment of one field, which Cancel swaps back in (UI-26)."""
    _check_field(field)
    return render(
        request, "issue/_view.html", {**_context(conn, issues.get_issue(conn, key)), "field": field}
    )


@router.get("/issues/{key}/edit/{field}")
def field_edit(
    request: Request, key: str, field: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]
):
    """The editor fragment of one field (UI-26)."""
    _check_field(field)
    context = {
        **_context(conn, issues.get_issue(conn, key)),
        "field": field,
        "persons": people.list_persons(conn),
        "priorities": issues.PRIORITIES,
        "issue_types": issues.ISSUE_TYPES,
    }
    return render(request, "issue/_editor.html", context)


@router.post("/issues/{key}/field/{field}")
def field_save(
    request: Request,
    key: str,
    field: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    value: Annotated[str, Form()] = "",
) -> Response:
    """Save one field as one change set and one audit entry (UI-26).

    The main column gets the field's display back; the side panel is re-rendered as a whole so that
    dependent values stay consistent.
    """
    _check_field(field)
    me = people.get_me(conn)
    before = issues.get_issue(conn, key)
    changes = {DOMAIN_FIELDS[field]: _parsed(field, value)}
    after = issues.update_issue(conn, key, changes, actor_id=me.id)
    if after != before:
        detail: dict[str, object] = {field: value.strip() or None, "via": "web"}
        audit.log(conn, "issue.edit", "B", actor_type="user", target=before.key, detail=detail)
    if field in MAIN_FIELDS:
        return render(request, "issue/_view.html", {**_context(conn, after), "field": field})
    return _side_response(request, conn, after)


@router.post("/issues/{key}/transition")
def transition(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    value: Annotated[str, Form()] = "",
    reason: Annotated[str | None, Form()] = None,
) -> Response:
    """Move the issue along the workflow; Blocked and Cancelled ask for a reason (DM-9, DM-10, DM-16)."""
    me = people.get_me(conn)
    before = issues.get_issue(conn, key)
    reason_text = (reason or "").strip()
    needs_reason = value in ("Blocked", "Cancelled") and value in issues.TRANSITIONS[before.status]
    if needs_reason and not reason_text:
        return _side_response(request, conn, before, reason_for=value)
    after = issues.transition_issue(conn, key, value, actor_id=me.id, reason=reason_text or None)
    detail: dict[str, object] = {"status": value.strip() or None, "via": "web"}
    if reason_text:
        detail["reason"] = reason_text
    if after != before:
        audit.log(conn, "issue.edit", "B", actor_type="user", target=before.key, detail=detail)
    return _side_response(request, conn, after)
