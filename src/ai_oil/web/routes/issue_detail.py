"""Issue detail page: field edits, labels, components, links, comments and history (UI-20, UI-23, UI-26)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import Response

from ai_oil import timeutil
from ai_oil.domain import comments, history_view, issue_links, issues, people, projects
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.governance import audit
from ai_oil.sources import activity
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import render
from ai_oil.web.routes.issue_sources import sources_context
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
RELATIONS = {
    "blocks": ("blocks", False),
    "is_blocked_by": ("blocks", True),
    "relates_to": ("relates_to", False),
    "duplicates": ("duplicates", False),
    "is_duplicated_by": ("duplicates", True),
}  # DM-14: the relation of the add form to the link type and whether the shown issue is the target
RELATION_LABELS = {
    "blocks": "blocks",
    "is_blocked_by": "is blocked by",
    "relates_to": "relates to",
    "duplicates": "duplicates",
    "is_duplicated_by": "is duplicated by",
}
LINK_DIRECTIONS = ("outward", "inward")
ACTIVITY_LIMIT = 200
NO_TIME = "—"
TIMELINE_TABS = {
    "all": (activity.KINDS, "No activity yet."),
    "source-updates": (("entry", "version"), "No source updates yet."),
}  # UI-23: the timeline tabs, the kinds each merges and the text when it is empty
ENTRY_LABELS = {
    "email_message": ("✉", "Email"),
    "jira_comment": ("◆", "Jira comment"),
    "jira_change": ("◆", "Jira change"),
    "confluence_comment": ("▤", "Confluence comment"),
    "teams_message": ("☰", "Teams"),
    "note_text": ("✎", "Note"),
}
KIND_LABELS = {"comment": ("✎", "Comment"), "history": ("↺", "History"), "version": ("⟳", "New version")}


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


@dataclass(frozen=True)
class CommentRow:
    id: int
    author: str
    created: str
    edited: bool
    body: str


@dataclass(frozen=True)
class TimelineRow:
    """One timeline item as the template shows it (UI-23, UI-24)."""

    item: activity.ActivityItem
    time: str
    icon: str
    label: str
    show_actor: bool
    compare: str | None


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


def _side_context(conn: sqlalchemy.Connection, issue: issues.Issue) -> dict[str, object]:
    """The options of the label and component forms (DM-3, DM-4)."""
    return {
        "all_labels": [label.name for label in projects.list_labels(conn)],
        "available_components": [
            component.name
            for component in projects.list_components(conn, issue.project_id)
            if component.name not in issue.components
        ],
    }


def _side_response(
    request: Request, conn: sqlalchemy.Connection, issue: issues.Issue, *, reason_for: str | None = None
) -> Response:
    context = {**_context(conn, issue, reason_for=reason_for), **_side_context(conn, issue)}
    return render(request, "issue/_side.html", context)


def _names(conn: sqlalchemy.Connection) -> dict[int, str]:
    return {person.id: person.display_name for person in people.list_persons(conn, include_merged=True)}


def _links_context(conn: sqlalchemy.Connection, issue: issues.Issue) -> dict[str, object]:
    return {
        "link_rows": issue_links.list_issue_links(conn, issue.key),
        "relations": list(RELATION_LABELS.items()),
    }


def _comments_context(conn: sqlalchemy.Connection, issue: issues.Issue) -> dict[str, object]:
    names = _names(conn)
    rows = [
        CommentRow(
            id=comment.id,
            author=names.get(comment.author_id, "Unknown"),
            created=_local_time(comment.created_at),
            edited=comment.edited_at is not None,
            body=comment.body,
        )
        for comment in comments.list_comments(conn, issue.key)
    ]
    return {"issue": issue, "comments": rows}


def _timeline_row(item: activity.ActivityItem) -> TimelineRow:
    icon, label = (
        ENTRY_LABELS.get(item.meta["entry_type"], ("•", "Source"))
        if item.kind == "entry"
        else KIND_LABELS[item.kind]
    )
    try:
        time = _local_time(item.at)
    except ValueError:
        time = item.at or NO_TIME
    compare = None
    if item.kind == "version":
        compare = f"{item.link}/diff?a={item.meta['previous_version_no']}&b={item.meta['version_no']}"
    return TimelineRow(
        item=item,
        time=time,
        icon=icon,
        label=label,
        show_actor=item.actor is not None and item.actor not in item.title,
        compare=compare,
    )


def _timeline_context(conn: sqlalchemy.Connection, issue: issues.Issue, tab: str) -> dict[str, object]:
    """The merged timeline of one tab, newest first; one extra item shows whether it was cut (UI-23)."""
    kinds, empty_text = TIMELINE_TABS[tab]
    found = activity.issue_activity(conn, issue.key, kinds=kinds, limit=ACTIVITY_LIMIT + 1)
    return {
        "issue": issue,
        "rows": [_timeline_row(item) for item in found[:ACTIVITY_LIMIT]],
        "cut_off": len(found) > ACTIVITY_LIMIT,
        "limit": ACTIVITY_LIMIT,
        "empty_text": empty_text,
    }


def _history_context(conn: sqlalchemy.Connection, issue: issues.Issue) -> dict[str, object]:
    """The history of the issue as change sets, newest first (DM-30, DM-32, DM-33)."""
    groups = history_view.change_sets(conn, issue.id, people.get_me(conn), _names(conn))
    return {"issue": issue, "groups": groups}


def _audit_web(conn: sqlalchemy.Connection, action: str, key: str, detail: dict[str, object]) -> None:
    audit.log(conn, action, "B", actor_type="user", target=key, detail={**detail, "via": "web"})


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
def issue_detail(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    link_error: Annotated[str | None, Query()] = None,
):
    issue = issues.get_issue(conn, key)
    context = {
        "link_error": link_error,
        **_context(conn, issue),
        **_side_context(conn, issue),
        **_links_context(conn, issue),
        **sources_context(conn, issue),
        **_timeline_context(conn, issue, "all"),
        "nav_active": "issues",
    }
    return render(request, "issue/detail.html", context)


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


def _change_membership(
    request: Request,
    conn: sqlalchemy.Connection,
    key: str,
    name: str,
    change: Callable[..., issues.Issue],
    option: str,
) -> Response:
    """Add or remove one label or component; one change set, one audit entry (UI-20, DM-3, DM-4)."""
    me = people.get_me(conn)
    before = issues.get_issue(conn, key)
    after = change(conn, key, name, actor_id=me.id)
    if after != before:
        _audit_web(conn, "issue.edit", before.key, {option: [name.strip()]})
    return _side_response(request, conn, after)


@router.post("/issues/{key}/labels")
def label_add(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
) -> Response:
    return _change_membership(request, conn, key, name, issues.add_label, "add_label")


@router.post("/issues/{key}/labels/remove")
def label_remove(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
) -> Response:
    return _change_membership(request, conn, key, name, issues.remove_label, "remove_label")


@router.post("/issues/{key}/components")
def component_add(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
) -> Response:
    return _change_membership(request, conn, key, name, issues.add_component, "add_component")


@router.post("/issues/{key}/components/remove")
def component_remove(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
) -> Response:
    return _change_membership(request, conn, key, name, issues.remove_component, "remove_component")


def _links_response(request: Request, conn: sqlalchemy.Connection, issue: issues.Issue) -> Response:
    return render(request, "issue/_links.html", {"issue": issue, **_links_context(conn, issue)})


@router.post("/issues/{key}/links")
def link_add(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    relation: Annotated[str, Form()] = "",
    other_key: Annotated[str, Form()] = "",
) -> Response:
    """Link the issue to another one; the "is ... by" relations swap the two ends (DM-14)."""
    if relation not in RELATIONS:
        raise ValidationError(f"relation must be one of {', '.join(RELATION_LABELS.values())}")
    other = other_key.strip().upper()
    if not other:
        raise ValidationError("the other issue key must not be blank")
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    link_type, swapped = RELATIONS[relation]
    from_key, to_key = (other, issue.key) if swapped else (issue.key, other)
    link_id = issue_links.link_issues(conn, from_key, link_type, to_key, actor_id=me.id)
    detail = {"from": from_key, "type": link_type, "to": to_key, "link_id": link_id}
    _audit_web(conn, "issue_link.add", from_key, detail)
    return _links_response(request, conn, issue)


@router.post("/issues/{key}/links/remove")
def link_remove(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    link_type: Annotated[str, Form()] = "",
    other_key: Annotated[str, Form()] = "",
    direction: Annotated[str, Form()] = "",
) -> Response:
    """Remove one link as seen from this issue (DM-14)."""
    if direction not in LINK_DIRECTIONS:
        raise ValidationError(f"direction must be one of {', '.join(LINK_DIRECTIONS)}")
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    other = other_key.strip().upper()
    from_key, to_key = (issue.key, other) if direction == "outward" else (other, issue.key)
    issue_links.unlink_issues(conn, from_key, link_type, to_key, actor_id=me.id)
    _audit_web(conn, "issue_link.remove", from_key, {"from": from_key, "type": link_type, "to": to_key})
    return _links_response(request, conn, issue)


def _comments_response(request: Request, conn: sqlalchemy.Connection, issue: issues.Issue) -> Response:
    return render(request, "issue/_comments.html", _comments_context(conn, issue))


def _comment_of(conn: sqlalchemy.Connection, issue: issues.Issue, comment_id: int) -> comments.Comment:
    comment = comments.get_comment(conn, comment_id)
    if comment.issue_id != issue.id:
        raise NotFoundError(f"comment {comment_id} not found on {issue.key}")
    return comment


@router.get("/issues/{key}/activity/{tab}")
def activity_tab(
    request: Request, key: str, tab: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]
) -> Response:
    """One tab of the activity area as a fragment (UI-23)."""
    issue = issues.get_issue(conn, key)
    if tab in TIMELINE_TABS:
        return render(request, "issue/_timeline.html", _timeline_context(conn, issue, tab))
    if tab == "comments":
        return _comments_response(request, conn, issue)
    if tab == "history":
        return render(request, "issue/_history.html", _history_context(conn, issue))
    raise NotFoundError(f"unknown activity tab {tab!r}")


@router.post("/issues/{key}/comments")
def comment_add(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    body: Annotated[str, Form()] = "",
) -> Response:
    me = people.get_me(conn)
    comment = comments.add_comment(conn, key, body, author_id=me.id)
    _audit_web(conn, "comment.add", comment.issue_key, {"comment_id": comment.id, "text": comment.body})
    return _comments_response(request, conn, issues.get_issue(conn, key))


@router.get("/issues/{key}/comments/{comment_id}/edit")
def comment_editor(
    request: Request, key: str, comment_id: int, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]
) -> Response:
    """The editor of one comment, which replaces the comment in the list (DM-19)."""
    issue = issues.get_issue(conn, key)
    comment = _comment_of(conn, issue, comment_id)
    if comment.deleted_at is not None:
        raise ValidationError(f"comment {comment_id} is deleted and cannot be edited (DM-19)")
    return render(request, "issue/_comment_editor.html", {"issue": issue, "comment": comment})


@router.post("/issues/{key}/comments/{comment_id}/edit")
def comment_edit(
    request: Request,
    key: str,
    comment_id: int,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    body: Annotated[str, Form()] = "",
) -> Response:
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    before = _comment_of(conn, issue, comment_id)
    after = comments.edit_comment(conn, comment_id, body, actor_id=me.id)
    if after != before:
        _audit_web(conn, "comment.edit", issue.key, {"comment_id": after.id, "text": after.body})
    return _comments_response(request, conn, issue)


@router.post("/issues/{key}/comments/{comment_id}/delete")
def comment_delete(
    request: Request,
    key: str,
    comment_id: int,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
) -> Response:
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    comment = _comment_of(conn, issue, comment_id)
    if comment.deleted_at is None:
        comments.delete_comment(conn, comment_id, actor_id=me.id)
        _audit_web(conn, "comment.delete", issue.key, {"comment_id": comment.id})
    return _comments_response(request, conn, issue)
