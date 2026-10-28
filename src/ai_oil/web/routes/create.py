"""Create-issue page: the form, the component options of a project and the creation itself (UI-27)."""

from typing import Annotated
from urllib.parse import quote

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from ai_oil.config import Config
from ai_oil.domain import issues, people, projects
from ai_oil.errors import AiOilError
from ai_oil.governance import audit
from ai_oil.sources import link_service
from ai_oil.web.deps import db_read, get_config, get_engine
from ai_oil.web.rendering import render
from ai_oil.web.routes.issue_sources import DEFAULT_ROLE, VIA
from ai_oil.web.routes.issues import _assignee_id, _due_date

router = APIRouter()

DEFAULT_TYPE = "Task"
DEFAULT_PRIORITY = "P4"


def _split(value: str) -> list[str]:
    """The non-empty, stripped parts of a comma-separated text."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _form_context(
    conn: sqlalchemy.Connection, values: dict[str, object], error: str | None = None
) -> dict[str, object]:
    """The context of `issues/new.html`; `values` are the entered (or default) field values."""
    open_projects = projects.list_projects(conn)
    selected = next((p for p in open_projects if p.key == values["project"]), None)
    if selected is None and open_projects:
        selected = open_projects[0]
    values = {**values, "project": selected.key if selected is not None else ""}
    return {
        "values": values,
        "error": error,
        "projects": open_projects,
        "components": projects.list_components(conn, selected.id) if selected is not None else [],
        "issue_types": issues.ISSUE_TYPES,
        "priorities": issues.PRIORITIES,
        "persons": people.list_persons(conn),
        "nav_active": "issues",
    }


@router.get("/issues/new")
def new_issue(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    project: Annotated[str, Query()] = "",
):
    values: dict[str, object] = {
        "project": project.strip().upper(),
        "issue_type": DEFAULT_TYPE,
        "summary": "",
        "description": "",
        "priority": DEFAULT_PRIORITY,
        "assignee": "",
        "due": "",
        "labels": "",
        "components": [],
        "parent": "",
        "acceptance_criteria": "",
        "reference": "",
    }
    return render(request, "issues/new.html", _form_context(conn, values))


@router.get("/projects/{key}/component-options")
def component_options(request: Request, key: str, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    """The `<option>` elements of the project's non-archived components (UI-27)."""
    project = projects.get_project(conn, key)
    return render(
        request,
        "issues/_component_options.html",
        {"components": projects.list_components(conn, project.id), "selected": []},
    )


def _link_error(
    engine: sqlalchemy.Engine, config: Config, key: str, reference: str, actor_id: int
) -> str | None:
    """Link `reference` to the new issue; the message of the failure, or None when it worked (SYNC-9)."""
    try:
        with engine.begin() as conn:
            selection = link_service.select_reference(conn, reference)
        link_service.link_selection(
            engine, config, key, selection, role=DEFAULT_ROLE, note=None, actor_id=actor_id, via=VIA
        )
    except Exception as exc:  # the issue exists by now; whatever went wrong is reported, not raised
        return str(exc) or type(exc).__name__
    return None


@router.post("/issues")
def create(
    request: Request,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
    project: Annotated[str, Form()] = "",
    issue_type: Annotated[str, Form()] = DEFAULT_TYPE,
    summary: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = DEFAULT_PRIORITY,
    assignee: Annotated[str, Form()] = "",
    due: Annotated[str, Form()] = "",
    labels: Annotated[str, Form()] = "",
    components: Annotated[list[str] | None, Form()] = None,
    parent: Annotated[str, Form()] = "",
    acceptance_criteria: Annotated[str, Form()] = "",
    reference: Annotated[str, Form()] = "",
) -> Response:
    """Create the issue in one transaction, then link the optional source outside of it (UI-27)."""
    values: dict[str, object] = {
        "project": project.strip().upper(),
        "issue_type": issue_type,
        "summary": summary,
        "description": description,
        "priority": priority,
        "assignee": assignee,
        "due": due,
        "labels": labels,
        "components": list(components or []),
        "parent": parent,
        "acceptance_criteria": acceptance_criteria,
        "reference": reference,
    }
    label_names = _split(labels)
    component_names = [name.strip() for name in components or [] if name.strip()]
    parent_key = parent.strip() or None
    try:
        with engine.begin() as conn:
            me = people.get_me(conn)
            assignee_id = _assignee_id(assignee)
            due_date = _due_date(due)
            issue = issues.create_issue(
                conn,
                project,
                summary,
                description,
                actor_id=me.id,
                issue_type=issue_type,
                parent_key=parent_key,
                priority=priority,
                assignee_id=assignee_id,
                due_date=due_date,
                acceptance_criteria=acceptance_criteria,
                labels=label_names,
                components=component_names,
            )
            audit.log(
                conn,
                "issue.create",
                "B",
                actor_type="user",
                target=issue.key,
                detail={
                    "project": project.strip(),
                    "summary": summary,
                    "description": description,
                    "type": issue_type,
                    "parent": parent_key,
                    "priority": priority,
                    "assignee": issue.assignee_id,
                    "reporter": None,
                    "due": due_date.isoformat() if due_date else None,
                    "acceptance": acceptance_criteria,
                    "labels": label_names,
                    "components": component_names,
                    "via": VIA,
                },
            )
    except AiOilError as exc:
        with engine.begin() as conn:
            return render(request, "issues/new.html", _form_context(conn, values, str(exc)), status_code=422)
    url = f"/issues/{issue.key}"
    if reference.strip():
        message = _link_error(engine, config, issue.key, reference, me.id)
        if message is not None:
            url += f"?link_error={quote(message, safe='')}"
    return RedirectResponse(url, status_code=303)
