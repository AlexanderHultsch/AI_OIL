import re
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil.domain import history, issues, people, projects
from ai_oil.governance import audit
from ai_oil.sources import linking

ERROR_PREFIX = '<div class="error" role="alert">'


@pytest.fixture
def post(client: TestClient, csrf_token: str):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        form = {**({"csrf_token": csrf_token} if csrf else {}), **(data or {})}
        return client.post(path, data=form, follow_redirects=False)

    return send


def options_of(page: str, select_id: str) -> list[tuple[str, str, bool]]:
    """(value, label, selected) of the options of the select with the id."""
    select = re.search(rf'<select id="{select_id}".*?</select>', page, re.DOTALL)
    assert select is not None, f"no select {select_id}"
    return [
        (value, label, selected.strip() == "selected")
        for value, selected, label in re.findall(
            r'<option value="([^"]*)"((?: selected)?)>(.*?)</option>', select.group(0)
        )
    ]


def seed_components(app: FastAPI) -> None:
    with app.state.engine.begin() as conn:
        oil = projects.get_project(conn, "OIL")
        projects.create_component(conn, oil.id, "Power stage")
        projects.create_component(conn, oil.id, "Firmware")
        projects.create_component(conn, oil.id, "Retired part")
        conn.execute(text("UPDATE component SET archived = 1 WHERE name = 'Retired part'"))
        abc = projects.get_project(conn, "ABC")
        projects.create_component(conn, abc.id, "Housing")


def add_person(app: FastAPI, name: str) -> int:
    with app.state.engine.begin() as conn:
        return people.create_person(conn, name).id


