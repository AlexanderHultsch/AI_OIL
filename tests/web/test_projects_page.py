import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_oil.domain import issues, people, projects
from ai_oil.governance import audit

ERROR_PREFIX = '<div class="error" role="alert">'


@pytest.fixture
def post(client: TestClient, csrf_token: str):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        form = {**({"csrf_token": csrf_token} if csrf else {}), **(data or {})}
        return client.post(path, data=form, follow_redirects=False)

    return send


def block_of(page: str, key: str) -> str:
    match = re.search(rf'<section class="project-block" id="project-{key}">.*?</section>', page, re.DOTALL)
    assert match is not None, f"no block for {key}"
    return match.group(0)


def counts_of(block: str) -> list[str]:
    body = re.search(r"<tbody>(.*?)</tbody>", block, re.DOTALL)
    assert body is not None
    return re.findall(r'<td class="num">(\d+)</td>', body.group(1))


def components_of(block: str) -> list[str]:
    return re.findall(r"<li>(.*?)(?: <span|</li>)", block)


def audit_entries(app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def add_component(app: FastAPI, key: str, name: str, description: str | None = None) -> None:
    with app.state.engine.begin() as conn:
        projects.create_component(conn, projects.get_project(conn, key).id, name, description)


# --- the list -----------------------------------------------------------------------------------------------


def test_the_page_lists_all_projects_with_counts_per_category(client: TestClient) -> None:
    response = client.get("/projects")

    page = response.text
    assert response.status_code == 200
    assert re.findall(r'<section class="project-block" id="project-(\w+)">', page) == ["ABC", "OIL", "OLD"]
    assert counts_of(block_of(page, "OIL")) == ["2", "1", "1"]
    assert counts_of(block_of(page, "ABC")) == ["0", "0", "0"]
    assert counts_of(block_of(page, "OLD")) == ["1", "0", "0"]
    assert re.findall(r"<th class=\"num\">(.*?)</th>", block_of(page, "OIL")) == [
        "To Do",
        "In Progress",
        "Done",
    ]
    assert re.search(r'<a class="active" href="/projects"|href="/projects" class="active"', page)


def test_the_page_shows_name_description_and_the_archived_badge(app: FastAPI, client: TestClient) -> None:
    with app.state.engine.begin() as conn:
        projects.update_project(conn, "OIL", description="Everything <open>")

    page = client.get("/projects").text

    oil = block_of(page, "OIL")
    assert "<span>Open issues</span>" in oil
    assert "Everything &lt;open&gt;" in oil
    assert "archived</span>" not in oil
    old = block_of(page, "OLD")
    assert '<span class="badge">archived</span>' in old
    assert ">Unarchive</button>" in old
    assert ">Archive</button>" in block_of(page, "OIL")


def test_components_are_listed_per_project(app: FastAPI, client: TestClient) -> None:
    add_component(app, "OIL", "Power stage", "Buck converter")
    add_component(app, "OIL", "Firmware")

    page = client.get("/projects").text

    assert components_of(block_of(page, "OIL")) == ["Firmware", "Power stage"]
    assert "— Buck converter" in block_of(page, "OIL")
    assert "No components." in block_of(page, "ABC")
    assert "No components." not in block_of(page, "OIL")


def test_the_page_has_the_add_forms_with_a_csrf_field(client: TestClient) -> None:
    page = client.get("/projects").text

    assert '<form class="inline-form" action="/projects" method="post" aria-label="Add project">' in page
    assert 'action="/projects/OIL/components"' in page
    assert 'action="/projects/OIL/archive"' in page
    assert len(re.findall(r'<input type="hidden" name="csrf_token" value="[^"]+">', page)) == 1 + 3 * 2


# --- adding -------------------------------------------------------------------------------------------------


def test_adding_a_project_redirects_back_and_audits(app: FastAPI, client: TestClient, post) -> None:
    response = post("/projects", {"key": "NEW", "name": "New program", "description": " Fresh start "})

    assert response.status_code == 303
    assert response.headers["location"] == "/projects"
    with app.state.engine.begin() as conn:
        project = projects.get_project(conn, "NEW")
    assert (project.name, project.description, project.archived) == ("New program", "Fresh start", False)
    [entry] = audit_entries(app, "project.create")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "NEW")
    assert entry.detail == {"name": "New program", "description": "Fresh start", "via": "web"}
    assert counts_of(block_of(client.get("/projects").text, "NEW")) == ["0", "0", "0"]


def test_a_blank_description_is_stored_as_none(app: FastAPI, post) -> None:
    post("/projects", {"key": "NEW", "name": "New program", "description": "  "})

    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "NEW").description is None
    assert audit_entries(app, "project.create")[0].detail["description"] is None


@pytest.mark.parametrize(
    ("data", "status", "message"),
    [
        ({"key": "OIL", "name": "Again"}, 422, "project OIL already exists"),
        ({"key": "oil", "name": "Lower"}, 422, "invalid project key"),
        ({"key": "X", "name": "Short"}, 422, "invalid project key"),
        ({"key": "NEW", "name": "  "}, 422, "project name must not be blank"),
    ],
)
def test_adding_an_invalid_project_shows_the_error_and_stores_nothing(
    app: FastAPI, post, data: dict, status: int, message: str
) -> None:
    response = post("/projects", data)

    assert response.status_code == status
    assert ERROR_PREFIX in response.text
    assert message in response.text
    assert audit_entries(app, "project.create") == []
    with app.state.engine.begin() as conn:
        assert [p.key for p in projects.list_projects(conn, include_archived=True)] == ["ABC", "OIL", "OLD"]


