"""Issues: create, read, edit, status workflow, hierarchy, labels and components (DM-5 .. DM-18)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy
from sqlalchemy import bindparam, text

from ai_oil.domain import history, people, projects
from ai_oil.errors import NotFoundError, TransitionError, ValidationError
from ai_oil.timeutil import utcnow_iso

ISSUE_TYPES = ("Epic", "Task", "Sub-task")
STATUSES = ("New", "Ready to Proceed", "In Progress", "Blocked", "Done", "Cancelled")
STATUS_CATEGORY = {
    "New": "To Do",
    "Ready to Proceed": "To Do",
    "In Progress": "In Progress",
    "Blocked": "In Progress",
    "Done": "Done",
    "Cancelled": "Done",
}
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "New": ("Ready to Proceed", "In Progress", "Blocked", "Done", "Cancelled"),
    "Ready to Proceed": ("In Progress", "Blocked", "Done", "Cancelled"),
    "In Progress": ("Blocked", "Done", "Cancelled"),
    "Blocked": ("In Progress", "Ready to Proceed", "Done", "Cancelled"),
    "Done": ("In Progress",),
    "Cancelled": ("In Progress",),
}
PRIORITIES = ("P1", "P2", "P3", "P4", "P5")
ORIGIN_TYPES = ("manual", "email", "jira", "confluence", "sharepoint", "teams", "meeting")
EDITABLE_FIELDS = (
    "summary",
    "description",
    "acceptance_criteria",
    "issue_type",
    "parent",
    "priority",
    "assignee_id",
    "reporter_id",
    "due_date",
    "snoozed_until",
)

_SUMMARY_MAX = 120
_ID_CHUNK = 500
_CHILD_TYPE = {"Epic": "Task", "Task": "Sub-task"}

_SELECT = (
    "SELECT i.id, i.project_id, p.key AS project_key, i.number, i.key, i.issue_type, i.parent_id,"
    " par.key AS parent_key, i.summary, i.description, i.acceptance_criteria, i.status,"
    " i.blocked_reason, i.cancel_reason, i.priority, i.assignee_id, i.reporter_id,"
    " i.original_due_date, i.revised_due_date, i.origin_type, i.snoozed_until,"
    " i.created_at, i.updated_at, i.resolved_at"
    " FROM issue i JOIN project p ON p.id = i.project_id LEFT JOIN issue par ON par.id = i.parent_id"
)


@dataclass(frozen=True)
class Issue:
    id: int
    project_id: int
    project_key: str
    number: int
    key: str
    issue_type: str
    parent_id: int | None
    parent_key: str | None
    summary: str
    description: str
    acceptance_criteria: str | None
    status: str
    status_category: str
    blocked_reason: str | None
    cancel_reason: str | None
    priority: str
    assignee_id: int | None
    reporter_id: int
    original_due_date: date | None
    revised_due_date: date | None
    origin_type: str
    snoozed_until: date | None
    created_at: str
    updated_at: str
    resolved_at: str | None
    labels: tuple[str, ...]
    components: tuple[str, ...]


def _opt_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _issue(conn: sqlalchemy.Connection, row: sqlalchemy.RowMapping) -> Issue:
    labels = conn.execute(
        text("SELECT l.name FROM issue_label il JOIN label l ON l.id = il.label_id WHERE il.issue_id = :id"),
        {"id": row["id"]},
    ).scalars()
    components = conn.execute(
        text(
            "SELECT c.name FROM issue_component ic JOIN component c ON c.id = ic.component_id"
            " WHERE ic.issue_id = :id"
        ),
        {"id": row["id"]},
    ).scalars()
    return _build_issue(row, list(labels), list(components))


def _build_issue(row: sqlalchemy.RowMapping, labels: Sequence[str], components: Sequence[str]) -> Issue:
    return Issue(
        id=row["id"],
        project_id=row["project_id"],
        project_key=row["project_key"],
        number=row["number"],
        key=row["key"],
        issue_type=row["issue_type"],
        parent_id=row["parent_id"],
        parent_key=row["parent_key"],
        summary=row["summary"],
        description=row["description"],
        acceptance_criteria=row["acceptance_criteria"],
        status=row["status"],
        status_category=STATUS_CATEGORY[row["status"]],
        blocked_reason=row["blocked_reason"],
        cancel_reason=row["cancel_reason"],
        priority=row["priority"],
        assignee_id=row["assignee_id"],
        reporter_id=row["reporter_id"],
        original_due_date=_opt_date(row["original_due_date"]),
        revised_due_date=_opt_date(row["revised_due_date"]),
        origin_type=row["origin_type"],
        snoozed_until=_opt_date(row["snoozed_until"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
        labels=tuple(sorted(labels, key=lambda name: (name.lower(), name))),
        components=tuple(sorted(components)),
    )


def get_issue(conn: sqlalchemy.Connection, key: str) -> Issue:
    """Look an issue up by key, case-insensitively."""
    key = key.strip().upper()
    row = conn.execute(text(f"{_SELECT} WHERE i.key = :key"), {"key": key}).mappings().first()
    if row is None:
        raise NotFoundError(f"issue {key} not found")
    return _issue(conn, row)


def get_issue_by_id(conn: sqlalchemy.Connection, issue_id: int) -> Issue:
    row = conn.execute(text(f"{_SELECT} WHERE i.id = :id"), {"id": issue_id}).mappings().first()
    if row is None:
        raise NotFoundError(f"issue {issue_id} not found")
    return _issue(conn, row)


def get_issues_by_ids(conn: sqlalchemy.Connection, issue_ids: Sequence[int]) -> list[Issue]:
    """The issues with these ids in the given order, loaded in a constant number of queries per 500 ids."""
    rows: dict[int, sqlalchemy.RowMapping] = {}
    labels: dict[int, list[str]] = {}
    components: dict[int, list[str]] = {}
    for start in range(0, len(issue_ids), _ID_CHUNK):
        chunk = list(issue_ids[start : start + _ID_CHUNK])
        found = conn.execute(
            text(f"{_SELECT} WHERE i.id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk},
        ).mappings()
        rows.update({row["id"]: row for row in found})
        for table, target in (("label", labels), ("component", components)):
            names = conn.execute(
                text(
                    f"SELECT x.issue_id, t.name FROM issue_{table} x JOIN {table} t ON t.id = x.{table}_id"
                    " WHERE x.issue_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": chunk},
            )
            for issue_id, name in names:
                target.setdefault(issue_id, []).append(name)
    missing = [issue_id for issue_id in issue_ids if issue_id not in rows]
    if missing:
        raise NotFoundError(f"issue {missing[0]} not found")
    return [_build_issue(rows[i], labels.get(i, []), components.get(i, [])) for i in issue_ids]


def list_children(conn: sqlalchemy.Connection, key: str) -> list[Issue]:
    """The direct children of an issue, ordered by number."""
    parent = get_issue(conn, key)
    rows = conn.execute(
        text(f"{_SELECT} WHERE i.parent_id = :parent_id ORDER BY i.number"), {"parent_id": parent.id}
    ).mappings()
    return [_issue(conn, row) for row in rows.all()]


def _summary(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("summary must be text")
    value = value.strip()
    if not 1 <= len(value) <= _SUMMARY_MAX:
        raise ValidationError(f"summary must be 1-{_SUMMARY_MAX} characters (DM-7)")
    return value


def _description(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("description must not be blank (DM-7)")
    return value.strip()


def _acceptance_criteria(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("acceptance_criteria must be text")
    return value.strip() or None


def _choice(field: str, value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _person_id(conn: sqlalchemy.Connection, field: str, value: object, *, required: bool) -> int | None:
    if value is None:
        if required:
            raise ValidationError(f"{field} must not be empty")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be a person id")
    return people.surviving_person(conn, value).id


def _date_value(field: str, value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValidationError(f"{field} must be a date")
    return value


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _check_hierarchy(issue_id: int | None, project_id: int, issue_type: str, parent: Issue | None) -> None:
    """Epic -> Task -> Sub-task, within one project (DM-15)."""
    if parent is not None:
        if issue_id is not None and parent.id == issue_id:
            raise ValidationError("an issue cannot be its own parent")
        if parent.project_id != project_id:
            raise ValidationError(f"parent {parent.key} belongs to another project (DM-15)")
    if issue_type == "Epic":
        if parent is not None:
            raise ValidationError("an Epic cannot have a parent (DM-15)")
    elif issue_type == "Task":
        if parent is not None and parent.issue_type != "Epic":
            raise ValidationError(
                f"the parent of a Task must be an Epic, {parent.key} is a {parent.issue_type}"
            )
    elif parent is None or parent.issue_type != "Task":
        raise ValidationError("a Sub-task requires a Task as parent (DM-15)")


def _check_children(conn: sqlalchemy.Connection, issue: Issue, new_type: str) -> None:
    """A type change must keep the existing children valid (DM-15)."""
    allowed = _CHILD_TYPE.get(new_type)
    for child in list_children(conn, issue.key):
        if child.issue_type != allowed:
            raise ValidationError(
                f"cannot change {issue.key} to {new_type}: child {child.key} is a {child.issue_type} (DM-15)"
            )


def _touch(conn: sqlalchemy.Connection, issue_id: int, now: str) -> None:
    conn.execute(text("UPDATE issue SET updated_at = :now WHERE id = :id"), {"now": now, "id": issue_id})


def create_issue(
    conn: sqlalchemy.Connection,
    project_key: str,
    summary: str,
    description: str,
    *,
    actor_id: int,
    issue_type: str = "Task",
    parent_key: str | None = None,
    priority: str = "P4",
    assignee_id: int | None = None,
    reporter_id: int | None = None,
    due_date: date | None = None,
    acceptance_criteria: str | None = None,
    labels: Sequence[str] = (),
    components: Sequence[str] = (),
    origin_type: str = "manual",
) -> Issue:
    """Create an issue in status New with one history event (DM-5, DM-7, DM-11, DM-12, DM-15, DM-30)."""
    project = projects.get_project(conn, project_key)
    if project.archived:
        raise ValidationError(f"project {project.key} is archived — unarchive it to add issues")
    summary = _summary(summary)
    description = _description(description)
    issue_type = _choice("issue_type", issue_type, ISSUE_TYPES)
    priority = _choice("priority", priority, PRIORITIES)
    origin_type = _choice("origin_type", origin_type, ORIGIN_TYPES)
    acceptance_criteria = _acceptance_criteria(acceptance_criteria)
    assignee = _person_id(conn, "assignee_id", assignee_id, required=False)
    reporter = _person_id(
        conn, "reporter_id", actor_id if reporter_id is None else reporter_id, required=True
    )
    due = _date_value("due_date", due_date)
    parent = get_issue(conn, parent_key) if parent_key is not None else None
    _check_hierarchy(None, project.id, issue_type, parent)
    component_ids = {projects.get_component(conn, project.id, name).id for name in components}
    label_ids = {projects.get_or_create_label(conn, name).id for name in labels}

    number = projects.allocate_issue_number(conn, project.id)
    key = f"{project.key}-{number}"
    now = utcnow_iso()
    issue_id = conn.execute(
        text(
            "INSERT INTO issue (project_id, number, key, issue_type, parent_id, summary, description,"
            " acceptance_criteria, status, priority, assignee_id, reporter_id, original_due_date,"
            " revised_due_date, origin_type, created_at, updated_at)"
            " VALUES (:project_id, :number, :key, :issue_type, :parent_id, :summary, :description,"
            " :acceptance_criteria, 'New', :priority, :assignee_id, :reporter_id, :due, :due,"
            " :origin_type, :now, :now) RETURNING id"
        ),
        {
            "project_id": project.id,
            "number": number,
            "key": key,
            "issue_type": issue_type,
            "parent_id": parent.id if parent is not None else None,
            "summary": summary,
            "description": description,
            "acceptance_criteria": acceptance_criteria,
            "priority": priority,
            "assignee_id": assignee,
            "reporter_id": reporter,
            "due": _iso(due),
            "origin_type": origin_type,
            "now": now,
        },
    ).scalar_one()
    for label_id in label_ids:
        conn.execute(
            text("INSERT INTO issue_label (issue_id, label_id) VALUES (:issue_id, :label_id)"),
            {"issue_id": issue_id, "label_id": label_id},
        )
    for component_id in component_ids:
        conn.execute(
            text("INSERT INTO issue_component (issue_id, component_id) VALUES (:issue_id, :component_id)"),
            {"issue_id": issue_id, "component_id": component_id},
        )
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, issue_id, "issue", "create", None, key)
    return get_issue_by_id(conn, issue_id)


def update_issue(
    conn: sqlalchemy.Connection,
    key: str,
    changes: Mapping[str, object],
    *,
    actor_id: int | None,
    actor_type: str = "user",
    proposal_id: int | None = None,
    sync_run_id: int | None = None,
) -> Issue:
    """Change the given fields; only differing values are written and recorded (DM-7, DM-12, DM-15, DM-30)."""
    for name in changes:
        if name not in EDITABLE_FIELDS:
            hint = " — use transition_issue" if name == "status" else ""
            raise ValidationError(f"field {name!r} cannot be edited{hint}")
    issue = get_issue(conn, key)

    columns: dict[str, object] = {}
    events: list[tuple[str, object, object]] = []

    def set_field(field: str, column: str, old: object, new: object) -> None:
        if new != old:
            columns[column] = new
            events.append((field, old, new))

    if "summary" in changes:
        set_field("summary", "summary", issue.summary, _summary(changes["summary"]))
    if "description" in changes:
        set_field("description", "description", issue.description, _description(changes["description"]))
    if "acceptance_criteria" in changes:
        set_field(
            "acceptance_criteria",
            "acceptance_criteria",
            issue.acceptance_criteria,
            _acceptance_criteria(changes["acceptance_criteria"]),
        )
    if "priority" in changes:
        set_field(
            "priority", "priority", issue.priority, _choice("priority", changes["priority"], PRIORITIES)
        )
    if "assignee_id" in changes:
        assignee = _person_id(conn, "assignee_id", changes["assignee_id"], required=False)
        set_field("assignee", "assignee_id", issue.assignee_id, assignee)
    if "reporter_id" in changes:
        reporter = _person_id(conn, "reporter_id", changes["reporter_id"], required=True)
        set_field("reporter", "reporter_id", issue.reporter_id, reporter)
    if "snoozed_until" in changes:
        snoozed = _date_value("snoozed_until", changes["snoozed_until"])
        set_field("snoozed_until", "snoozed_until", _iso(issue.snoozed_until), _iso(snoozed))

    new_type = issue.issue_type
    if "issue_type" in changes:
        new_type = _choice("issue_type", changes["issue_type"], ISSUE_TYPES)
    parent: Issue | None
    if "parent" in changes:
        parent_key = changes["parent"]
        if parent_key is not None and not isinstance(parent_key, str):
            raise ValidationError("parent must be an issue key or None")
        parent = get_issue(conn, parent_key) if parent_key is not None else None
    else:
        parent = get_issue_by_id(conn, issue.parent_id) if issue.parent_id is not None else None
    new_parent_id = parent.id if parent is not None else None
    if new_type != issue.issue_type or new_parent_id != issue.parent_id:
        _check_hierarchy(issue.id, issue.project_id, new_type, parent)
        if new_type != issue.issue_type:
            _check_children(conn, issue, new_type)
    set_field("issue_type", "issue_type", issue.issue_type, new_type)
    if new_parent_id != issue.parent_id:
        columns["parent_id"] = new_parent_id
        events.append(("parent", issue.parent_key, parent.key if parent is not None else None))

    if "due_date" in changes:
        due = _date_value("due_date", changes["due_date"])
        if due is None:
            if issue.revised_due_date is not None:
                raise ValidationError("the due date cannot be removed once set (DM-12)")
        elif due != issue.revised_due_date:
            columns["revised_due_date"] = due.isoformat()
            events.append(("due_date", _iso(issue.revised_due_date), due.isoformat()))
            if issue.original_due_date is None:
                columns["original_due_date"] = due.isoformat()
                events.append(("original_due_date", None, due.isoformat()))

    if not events:
        return issue

    now = utcnow_iso()
    columns["updated_at"] = now
    assignments = ", ".join(f"{column} = :{column}" for column in columns)
    conn.execute(text(f"UPDATE issue SET {assignments} WHERE id = :id"), {**columns, "id": issue.id})
    change_set_id = history.start_change_set(
        conn, actor_type, actor_id=actor_id, proposal_id=proposal_id, sync_run_id=sync_run_id
    )
    for field, old, new in events:
        history.record(conn, change_set_id, issue.id, field, "set", old, new)
    return get_issue_by_id(conn, issue.id)


def transition_issue(
    conn: sqlalchemy.Connection,
    key: str,
    to_status: str,
    *,
    actor_id: int | None,
    reason: str | None = None,
    actor_type: str = "user",
    proposal_id: int | None = None,
) -> Issue:
    """Move an issue along the workflow (DM-8, DM-9, DM-10, DM-16, DM-17)."""
    to_status = _choice("status", to_status, STATUSES)
    issue = get_issue(conn, key)
    current = issue.status
    if to_status not in TRANSITIONS[current]:
        raise TransitionError(f"cannot move {issue.key} from {current} to {to_status}")
    reason = reason.strip() if reason else None
    if to_status in ("Blocked", "Cancelled") and not reason:
        raise ValidationError(f"moving to {to_status} requires a reason")

    now = utcnow_iso()
    reopening = current in ("Done", "Cancelled")
    blocked_reason = issue.blocked_reason
    cancel_reason = issue.cancel_reason
    resolved_at = issue.resolved_at
    if to_status == "Blocked":
        blocked_reason = reason
    elif current == "Blocked":
        blocked_reason = None
    if to_status == "Cancelled":
        cancel_reason = reason
    elif reopening:
        cancel_reason = None
    if to_status in ("Done", "Cancelled"):
        resolved_at = now
    elif reopening:
        resolved_at = None

    conn.execute(
        text(
            "UPDATE issue SET status = :status, blocked_reason = :blocked_reason,"
            " cancel_reason = :cancel_reason, resolved_at = :resolved_at, updated_at = :now WHERE id = :id"
        ),
        {
            "status": to_status,
            "blocked_reason": blocked_reason,
            "cancel_reason": cancel_reason,
            "resolved_at": resolved_at,
            "now": now,
            "id": issue.id,
        },
    )
    change_set_id = history.start_change_set(conn, actor_type, actor_id=actor_id, proposal_id=proposal_id)
    history.record(conn, change_set_id, issue.id, "status", "set", current, to_status)
    for field, old, new in (
        ("blocked_reason", issue.blocked_reason, blocked_reason),
        ("cancel_reason", issue.cancel_reason, cancel_reason),
        ("resolved_at", issue.resolved_at, resolved_at),
    ):
        if old != new:
            history.record(conn, change_set_id, issue.id, field, "set", old, new)
    return get_issue_by_id(conn, issue.id)


def _find_label(conn: sqlalchemy.Connection, name: str) -> projects.Label | None:
    row = conn.execute(text("SELECT id, name FROM label WHERE name = :name"), {"name": name.strip()}).first()
    return projects.Label(row.id, row.name) if row is not None else None


def add_label(conn: sqlalchemy.Connection, key: str, name: str, *, actor_id: int) -> Issue:
    """Attach a label; a label already on the issue is a no-op (DM-4, DM-30)."""
    issue = get_issue(conn, key)
    label = projects.get_or_create_label(conn, name)
    params = {"issue_id": issue.id, "label_id": label.id}
    if conn.execute(
        text("SELECT 1 FROM issue_label WHERE issue_id = :issue_id AND label_id = :label_id"), params
    ).first():
        return issue
    conn.execute(text("INSERT INTO issue_label (issue_id, label_id) VALUES (:issue_id, :label_id)"), params)
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, issue.id, "label", "add", None, label.name)
    _touch(conn, issue.id, utcnow_iso())
    return get_issue_by_id(conn, issue.id)


def remove_label(conn: sqlalchemy.Connection, key: str, name: str, *, actor_id: int) -> Issue:
    """Detach a label; one that is unknown or not on the issue is a no-op (DM-4, DM-30)."""
    issue = get_issue(conn, key)
    label = _find_label(conn, name)
    if label is None:
        return issue
    params = {"issue_id": issue.id, "label_id": label.id}
    removed = conn.execute(
        text("DELETE FROM issue_label WHERE issue_id = :issue_id AND label_id = :label_id"), params
    ).rowcount
    if not removed:
        return issue
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, issue.id, "label", "remove", label.name, None)
    _touch(conn, issue.id, utcnow_iso())
    return get_issue_by_id(conn, issue.id)


def add_component(conn: sqlalchemy.Connection, key: str, name: str, *, actor_id: int) -> Issue:
    """Attach a component of the issue's project; one already on the issue is a no-op (DM-3, DM-30)."""
    issue = get_issue(conn, key)
    component = projects.get_component(conn, issue.project_id, name)
    params = {"issue_id": issue.id, "component_id": component.id}
    if conn.execute(
        text("SELECT 1 FROM issue_component WHERE issue_id = :issue_id AND component_id = :component_id"),
        params,
    ).first():
        return issue
    conn.execute(
        text("INSERT INTO issue_component (issue_id, component_id) VALUES (:issue_id, :component_id)"), params
    )
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, issue.id, "component", "add", None, component.name)
    _touch(conn, issue.id, utcnow_iso())
    return get_issue_by_id(conn, issue.id)


def remove_component(conn: sqlalchemy.Connection, key: str, name: str, *, actor_id: int) -> Issue:
    """Detach a component of the issue's project; one not on the issue is a no-op (DM-3, DM-30)."""
    issue = get_issue(conn, key)
    component = projects.get_component(conn, issue.project_id, name)
    removed = conn.execute(
        text("DELETE FROM issue_component WHERE issue_id = :issue_id AND component_id = :component_id"),
        {"issue_id": issue.id, "component_id": component.id},
    ).rowcount
    if not removed:
        return issue
    change_set_id = history.start_change_set(conn, "user", actor_id=actor_id)
    history.record(conn, change_set_id, issue.id, "component", "remove", component.name, None)
    _touch(conn, issue.id, utcnow_iso())
    return get_issue_by_id(conn, issue.id)