def audit_entries(app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def issue_count(app: FastAPI) -> int:
    with app.state.engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM issue")).scalar_one()


# --- the form -----------------------------------------------------------------------------------------------


def test_the_form_has_all_fields_with_their_defaults(app: FastAPI, client: TestClient) -> None:
    bea = add_person(app, "Bea Brandt")

    response = client.get("/issues/new")

    page = response.text
    assert response.status_code == 200
    assert '<form class="create-form" action="/issues" method="post">' in page
    assert re.search(r'<input type="hidden" name="csrf_token" value="[^"]+">', page)
    assert options_of(page, "create-project") == [
        ("ABC", "ABC — Alphabet", True),
        ("OIL", "OIL — Open issues", False),
    ]
    assert options_of(page, "create-type") == [
        ("Epic", "Epic", False),
        ("Task", "Task", True),
        ("Sub-task", "Sub-task", False),
    ]
    assert options_of(page, "create-priority") == [(p, p, p == "P4") for p in ("P1", "P2", "P3", "P4", "P5")]
    assert [(v, label, s) for v, label, s in options_of(page, "create-assignee") if v in ("", str(bea))] == [
        ("", "Unassigned", True),
        (str(bea), "Bea Brandt", False),
    ]
    for name in ("summary", "description", "due", "labels", "parent", "acceptance_criteria", "reference"):
        assert f'name="{name}"' in page
    assert '<input type="date" id="create-due" name="due" value="">' in page
    assert "Link a source now" in page
    assert '<select id="create-components" name="components" multiple' in page
    assert options_of(page, "create-components") == []


def test_the_assignee_choices_are_the_non_merged_persons_by_id(app: FastAPI, client: TestClient) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")
    with app.state.engine.begin() as conn:
        people.merge_persons(conn, twin, bea, actor_id=people.get_me(conn).id)
        me = people.get_me(conn)

    choices = options_of(client.get("/issues/new").text, "create-assignee")

    assert choices == [
        ("", "Unassigned", True),
        (str(me.id), "Alex", False),
        (str(bea), "Bea Brandt", False),
    ]


def test_the_project_query_parameter_selects_the_project_and_lists_its_components(
    app: FastAPI, client: TestClient
) -> None:
    seed_components(app)

    page = client.get("/issues/new?project=oil").text

    assert [(v, s) for v, _, s in options_of(page, "create-project")] == [("ABC", False), ("OIL", True)]
    assert options_of(page, "create-components") == [
        ("Firmware", "Firmware", False),
        ("Power stage", "Power stage", False),
    ]


@pytest.mark.parametrize("requested", ["OLD", "NOPE", ""])
def test_an_archived_or_unknown_project_falls_back_to_the_first(client: TestClient, requested: str) -> None:
    page = client.get("/issues/new", params={"project": requested}).text

    assert [(v, s) for v, _, s in options_of(page, "create-project")] == [("ABC", True), ("OIL", False)]


def test_an_archived_project_is_not_offered_in_the_project_choice(app: FastAPI, client: TestClient) -> None:
    with app.state.engine.begin() as conn:
        projects.update_project(conn, "ABC", archived=True)

    page = client.get("/issues/new", params={"project": "ABC"}).text

    assert [(v, s) for v, _, s in options_of(page, "create-project")] == [("OIL", True)]


def test_changing_the_project_asks_for_its_component_options(client: TestClient) -> None:
    page = client.get("/issues/new").text

    select = re.search(r'<select id="create-project".*?>', page, re.DOTALL)
    assert select is not None
    assert 'hx-get="/projects/__project__/component-options"' in select.group(0)
    assert 'data-path-from-value="__project__"' in select.group(0)
    assert 'hx-target="#create-components"' in select.group(0)
    assert "data-path-from-value" in client.get("/static/app.js").text


def test_the_component_options_fragment_lists_the_non_archived_components_of_the_project(
    app: FastAPI, client: TestClient
) -> None:
    seed_components(app)

    oil = client.get("/projects/OIL/component-options")
    abc = client.get("/projects/abc/component-options")
    none = client.get("/projects/OLD/component-options")

    assert oil.status_code == 200
    assert (
        oil.text
        == '<option value="Firmware">Firmware</option><option value="Power stage">Power stage</option>'
    )
    assert abc.text == '<option value="Housing">Housing</option>'
    assert none.status_code == 200 and none.text == ""


def test_the_component_options_of_an_unknown_project_are_a_404(client: TestClient) -> None:
    assert client.get("/projects/NOPE/component-options").status_code == 404


def test_every_page_has_the_create_link_in_the_header(client: TestClient) -> None:
    for path in ("/", "/issues", "/issues/new"):
        header = re.search(r'<header class="topbar">.*?</header>', client.get(path).text, re.DOTALL)
        assert header is not None
        assert '<a class="button primary" href="/issues/new">Create</a>' in header.group(0), path


def test_new_is_not_taken_as_an_issue_key(client: TestClient) -> None:
    assert "Create issue" in client.get("/issues/new").text
    assert client.get("/issues/OIL-1").status_code == 200


# --- creating -----------------------------------------------------------------------------------------------


def test_creating_with_all_fields_stores_them_and_audits(app: FastAPI, client: TestClient, post) -> None:
    seed_components(app)
    bea = add_person(app, "Bea Brandt")
    with app.state.engine.begin() as conn:
        issues.create_issue(conn, "OIL", "Platform", "Epic", actor_id=1, issue_type="Epic")
    epic = "OIL-5"

    response = post(
        "/issues",
        {
            "project": "OIL",
            "issue_type": "Task",
            "summary": "  Qualify inductor  ",
            "description": "Second source for L1",
            "priority": "P2",
            "assignee": str(bea),
            "due": "2026-12-01",
            "labels": "supplier, b-sample,,",
            "components": ["Power stage", "Firmware"],
            "parent": epic,
            "acceptance_criteria": "EMC passed",
            "reference": "",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/issues/OIL-6"
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, "OIL-6")
        events = [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]
        me = people.get_me(conn)
    assert (issue.summary, issue.description, issue.issue_type, issue.priority) == (
        "Qualify inductor",
        "Second source for L1",
        "Task",
        "P2",
    )
    assert (issue.assignee_id, issue.reporter_id, issue.status) == (bea, me.id, "New")
    assert (issue.original_due_date, issue.revised_due_date) == (date(2026, 12, 1), date(2026, 12, 1))
    assert sorted(issue.labels) == ["b-sample", "supplier"]
    assert sorted(issue.components) == ["Firmware", "Power stage"]
    assert issue.parent_key == epic
    assert issue.acceptance_criteria == "EMC passed"
    assert events == [("issue", "create", None, "OIL-6")]
    [entry] = audit_entries(app, "issue.create")[:1]
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "OIL-6")
    assert entry.detail == {
        "project": "OIL",
        "summary": "  Qualify inductor  ",
        "description": "Second source for L1",
        "type": "Task",
        "parent": epic,
        "priority": "P2",
        "assignee": bea,
        "reporter": None,
        "due": "2026-12-01",
        "acceptance": "EMC passed",
        "labels": ["supplier", "b-sample"],
        "components": ["Power stage", "Firmware"],
        "via": "web",
    }
    assert client.get(response.headers["location"]).status_code == 200


def test_creating_in_an_archived_project_is_refused_and_nothing_is_stored(app: FastAPI, post) -> None:
    before = issue_count(app)

    response = post(
        "/issues",
        {"project": "OLD", "issue_type": "Task", "summary": "Late", "description": "Too late"},
    )

    assert response.status_code == 422
    assert ERROR_PREFIX in response.text
    assert "project OLD is archived — unarchive it to add issues" in response.text
    assert issue_count(app) == before
    assert audit_entries(app, "issue.create") == []
    with app.state.engine.begin() as conn:
        assert projects.get_project(conn, "OLD").next_number == 2


def test_an_assignee_that_was_merged_is_stored_and_audited_as_the_survivor(app: FastAPI, post) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")
    with app.state.engine.begin() as conn:
        people.merge_persons(conn, twin, bea, actor_id=people.get_me(conn).id)

    response = post(
        "/issues",
        {
            "project": "ABC",
            "issue_type": "Task",
            "summary": "Plain",
            "description": "Text",
            "assignee": str(twin),
        },
    )

    assert response.status_code == 303
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, "ABC-1")
    assert issue.assignee_id == bea
    [entry] = audit_entries(app, "issue.create")
    assert entry.detail["assignee"] == bea


