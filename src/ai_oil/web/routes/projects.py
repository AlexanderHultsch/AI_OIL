"""Projects page: projects, their components and archiving (spec 10.4)."""

from dataclasses import dataclass, field
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import text
from starlette.responses import RedirectResponse, Response

from ai_oil.domain import projects
from ai_oil.domain.issues import STATUS_CATEGORY
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import render
from ai_oil.web.routes.home import CATEGORIES

router = APIRouter()

VIA = "web"
PAGE = "/projects"


@dataclass
class ProjectRow:
    project: projects.Project
    components: list[projects.Component]
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))


def _counts(conn: sqlalchemy.Connection) -> dict[int, dict[str, int]]:
    """Issues per project id and status category."""
    per_project: dict[int, dict[str, int]] = {}
    rows = conn.execute(
        text("SELECT project_id, status, COUNT(*) AS n FROM issue GROUP BY project_id, status")
    ).mappings()
    for row in rows:
        counts = per_project.setdefault(row["project_id"], dict.fromkeys(CATEGORIES, 0))
        counts[STATUS_CATEGORY[row["status"]]] += row["n"]
    return per_project


@router.get(PAGE)
def project_list(request: Request, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    counts = _counts(conn)
    rows = []
    for project in projects.list_projects(conn, include_archived=True):
        row = ProjectRow(project, projects.list_components(conn, project.id))
        row.counts.update(counts.get(project.id, {}))
        rows.append(row)
    return render(
        request,
        "projects/list.html",
        {"rows": rows, "categories": CATEGORIES, "nav_active": "projects"},
    )


@router.post(PAGE)
def project_create(
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    key: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> Response:
    text_ = description.strip() or None
    project = projects.create_project(conn, key, name, description=text_)
    audit.log(
        conn,
        "project.create",
        "B",
        actor_type="user",
        target=project.key,
        detail={"name": project.name, "description": text_, "via": VIA},
    )
    return RedirectResponse(PAGE, status_code=303)


@router.post("/projects/{key}/components")
def component_create(
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> Response:
    project = projects.get_project(conn, key)
    text_ = description.strip() or None
    component = projects.create_component(conn, project.id, name, text_)
    audit.log(
        conn,
        "component.create",
        "B",
        actor_type="user",
        target=project.key,
        detail={"component": component.name, "description": text_, "via": VIA},
    )
    return RedirectResponse(PAGE, status_code=303)


@router.post("/projects/{key}/archive")
def project_archive(
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    archived: Annotated[str, Form()] = "",
) -> Response:
    """Archive (`archived=1`) or unarchive (`archived=0`) a project."""
    if archived not in ("0", "1"):
        raise ValidationError("archived must be 1 or 0")
    project = projects.update_project(conn, key, archived=archived == "1")
    audit.log(
        conn,
        "project.update",
        "B",
        actor_type="user",
        target=project.key,
        detail={"archived": project.archived, "via": VIA},
    )
    return RedirectResponse(PAGE, status_code=303)
