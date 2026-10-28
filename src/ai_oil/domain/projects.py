"""Projects, components and labels (DM-1 .. DM-4)."""

import re
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import bindparam, text

from ai_oil.domain import people
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.timeutil import utcnow_iso

_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9]{1,9}")
_PROJECT_COLUMNS = "id, key, name, description, lead_id, next_number, archived"
_COMPONENT_COLUMNS = "id, project_id, name, description, archived"
_CLOSED_STATUSES = ("Done", "Cancelled")
_ARCHIVE_KEYS_SHOWN = 10


@dataclass(frozen=True)
class Project:
    id: int
    key: str
    name: str
    description: str | None
    lead_id: int | None
    next_number: int
    archived: bool


@dataclass(frozen=True)
class Component:
    id: int
    project_id: int
    name: str
    description: str | None
    archived: bool


@dataclass(frozen=True)
class Label:
    id: int
    name: str


def _project(row: sqlalchemy.RowMapping) -> Project:
    return Project(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        description=row["description"],
        lead_id=row["lead_id"],
        next_number=row["next_number"],
        archived=bool(row["archived"]),
    )


def _component(row: sqlalchemy.RowMapping) -> Component:
    return Component(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        description=row["description"],
        archived=bool(row["archived"]),
    )


def create_project(
    conn: sqlalchemy.Connection,
    key: str,
    name: str,
    *,
    description: str | None = None,
    lead_id: int | None = None,
) -> Project:
    """Create a project; the key is 2-10 characters of `[A-Z][A-Z0-9]*` and is not auto-upper-cased (DM-1)."""
    key = key.strip()
    if not _KEY_PATTERN.fullmatch(key):
        raise ValidationError(
            f"invalid project key {key!r}: use 2-10 characters, upper-case letters and digits,"
            " starting with a letter"
        )
    name = name.strip()
    if not name:
        raise ValidationError("project name must not be blank")
    if conn.execute(text("SELECT 1 FROM project WHERE key = :key"), {"key": key}).first() is not None:
        raise ConflictError(f"project {key} already exists")
    if lead_id is not None:
        lead_id = people.surviving_person(conn, lead_id).id
    conn.execute(
        text(
            "INSERT INTO project (key, name, description, lead_id, created_at)"
            " VALUES (:key, :name, :description, :lead_id, :now)"
        ),
        {"key": key, "name": name, "description": description, "lead_id": lead_id, "now": utcnow_iso()},
    )
    return get_project(conn, key)