def test_creating_with_only_the_required_fields_uses_the_defaults(app: FastAPI, post) -> None:
    response = post(
        "/issues",
        {"project": "ABC", "issue_type": "Task", "summary": "Plain", "description": "Plain text"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/issues/ABC-1"
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, "ABC-1")
    assert (issue.priority, issue.assignee_id, issue.revised_due_date, issue.parent_key) == (
        "P4",
        None,
        None,
        None,
    )
    assert (issue.labels, issue.components, issue.acceptance_criteria) == ((), (), None)
    [entry] = audit_entries(app, "issue.create")
    assert entry.detail["assignee"] is None and entry.detail["due"] is None
    assert entry.detail["labels"] == [] and entry.detail["components"] == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"summary": ""}, "summary must be 1-120 characters"),
        ({"description": "   "}, "description must not be blank"),
        ({"due": "01.12.2026"}, "due must be a date (YYYY-MM-DD)"),
        ({"assignee": "bea"}, "assignee must be a person id"),
        ({"assignee": "999"}, "person 999 not found"),
        ({"issue_type": "Story"}, "issue_type must be one of"),
        ({"issue_type": "Sub-task"}, "a Sub-task requires a Task as parent"),
        ({"parent": "OIL-99"}, "OIL-99"),
        ({"labels": "has space"}, "invalid label"),
        ({"components": ["Nope"]}, "component 'Nope' not found"),
        ({"project": "NOPE"}, "project NOPE not found"),
    ],
)
def test_an_invalid_form_is_rerendered_with_the_message_and_nothing_is_stored(
    app: FastAPI, post, override: dict, message: str
) -> None:
    seed_components(app)
    before = issue_count(app)
    data = {
        "project": "OIL",
        "issue_type": "Task",
        "summary": "Kept summary",
        "description": "Kept description",
        "priority": "P2",
        "due": "2026-12-01",
        "labels": "supplier",
        "components": ["Firmware"],
        "parent": "",
        "acceptance_criteria": "Kept criteria",
        "reference": "https://example.com/x",
        **override,
    }

    response = post("/issues", data)

    assert response.status_code == 422
    assert ERROR_PREFIX in response.text
    assert message in response.text.replace("&#39;", "'")
    assert issue_count(app) == before
    assert audit_entries(app, "issue.create") == []
    assert audit_entries(app, "source.link") == []


