import pytest

from ai_oil.domain import projects
from ai_oil.governance import audit


def test_project_add_and_list(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("project", "add", "OIL", "Open issues", "--description", "The list") == 0
    assert capsys.readouterr().out == "Created project OIL\n"
    assert oil("project", "add", "ZED", "Zed") == 0
    capsys.readouterr()

    assert oil("project", "list") == 0

    assert capsys.readouterr().out == "OIL  Open issues\nZED  Zed\n"
    with db() as conn:
        assert projects.get_project(conn, "OIL").description == "The list"
        entries = audit.list_entries(conn, action_prefix="project.")
    assert [(e.action, e.action_class, e.actor_type, e.target) for e in entries] == [
        ("project.create", "B", "user", "ZED"),
        ("project.create", "B", "user", "OIL"),
    ]
    assert entries[1].detail == {"name": "Open issues", "description": "The list"}


def test_project_list_hides_archived_unless_all(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("project", "add", "OIL", "Open issues")
    oil("project", "add", "OLD", "Old stuff")
    with db() as conn:
        projects.update_project(conn, "OLD", archived=True)
        conn.commit()
    capsys.readouterr()

    assert oil("project", "list") == 0
    assert capsys.readouterr().out == "OIL  Open issues\n"

    assert oil("project", "list", "--all") == 0
    assert capsys.readouterr().out == "OIL  Open issues\nOLD  Old stuff  (archived)\n"


def test_project_add_duplicate_is_an_error(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("project", "add", "OIL", "Open issues") == 0
    capsys.readouterr()

    assert oil("project", "add", "OIL", "Again") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: project OIL already exists\n"


def test_component_add_and_list(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("project", "add", "OIL", "Open issues")
    capsys.readouterr()

    assert oil("component", "add", "oil", "Housing", "--description", "Mechanics") == 0
    assert capsys.readouterr().out == "Created component Housing in OIL\n"
    assert oil("component", "add", "OIL", "Electronics") == 0
    capsys.readouterr()

    assert oil("component", "list", "OIL") == 0

    assert capsys.readouterr().out == "Electronics\nHousing\n"
    with db() as conn:
        project = projects.get_project(conn, "OIL")
        assert projects.get_component(conn, project.id, "Housing").description == "Mechanics"
        entries = audit.list_entries(conn, action_prefix="component.")
    assert [(e.action, e.action_class, e.target) for e in entries] == [
        ("component.create", "B", "OIL"),
        ("component.create", "B", "OIL"),
    ]
    assert entries[1].detail == {"component": "Housing", "description": "Mechanics"}


def test_component_commands_on_unknown_project(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("component", "add", "NOPE", "X") == 1
    assert capsys.readouterr().err == "error: project NOPE not found\n"
    assert oil("component", "list", "NOPE") == 1
    assert capsys.readouterr().err == "error: project NOPE not found\n"
