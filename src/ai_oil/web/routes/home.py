"""Home page and health check."""

from dataclasses import dataclass, field
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from ai_oil.domain import projects
from ai_oil.domain.issues import STATUS_CATEGORY
from ai_oil.web.deps import db_read
from ai_oil.web.rendering import render

router = APIRouter()

CATEGORIES = ("To Do", "In Progress", "Done")


@dataclass
class ProjectCounts:
    key: str
    name: str
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))


@router.get("/")
def home(request: Request, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    rows = conn.execute(
        text("SELECT project_id, status, COUNT(*) AS n FROM issue GROUP BY project_id, status")
    ).mappings()
    per_project: dict[int, dict[str, int]] = {}
    for row in rows:
        counts = per_project.setdefault(row["project_id"], dict.fromkeys(CATEGORIES, 0))
        counts[STATUS_CATEGORY[row["status"]]] += row["n"]
    overview = []
    for project in projects.list_projects(conn):
        entry = ProjectCounts(key=project.key, name=project.name)
        entry.counts.update(per_project.get(project.id, {}))
        overview.append(entry)
    return render(request, "home.html", {"projects": overview, "categories": CATEGORIES})


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