def test_adding_a_component_redirects_back_and_audits(app: FastAPI, client: TestClient, post) -> None:
    response = post("/projects/oil/components", {"name": " Power stage ", "description": "Buck"})

    assert response.status_code == 303
    assert response.headers["location"] == "/projects"
    with app.state.engine.begin() as conn:
        oil = projects.get_project(conn, "OIL")
        [component] = projects.list_components(conn, oil.id)
    assert (component.name, component.description) == ("Power stage", "Buck")
    [entry] = audit_entries(app, "component.create")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "OIL")
    assert entry.detail == {"component": "Power stage", "description": "Buck", "via": "web"}
    assert components_of(block_of(client.get("/projects").text, "OIL")) == ["Power stage"]


@pytest.mark.parametrize(
    ("path", "name", "status", "message"),
    [
        ("/projects/OIL/components", "Power stage", 422, "already exists in project OIL"),
        ("/projects/OIL/components", "  ", 422, "component name must not be blank"),
        ("/projects/NOPE/components", "Anything", 404, "project NOPE not found"),
    ],
)
def test_an_invalid_component_shows_the_error(
    app: FastAPI, post, path: str, name: str, status: int, message: str
) -> None:
    add_component(app, "OIL", "Power stage")

    response = post(path, {"name": name})

    assert response.status_code == status
    assert ERROR_PREFIX in response.text
    assert message in response.text
    assert audit_entries(app, "component.create") == []


# --- archiving ----------------------------------------------------------------------------------------------


def test_archiving_and_unarchiving_a_project(app: FastAPI, client: TestClient, post) -> None:
    archived = post("/projects/ABC/archive", {"archived": "1"})

    assert archived.status_code == 303 and archived.headers["location"] == "/projects"
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "ABC").archived is True
        assert [p.key for p in projects.list_projects(conn)] == ["OIL"]
    assert '<span class="badge">archived</span>' in block_of(client.get("/projects").text, "ABC")

    restored = post("/projects/OLD/archive", {"archived": "0"})

    assert restored.status_code == 303
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "OLD").archived is False
    assert "archived</span>" not in block_of(client.get("/projects").text, "OLD")
    entries = audit_entries(app, "project.update")
    assert [(e.target, e.detail) for e in reversed(entries)] == [
        ("ABC", {"archived": True, "via": "web"}),
        ("OLD", {"archived": False, "via": "web"}),
    ]
    assert {(e.action_class, e.actor_type) for e in entries} == {("B", "user")}


def test_archiving_a_project_with_open_issues_shows_the_refusal_and_changes_nothing(
    app: FastAPI, post
) -> None:
    response = post("/projects/OIL/archive", {"archived": "1"})

    assert response.status_code == 422
    assert ERROR_PREFIX in response.text
    assert (
        "project OIL cannot be archived: 3 issues are not Done or Cancelled: OIL-1, OIL-2, OIL-3"
        in response.text
    )
    assert audit_entries(app, "project.update") == []
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "OIL").archived is False


def test_archiving_a_project_whose_issues_are_all_done_works(app: FastAPI, post) -> None:
    with app.state.engine.begin() as conn:
        me = people.get_me(conn)
        for key in ("OIL-1", "OIL-2", "OIL-3"):
            issues.transition_issue(conn, key, "Done", actor_id=me.id)

    response = post("/projects/OIL/archive", {"archived": "1"})

    assert response.status_code == 303
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "OIL").archived is True


def test_unarchiving_a_project_with_open_issues_works(app: FastAPI, post) -> None:
    response = post("/projects/OLD/archive", {"archived": "0"})

    assert response.status_code == 303
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "OLD").archived is False


def test_an_issue_of_an_archived_project_can_still_be_edited_and_reopened(app: FastAPI, post) -> None:
    with app.state.engine.begin() as conn:
        issues.transition_issue(conn, "OLD-1", "Done", actor_id=people.get_me(conn).id)

    edited = post("/issues/OLD-1/field/summary", {"value": "Renamed"})
    reopened = post("/issues/OLD-1/transition", {"value": "In Progress"})

    assert edited.status_code == 200 and reopened.status_code == 200
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, "OLD-1")
    assert (issue.summary, issue.status) == ("Renamed", "In Progress")


@pytest.mark.parametrize(
    ("key", "value", "status"), [("NOPE", "1", 404), ("ABC", "yes", 422), ("ABC", "", 422)]
)
def test_an_invalid_archive_request_is_an_error(
    app: FastAPI, post, key: str, value: str, status: int
) -> None:
    response = post(f"/projects/{key}/archive", {"archived": value})

    assert response.status_code == status
    assert ERROR_PREFIX in response.text
    assert audit_entries(app, "project.update") == []
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "ABC").archived is False


def test_adding_a_project_without_the_csrf_token_is_refused(app: FastAPI, post) -> None:
    response = post("/projects", {"key": "NEW", "name": "New program"}, csrf=False)

    assert response.status_code == 403
    assert audit_entries(app, "project.create") == []
    with app.state.engine.begin() as conn:
        assert [p.key for p in projects.list_projects(conn, include_archived=True)] == ["ABC", "OIL", "OLD"]
