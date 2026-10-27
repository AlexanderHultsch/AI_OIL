import pytest
import sqlalchemy

from ai_oil.domain import projects
from ai_oil.errors import ConflictError, NotFoundError, ValidationError


@pytest.mark.parametrize("key", ["oil", "O", "ABCDEFGHIJK", "1AB", "", "A B", "OIL-1"])
def test_invalid_keys_rejected(conn: sqlalchemy.Connection, key: str) -> None:
    with pytest.raises(ValidationError, match="project key"):
        projects.create_project(conn, key, "Name")


@pytest.mark.parametrize("key", ["OIL", "HW2", "AB", "ABCDEFGHIJ"])
def test_valid_keys_accepted(conn: sqlalchemy.Connection, key: str) -> None:
    project = projects.create_project(conn, key, "Name")
    assert project.key == key


def test_create_project_returns_defaults(conn: sqlalchemy.Connection) -> None:
    project = projects.create_project(conn, "OIL", " Open issues ", description="d")
    assert project == projects.Project(
        id=project.id,
        key="OIL",
        name="Open issues",
        description="d",
        lead_id=None,
        next_number=1,
        archived=False,
    )


def test_blank_name_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        projects.create_project(conn, "OIL", "   ")


def test_duplicate_key_rejected(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "OIL", "One")
    with pytest.raises(ConflictError):
        projects.create_project(conn, "OIL", "Two")


def test_get_project_is_case_insensitive(conn: sqlalchemy.Connection) -> None:
    created = projects.create_project(conn, "OIL", "One")
    assert projects.get_project(conn, "oil") == created
    assert projects.get_project_by_id(conn, created.id) == created
    with pytest.raises(NotFoundError):
        projects.get_project(conn, "NOPE")
    with pytest.raises(NotFoundError):
        projects.get_project_by_id(conn, 999)


def test_list_projects_ordered_and_filters_archived(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "ZED", "Z")
    projects.create_project(conn, "ABC", "A")
    projects.create_project(conn, "MID", "M")
    projects.update_project(conn, "MID", archived=True)
    assert [p.key for p in projects.list_projects(conn)] == ["ABC", "ZED"]
    assert [p.key for p in projects.list_projects(conn, include_archived=True)] == ["ABC", "MID", "ZED"]


def test_update_project_changes_only_given_fields(conn: sqlalchemy.Connection) -> None:
    conn.execute(sqlalchemy.text("INSERT INTO person(id, display_name) VALUES (1, 'Lead')"))
    projects.create_project(conn, "OIL", "One", description="old")
    updated = projects.update_project(conn, "oil", name="Renamed", lead_id=1)
    assert (updated.key, updated.name, updated.description, updated.lead_id, updated.archived) == (
        "OIL",
        "Renamed",
        "old",
        1,
        False,
    )
    updated = projects.update_project(conn, "OIL", description="new", archived=True)
    assert (updated.name, updated.description, updated.lead_id, updated.archived) == (
        "Renamed",
        "new",
        1,
        True,
    )
    updated = projects.update_project(conn, "OIL", archived=False)
    assert updated.archived is False
    assert projects.update_project(conn, "OIL") == updated


def test_update_project_blank_name_and_unknown_key(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "OIL", "One")
    with pytest.raises(ValidationError):
        projects.update_project(conn, "OIL", name=" ")
    with pytest.raises(NotFoundError):
        projects.update_project(conn, "NOPE", name="x")


def test_allocate_issue_number_counts_up(conn: sqlalchemy.Connection) -> None:
    project = projects.create_project(conn, "OIL", "One")
    assert [projects.allocate_issue_number(conn, project.id) for _ in range(3)] == [1, 2, 3]
    assert projects.get_project(conn, "OIL").next_number == 4


def test_allocate_issue_number_unknown_project(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(NotFoundError):
        projects.allocate_issue_number(conn, 999)


def test_components(conn: sqlalchemy.Connection) -> None:
    oil = projects.create_project(conn, "OIL", "One")
    other = projects.create_project(conn, "OTH", "Two")
    projects.create_component(conn, oil.id, "Hardware", "boards")
    projects.create_component(conn, oil.id, "Firmware")
    projects.create_component(conn, other.id, "Hardware")

    with pytest.raises(ConflictError):
        projects.create_component(conn, oil.id, "Hardware")
    with pytest.raises(ValidationError):
        projects.create_component(conn, oil.id, "  ")
    with pytest.raises(NotFoundError):
        projects.create_component(conn, 999, "X")

    assert [c.name for c in projects.list_components(conn, oil.id)] == ["Firmware", "Hardware"]
    assert projects.get_component(conn, oil.id, "Hardware").description == "boards"
    with pytest.raises(NotFoundError):
        projects.get_component(conn, oil.id, "Mechanics")


def test_list_components_filters_archived(conn: sqlalchemy.Connection) -> None:
    oil = projects.create_project(conn, "OIL", "One")
    projects.create_component(conn, oil.id, "Old")
    projects.create_component(conn, oil.id, "New")
    conn.execute(sqlalchemy.text("UPDATE component SET archived = 1 WHERE name = 'Old'"))
    assert [c.name for c in projects.list_components(conn, oil.id)] == ["New"]
    listed = projects.list_components(conn, oil.id, include_archived=True)
    assert [(c.name, c.archived) for c in listed] == [("New", False), ("Old", True)]


@pytest.mark.parametrize("name", ["", "  ", "two words", "a,b"])
def test_invalid_labels_rejected(conn: sqlalchemy.Connection, name: str) -> None:
    with pytest.raises(ValidationError):
        projects.get_or_create_label(conn, name)


def test_label_reuse_is_case_insensitive(conn: sqlalchemy.Connection) -> None:
    first = projects.get_or_create_label(conn, " Hardware ")
    again = projects.get_or_create_label(conn, "HARDWARE")
    assert first.name == "Hardware"
    assert again == first
    projects.get_or_create_label(conn, "alpha")
    assert [label.name for label in projects.list_labels(conn)] == ["alpha", "Hardware"]