def test_the_rerendered_form_keeps_every_entered_value(app: FastAPI, post) -> None:
    seed_components(app)
    bea = add_person(app, "Bea Brandt")

    response = post(
        "/issues",
        {
            "project": "OIL",
            "issue_type": "Epic",
            "summary": "Kept <summary>",
            "description": "Kept\ndescription",
            "priority": "P1",
            "assignee": str(bea),
            "due": "bad date",
            "labels": "a, b",
            "components": ["Firmware", "Power stage"],
            "parent": "OIL-1",
            "acceptance_criteria": "Kept criteria",
            "reference": "https://example.com/x",
        },
    )

    page = response.text
    assert response.status_code == 422
    assert [(v, s) for v, _, s in options_of(page, "create-project")] == [("ABC", False), ("OIL", True)]
    assert [(v, s) for v, _, s in options_of(page, "create-type")] == [
        ("Epic", True),
        ("Task", False),
        ("Sub-task", False),
    ]
    assert [v for v, _, s in options_of(page, "create-priority") if s] == ["P1"]
    assert [v for v, _, s in options_of(page, "create-assignee") if s] == [str(bea)]
    assert [(v, s) for v, _, s in options_of(page, "create-components")] == [
        ("Firmware", True),
        ("Power stage", True),
    ]
    assert 'name="summary" value="Kept &lt;summary&gt;"' in page
    assert ">Kept\ndescription</textarea>" in page
    assert 'name="due" value="bad date"' in page
    assert 'name="labels" value="a, b"' in page
    assert 'name="parent" value="OIL-1"' in page
    assert ">Kept criteria</textarea>" in page
    assert 'name="reference" value="https://example.com/x"' in page


# --- linking a source now -----------------------------------------------------------------------------------


def test_a_working_reference_is_linked_and_synced_after_the_creation(
    app: FastAPI, client: TestClient, post
) -> None:
    response = post(
        "/issues",
        {
            "project": "OIL",
            "issue_type": "Task",
            "summary": "With source",
            "description": "Has a link",
            "reference": "https://example.com/spec",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/issues/OIL-5"
    with app.state.engine.begin() as conn:
        [link] = linking.list_issue_sources(conn, "OIL-5")
        sync_runs = conn.execute(text("SELECT trigger, scope FROM sync_run")).all()
    assert (link.object_type, link.role, link.connector_name) == ("web_link", "reference", "Manual")
    assert [tuple(r) for r in sync_runs] == [("ui", f"source:{link.source_object_id}")]
    [created] = audit_entries(app, "issue.create")
    [linked] = audit_entries(app, "source.link")
    assert created.detail["via"] == "web" and linked.detail["via"] == "web"
    assert linked.target == "OIL-5"
    page = client.get(response.headers["location"]).text
    assert "example.com/spec" in page
    assert "could not be linked" not in page


def test_a_failing_reference_keeps_the_issue_and_reports_the_reason(
    app: FastAPI, client: TestClient, post
) -> None:
    response = post(
        "/issues",
        {
            "project": "OIL",
            "issue_type": "Task",
            "summary": "With bad source",
            "description": "Link will fail",
            "reference": "no idea what this is",
        },
    )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/issues/OIL-5"
    [message] = parse_qs(location.query)["link_error"]
    assert message.startswith("no connector recognises 'no idea what this is'")
    with app.state.engine.begin() as conn:
        assert issues.get_issue(conn, "OIL-5").summary == "With bad source"
        assert linking.list_issue_sources(conn, "OIL-5") == []
    assert len(audit_entries(app, "issue.create")) == 1
    assert audit_entries(app, "source.link") == []
    page = client.get(response.headers["location"]).text
    box = re.search(r'<div class="error" role="alert">(.*?)</div>', page, re.DOTALL)
    assert box is not None
    assert "The issue was created, but the source could not be linked" in box.group(1)
    assert "no connector recognises &#39;no idea what this is&#39;" in box.group(1)


def test_the_link_error_is_shown_escaped_and_only_when_given(client: TestClient) -> None:
    plain = client.get("/issues/OIL-1").text
    shown = client.get("/issues/OIL-1", params={"link_error": "<b>boom</b>"}).text

    assert "could not be linked" not in plain
    assert "<b>boom</b>" not in shown
    assert "&lt;b&gt;boom&lt;/b&gt;" in shown


def test_creating_without_the_csrf_token_is_refused(app: FastAPI, post) -> None:
    before = issue_count(app)

    response = post("/issues", {"project": "OIL", "summary": "No token", "description": "x"}, csrf=False)

    assert response.status_code == 403
    assert issue_count(app) == before