def get_project(conn: sqlalchemy.Connection, key: str) -> Project:
    """Look a project up by key, case-insensitively."""
    row = (
        conn.execute(
            text(f"SELECT {_PROJECT_COLUMNS} FROM project WHERE key = :key"), {"key": key.strip().upper()}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"project {key.strip().upper()} not found")
    return _project(row)


def get_project_by_id(conn: sqlalchemy.Connection, project_id: int) -> Project:
    row = (
        conn.execute(text(f"SELECT {_PROJECT_COLUMNS} FROM project WHERE id = :id"), {"id": project_id})
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"project {project_id} not found")
    return _project(row)


def list_projects(conn: sqlalchemy.Connection, *, include_archived: bool = False) -> list[Project]:
    where = "" if include_archived else " WHERE archived = 0"
    rows = conn.execute(text(f"SELECT {_PROJECT_COLUMNS} FROM project{where} ORDER BY key")).mappings()
    return [_project(row) for row in rows]


def _check_archivable(conn: sqlalchemy.Connection, project: Project) -> None:
    """Refuse archiving while any issue of the project is not Done or Cancelled (DM-1)."""
    open_keys = list(
        conn.execute(
            text(
                "SELECT key FROM issue WHERE project_id = :project_id AND status NOT IN :closed"
                " ORDER BY number"
            ).bindparams(bindparam("closed", expanding=True)),
            {"project_id": project.id, "closed": list(_CLOSED_STATUSES)},
        ).scalars()
    )
    if not open_keys:
        return
    shown = ", ".join(open_keys[:_ARCHIVE_KEYS_SHOWN])
    if len(open_keys) > _ARCHIVE_KEYS_SHOWN:
        shown += f" and {len(open_keys) - _ARCHIVE_KEYS_SHOWN} more"
    count = "1 issue is" if len(open_keys) == 1 else f"{len(open_keys)} issues are"
    raise ValidationError(f"project {project.key} cannot be archived: {count} not Done or Cancelled: {shown}")


def update_project(
    conn: sqlalchemy.Connection,
    key: str,
    *,
    name: str | None = None,
    description: str | None = None,
    lead_id: int | None = None,
    archived: bool | None = None,
) -> Project:
    """Change the given fields only; the key itself cannot be changed (DM-1, DM-23).

    Archiving is refused while an issue is not Done or Cancelled; unarchiving is always allowed.
    """
    project = get_project(conn, key)
    if archived and not project.archived:
        _check_archivable(conn, project)
    changes: dict[str, object] = {}
    if name is not None:
        name = name.strip()
        if not name:
            raise ValidationError("project name must not be blank")
        changes["name"] = name
    if description is not None:
        changes["description"] = description
    if lead_id is not None:
        changes["lead_id"] = people.surviving_person(conn, lead_id).id
    if archived is not None:
        changes["archived"] = int(archived)
    if changes:
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        conn.execute(text(f"UPDATE project SET {assignments} WHERE id = :id"), {**changes, "id": project.id})
    return get_project_by_id(conn, project.id)


def allocate_issue_number(conn: sqlalchemy.Connection, project_id: int) -> int:
    """Take the next issue number of a project; numbers are never reused (DM-2)."""
    number = conn.execute(
        text("UPDATE project SET next_number = next_number + 1 WHERE id = :id RETURNING next_number - 1"),
        {"id": project_id},
    ).scalar_one_or_none()
    if number is None:
        raise NotFoundError(f"project {project_id} not found")
    return number


def create_component(
    conn: sqlalchemy.Connection, project_id: int, name: str, description: str | None = None
) -> Component:
    """Add a component to a project (DM-3)."""
    name = name.strip()
    if not name:
        raise ValidationError("component name must not be blank")
    project = get_project_by_id(conn, project_id)
    if (
        conn.execute(
            text("SELECT 1 FROM component WHERE project_id = :project_id AND name = :name"),
            {"project_id": project_id, "name": name},
        ).first()
        is not None
    ):
        raise ConflictError(f"component {name!r} already exists in project {project.key}")
    component_id = conn.execute(
        text(
            "INSERT INTO component (project_id, name, description)"
            " VALUES (:project_id, :name, :description) RETURNING id"
        ),
        {"project_id": project_id, "name": name, "description": description},
    ).scalar_one()
    return Component(component_id, project_id, name, description, False)


def list_components(
    conn: sqlalchemy.Connection, project_id: int, *, include_archived: bool = False
) -> list[Component]:
    archived_filter = "" if include_archived else " AND archived = 0"
    rows = conn.execute(
        text(
            f"SELECT {_COMPONENT_COLUMNS} FROM component WHERE project_id = :project_id{archived_filter}"
            " ORDER BY name"
        ),
        {"project_id": project_id},
    ).mappings()
    return [_component(row) for row in rows]


def get_component(conn: sqlalchemy.Connection, project_id: int, name: str) -> Component:
    row = (
        conn.execute(
            text(
                f"SELECT {_COMPONENT_COLUMNS} FROM component WHERE project_id = :project_id AND name = :name"
            ),
            {"project_id": project_id, "name": name.strip()},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"component {name.strip()!r} not found")
    return _component(row)


def get_or_create_label(conn: sqlalchemy.Connection, name: str) -> Label:
    """Return the label, creating it if needed; labels are global and case-insensitive (DM-4)."""
    name = name.strip()
    if not name:
        raise ValidationError("label must not be blank")
    if " " in name or "," in name:
        raise ValidationError(f"invalid label {name!r}: labels must not contain spaces or commas")
    row = conn.execute(text("SELECT id, name FROM label WHERE name = :name"), {"name": name}).first()
    if row is not None:
        return Label(row.id, row.name)
    label_id = conn.execute(
        text("INSERT INTO label (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()
    return Label(label_id, name)


def list_labels(conn: sqlalchemy.Connection) -> list[Label]:
    rows = conn.execute(text("SELECT id, name FROM label ORDER BY name COLLATE NOCASE, id"))
    return [Label(row.id, row.name) for row in rows]
