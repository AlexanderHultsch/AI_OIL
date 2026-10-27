import re
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_oil import timeutil
from ai_oil.domain import history, issues, people
from ai_oil.governance import audit

NOW = datetime(2026, 10, 28, 13, 0)
HTMX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: NOW.astimezone())


@pytest.fixture
def token(detail_app: FastAPI) -> str:
    return detail_app.state.csrf.token


@pytest.fixture
def post(detail_client: TestClient, token: str):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        headers = {**HTMX, **({"X-CSRF-Token": token} if csrf else {})}
        return detail_client.post(path, data=data or {}, headers=headers, follow_redirects=False)

    return send


@pytest.fixture
def get(detail_client: TestClient):
    def send(path: str):
        return detail_client.get(path, headers=HTMX)

    return send


def load(app: FastAPI, key: str) -> issues.Issue:
    with app.state.engine.begin() as conn:
        return issues.get_issue(conn, key)


def person_id(app: FastAPI, name: str) -> int:
    with app.state.engine.begin() as conn:
        return next(p.id for p in people.list_persons(conn, include_merged=True) if p.display_name == name)


def entries(app: FastAPI) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix="issue.")


def changes(app: FastAPI, key: str) -> list[tuple[str, str | None, str | None]]:
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, key)
        return [(e.field, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


def local(stamp: str) -> str:
    return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def side(page: str) -> str:
    match = re.search(r'<aside id="side-panel".*?</aside>', page, re.DOTALL)
    assert match is not None
    return match.group(0)


def side_rows(page: str) -> dict[str, str]:
    """Label to the plain text of its value in the side panel."""
    rows = re.findall(
        r'<span class="label">([^<]*)</span>\s*<span class="value[^"]*">(.*?)</span>', side(page), re.DOTALL
    )
    return {label: plain(value) for label, value in rows}


def selected(fragment: str) -> list[str]:
    return re.findall(r"<option[^>]* selected[^>]*>(.*?)</option>", fragment, re.DOTALL)


def options(fragment: str) -> list[str]:
    return re.findall(r"<option[^>]*>(.*?)</option>", fragment, re.DOTALL)


# --- the page (UI-20) -------------------------------------------------------------------------------


def test_the_page_renders_every_side_panel_field(detail_app: FastAPI, detail_client: TestClient) -> None:
    response = detail_client.get("/issues/OIL-2")
    issue = load(detail_app, "OIL-2")

    assert response.status_code == 200
    assert "<title>OIL-2 — AI_OIL</title>" in response.text
    rows = side_rows(response.text)
    assert rows == {
        "Blocked reason": "waiting for supplier",
        "Priority": "P2",
        "Type": "Task",
        "Assignee": "Alex",
        "Reporter": "Bea Brandt",
        "Due": "2026-10-20 (original 2026-10-15)",
        "Parent": "OIL-1",
        "Origin": "manual",
        "Created": local(issue.created_at),
        "Updated": local(issue.updated_at),
        "Resolved": "—",
    }
    assert '<a href="/issues/OIL-1">OIL-1</a>' in side(response.text)


def test_the_page_header_and_main_column(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-2").text

    assert re.search(r'<span class="issue-key muted">OIL-2</span>', page)
    assert "Power stage redesign" in re.search(r'id="field-summary".*?</div>', page, re.DOTALL).group(0)
    assert "Inductor lead time is too long" in page
    assert "Board passes EMC" in page
    match = re.search(r'<section class="panel" id="sources-panel">(.*?)</section>', page, re.DOTALL)
    assert match is not None
    assert "Coming soon" in match.group(1)
    assert '<a href="/issues" class="active" aria-current="page">Issues</a>' in page


def test_the_status_select_lists_the_current_status_and_its_transitions(detail_client: TestClient) -> None:
    for key, status in (("OIL-2", "Blocked"), ("OIL-3", "Done"), ("OIL-4", "New")):
        panel = side(detail_client.get(f"/issues/{key}").text)
        select = re.search(r"<select name=\"value\" aria-label=\"Status\".*?</select>", panel, re.DOTALL)
        assert select is not None
        assert 'hx-post="/issues/' + key + '/transition"' in select.group(0)
        assert options(select.group(0)) == [status, *issues.TRANSITIONS[status]]
        assert selected(select.group(0)) == [status]


def test_resolved_time_and_cancel_reason(detail_app: FastAPI, detail_client: TestClient) -> None:
    done = side_rows(detail_client.get("/issues/OIL-3").text)
    cancelled = side_rows(detail_client.get("/issues/OIL-5").text)

    assert done["Resolved"] == local(load(detail_app, "OIL-3").resolved_at)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", done["Resolved"])
    assert cancelled["Cancel reason"] == "descoped"
    assert "Blocked reason" not in cancelled
    assert "Cancel reason" not in side_rows(detail_client.get("/issues/OIL-2").text)


def test_overdue_due_dates_are_marked_unless_done(detail_client: TestClient) -> None:
    late = side(detail_client.get("/issues/OIL-6").text)
    done = side(detail_client.get("/issues/OIL-3").text)
    none = side(detail_client.get("/issues/OIL-4").text)

    assert '<span class="value overdue">2026-10-01</span>' in late
    assert "overdue" in side(detail_client.get("/issues/OIL-2").text)
    assert "overdue" not in done
    assert "overdue" not in none


def test_the_original_date_is_only_shown_when_it_differs(detail_client: TestClient) -> None:
    assert side_rows(detail_client.get("/issues/OIL-6").text)["Due"] == "2026-10-01"
    assert side_rows(detail_client.get("/issues/OIL-4").text)["Due"] == "—"


def test_an_issue_without_parent_assignee_or_criteria(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-4").text
    rows = side_rows(page)

    assert (rows["Parent"], rows["Assignee"]) == ("—", "Unassigned")
    placeholder = re.search(r'id="field-acceptance_criteria".*?</section>', page, re.DOTALL)
    assert placeholder is not None
    assert '<div class="value placeholder">No acceptance criteria</div>' in placeholder.group(0)
    assert "Acceptance criteria" in placeholder.group(0)


def test_description_and_summary_are_escaped_and_kept_verbatim(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-7").text

    assert "<script>alert(1)</script>" not in page
    assert '<div class="value text">&lt;script&gt;alert(1)&lt;/script&gt;\nsecond line</div>' in page
    assert "Escaping &lt;b&gt;" in page
    assert "<b>" not in page


def test_an_unknown_issue_is_a_404(detail_client: TestClient) -> None:
    response = detail_client.get("/issues/OIL-99")

    assert response.status_code == 404
    assert "OIL-99" in response.text
    assert "<html" in response.text


def test_the_navigator_routes_still_win_over_the_detail_page(detail_client: TestClient) -> None:
    assert detail_client.get("/issues/build", follow_redirects=False).status_code == 303


# --- editors (UI-26) --------------------------------------------------------------------------------


def editor_for(get, key: str, field: str) -> str:
    response = get(f"/issues/{key}/edit/{field}")
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<form")
    assert "<html" not in response.text
    assert f'id="field-{field}"' in response.text
    return response.text


@pytest.mark.parametrize("field", ["summary", "parent"])
def test_text_editors_carry_the_current_value(get, field: str) -> None:
    fragment = editor_for(get, "OIL-2", field)
    expected = {"summary": "Power stage redesign", "parent": "OIL-1"}[field]

    assert f'<input type="text" id="editor-{field}" name="value" value="{expected}"' in fragment


def test_the_summary_editor_is_limited_to_120_characters(get) -> None:
    assert 'maxlength="120"' in editor_for(get, "OIL-2", "summary")


def test_the_parent_editor_is_empty_without_a_parent(get) -> None:
    assert 'name="value" value=""' in editor_for(get, "OIL-4", "parent")


@pytest.mark.parametrize(
    ("field", "expected"),
    [("description", "Inductor lead time is too long"), ("acceptance_criteria", "Board passes EMC")],
)
def test_textarea_editors_carry_the_current_value(get, field: str, expected: str) -> None:
    fragment = editor_for(get, "OIL-2", field)

    assert re.search(rf'<textarea id="editor-{field}" name="value"[^>]*>{expected}</textarea>', fragment)


def test_the_acceptance_criteria_editor_is_empty_when_there_are_none(get) -> None:
    assert re.search(
        r'<textarea id="editor-acceptance_criteria"[^>]*></textarea>',
        editor_for(get, "OIL-4", "acceptance_criteria"),
    )


def test_the_editor_escapes_the_current_value(get) -> None:
    assert "Escaping &lt;b&gt;" in editor_for(get, "OIL-7", "summary")
    assert ">&lt;script&gt;alert(1)&lt;/script&gt;" in editor_for(get, "OIL-7", "description")


def test_priority_and_type_editors_are_selects(get) -> None:
    priority = editor_for(get, "OIL-2", "priority")
    issue_type = editor_for(get, "OIL-2", "issue_type")

    assert options(priority) == list(issues.PRIORITIES)
    assert selected(priority) == ["P2"]
    assert options(issue_type) == list(issues.ISSUE_TYPES)
    assert selected(issue_type) == ["Task"]


def test_assignee_and_reporter_editors_list_the_people_that_are_not_merged(detail_app: FastAPI, get) -> None:
    alex, bea = person_id(detail_app, "Alex"), person_id(detail_app, "Bea Brandt")
    assignee = editor_for(get, "OIL-2", "assignee")
    reporter = editor_for(get, "OIL-2", "reporter")
    unassigned = editor_for(get, "OIL-4", "assignee")

    assert options(assignee) == ["Unassigned", "Alex", "Bea Brandt"]
    assert f'<option value="{alex}" selected>' in assignee
    assert options(reporter) == ["Alex", "Bea Brandt"]
    assert f'<option value="{bea}" selected>' in reporter
    assert selected(unassigned) == ["Unassigned"]
    assert '<option value="" selected>' in unassigned


def test_the_due_date_editor_is_a_date_input(get) -> None:
    assert '<input type="date" id="editor-due_date" name="value" value="2026-10-20">' in editor_for(
        get, "OIL-2", "due_date"
    )
    assert 'type="date" id="editor-due_date" name="value" value=""' in editor_for(get, "OIL-4", "due_date")


@pytest.mark.parametrize(
    ("field", "target"),
    [
        ("summary", "#field-summary"),
        ("description", "#field-description"),
        ("acceptance_criteria", "#field-acceptance_criteria"),
        ("priority", "#side-panel"),
        ("issue_type", "#side-panel"),
        ("assignee", "#side-panel"),
        ("reporter", "#side-panel"),
        ("due_date", "#side-panel"),
        ("parent", "#side-panel"),
    ],
)
def test_save_and_cancel_buttons_target_the_right_container(get, field: str, target: str) -> None:
    fragment = editor_for(get, "OIL-2", field)

    assert (
        f'<button type="submit" class="primary" hx-post="/issues/OIL-2/field/{field}" '
        f'hx-target="{target}" hx-swap="outerHTML">Save</button>'
    ) in fragment
    assert (
        f'<button type="button" hx-get="/issues/OIL-2/view/{field}" '
        f'hx-target="#field-{field}" hx-swap="outerHTML">Cancel</button>'
    ) in fragment


def test_every_displayed_field_has_an_edit_button(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-2").text

    for field in ("summary", "description", "acceptance_criteria", "priority", "issue_type", "assignee"):
        assert f'hx-get="/issues/OIL-2/edit/{field}" hx-target="#field-{field}" hx-swap="outerHTML"' in page
    for field in ("reporter", "due_date", "parent"):
        assert f'hx-get="/issues/OIL-2/edit/{field}" hx-target="#field-{field}" hx-swap="outerHTML"' in page


@pytest.mark.parametrize("field", ["summary", "description", "acceptance_criteria"])
def test_cancel_returns_the_display_of_a_main_field(get, field: str) -> None:
    response = get(f"/issues/OIL-2/view/{field}")

    assert response.status_code == 200
    assert f'id="field-{field}"' in response.text
    assert f'hx-get="/issues/OIL-2/edit/{field}"' in response.text
    assert "<form" not in response.text
    assert "<aside" not in response.text


@pytest.mark.parametrize(
    ("field", "shown"),
    [
        ("priority", "P2"),
        ("issue_type", "Task"),
        ("assignee", "Alex"),
        ("reporter", "Bea Brandt"),
        ("due_date", "2026-10-20 (original 2026-10-15)"),
        ("parent", "OIL-1"),
    ],
)
def test_cancel_returns_the_display_of_a_side_field(get, field: str, shown: str) -> None:
    response = get(f"/issues/OIL-2/view/{field}")

    assert response.status_code == 200
    assert response.text.lstrip().startswith(f'<div class="field field-side" id="field-{field}">')
    assert plain(response.text).endswith(f"{shown} Edit")
    assert "<form" not in response.text
    assert "<aside" not in response.text


@pytest.mark.parametrize("route", ["edit", "view"])
@pytest.mark.parametrize("field", ["status", "assignee_id", "labels", "Summary", "due"])
def test_unknown_fields_are_a_404_for_editor_and_display(get, route: str, field: str) -> None:
    assert get(f"/issues/OIL-2/{route}/{field}").status_code == 404


def test_the_editor_of_an_unknown_issue_is_a_404(get) -> None:
    assert get("/issues/OIL-99/edit/summary").status_code == 404
    assert get("/issues/OIL-99/view/summary").status_code == 404


# --- saving fields (UI-26) --------------------------------------------------------------------------

MAIN_SAVES = [
    ("summary", "A better summary", "summary", "Plain item", "A better summary"),
    ("description", "A fuller description", "description", "Nothing special", "A fuller description"),
    ("acceptance_criteria", "Green build", "acceptance_criteria", None, "Green build"),
]


@pytest.mark.parametrize(("field", "value", "column", "old", "new"), MAIN_SAVES)
def test_saving_a_main_field_returns_its_display_and_records_history_and_audit(
    detail_app: FastAPI, post, field: str, value: str, column: str, old: str | None, new: str
) -> None:
    response = post(f"/issues/OIL-4/field/{field}", {"value": value})

    assert response.status_code == 200
    assert f'id="field-{field}"' in response.text
    assert new in response.text
    assert "<form" not in response.text
    assert "<aside" not in response.text
    assert getattr(load(detail_app, "OIL-4"), column) == new
    assert (field, old, new) in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app)
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue.edit",
        "B",
        "user",
        "OIL-4",
    )
    assert entry.detail == {field: value, "via": "web"}


def test_the_saved_description_is_escaped_in_the_returned_fragment(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/field/description", {"value": "<img src=x onerror=alert(1)>"})

    assert "<img" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text
    assert load(detail_app, "OIL-4").description == "<img src=x onerror=alert(1)>"


def test_clearing_the_acceptance_criteria_shows_the_placeholder(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/field/acceptance_criteria", {"value": "  "})

    assert response.status_code == 200
    assert "No acceptance criteria" in response.text
    assert load(detail_app, "OIL-2").acceptance_criteria is None
    assert ("acceptance_criteria", "Board passes EMC", None) in changes(detail_app, "OIL-2")


@pytest.mark.parametrize(
    ("field", "value", "old", "new", "row", "shown"),
    [
        ("priority", "P1", "P4", "P1", "Priority", "P1"),
        ("issue_type", "Epic", "Task", "Epic", "Type", "Epic"),
        ("due_date", "2026-12-01", None, "2026-12-01", "Due", "2026-12-01"),
        ("parent", "OIL-1", None, "OIL-1", "Parent", "OIL-1"),
    ],
)
def test_saving_a_side_field_returns_the_whole_side_panel(
    detail_app: FastAPI, post, field: str, value: str, old, new, row: str, shown: str
) -> None:
    key = "OIL-4"
    response = post(f"/issues/{key}/field/{field}", {"value": value})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<aside id="side-panel"')
    assert response.text.rstrip().endswith("</aside>")
    assert "<html" not in response.text
    assert side_rows(response.text)[row] == shown
    assert (field, old, new) in changes(detail_app, key)
    [entry] = entries(detail_app)
    assert entry.detail == {field: value, "via": "web"}
    assert (entry.action, entry.target) == ("issue.edit", key)


def test_the_first_due_date_sets_the_original_and_the_panel_shows_no_original(
    detail_app: FastAPI, post
) -> None:
    response = post("/issues/OIL-4/field/due_date", {"value": "2026-12-01"})

    issue = load(detail_app, "OIL-4")
    assert (issue.original_due_date, issue.revised_due_date) == (date(2026, 12, 1), date(2026, 12, 1))
    assert side_rows(response.text)["Due"] == "2026-12-01"
    assert ("original_due_date", None, "2026-12-01") in changes(detail_app, "OIL-4")


def test_a_later_due_date_shows_the_frozen_original(post) -> None:
    response = post("/issues/OIL-2/field/due_date", {"value": "2026-11-05"})

    assert side_rows(response.text)["Due"] == "2026-11-05 (original 2026-10-15)"
    assert "overdue" not in side(response.text)


def test_saving_the_assignee_and_reporter(detail_app: FastAPI, post) -> None:
    bea = person_id(detail_app, "Bea Brandt")
    alex = person_id(detail_app, "Alex")

    assigned = post("/issues/OIL-4/field/assignee", {"value": str(bea)})
    cleared = post("/issues/OIL-4/field/assignee", {"value": ""})
    reported = post("/issues/OIL-4/field/reporter", {"value": str(bea)})

    assert side_rows(assigned.text)["Assignee"] == "Bea Brandt"
    assert side_rows(cleared.text)["Assignee"] == "Unassigned"
    assert side_rows(reported.text)["Reporter"] == "Bea Brandt"
    issue = load(detail_app, "OIL-4")
    assert (issue.assignee_id, issue.reporter_id) == (None, bea)
    assert [(e[0], e[1], e[2]) for e in changes(detail_app, "OIL-4") if e[0] in ("assignee", "reporter")] == [
        ("assignee", None, str(bea)),
        ("assignee", str(bea), None),
        ("reporter", str(alex), str(bea)),
    ]
    assert [e.detail for e in reversed(entries(detail_app))] == [
        {"assignee": str(bea), "via": "web"},
        {"assignee": None, "via": "web"},
        {"reporter": str(bea), "via": "web"},
    ]


def test_clearing_the_parent_removes_it(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/field/parent", {"value": ""})

    assert response.status_code == 200
    assert side_rows(response.text)["Parent"] == "—"
    assert load(detail_app, "OIL-2").parent_key is None
    assert ("parent", "OIL-1", None) in changes(detail_app, "OIL-2")


def test_the_parent_key_is_case_insensitive(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/field/parent", {"value": " oil-1 "})

    assert load(detail_app, "OIL-4").parent_key == "OIL-1"


def test_a_save_is_one_change_set_and_the_actor_is_me(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/field/due_date", {"value": "2026-12-01"})

    with detail_app.state.engine.begin() as conn:
        me = people.get_me(conn)
        issue = issues.get_issue(conn, "OIL-4")
        saved = [
            e for e in history.issue_history(conn, issue.id) if e.field in ("due_date", "original_due_date")
        ]
    assert [e.field for e in saved] == ["due_date", "original_due_date"]
    assert len({e.change_set_id for e in saved}) == 1
    assert {(e.actor_type, e.actor_id) for e in saved} == {("user", me.id)}


def test_saving_an_unchanged_value_writes_neither_history_nor_audit(detail_app: FastAPI, post) -> None:
    before = changes(detail_app, "OIL-2")

    response = post("/issues/OIL-2/field/priority", {"value": "P2"})

    assert response.status_code == 200
    assert changes(detail_app, "OIL-2") == before
    assert entries(detail_app) == []


def test_a_parent_in_another_project_is_a_422_and_changes_nothing(detail_app: FastAPI, post) -> None:
    before = load(detail_app, "OIL-2")
    history_before = changes(detail_app, "OIL-2")

    response = post("/issues/OIL-2/field/parent", {"value": "ABC-1"})

    assert response.status_code == 422
    assert response.text.startswith('<div class="error" role="alert">')
    assert "belongs to another project" in response.text
    assert "<html" not in response.text
    assert load(detail_app, "OIL-2") == before
    assert changes(detail_app, "OIL-2") == history_before
    assert entries(detail_app) == []


def test_clearing_a_set_due_date_is_a_422(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/field/due_date", {"value": ""})

    assert response.status_code == 422
    assert "the due date cannot be removed once set" in response.text
    assert str(load(detail_app, "OIL-2").revised_due_date) == "2026-10-20"
    assert entries(detail_app) == []


@pytest.mark.parametrize(
    ("key", "field", "value", "message"),
    [
        ("OIL-4", "summary", "   ", "summary must be 1-120 characters"),
        ("OIL-4", "summary", "x" * 121, "summary must be 1-120 characters"),
        ("OIL-4", "description", "  ", "description must not be blank"),
        ("OIL-4", "priority", "P9", "priority must be one of"),
        ("OIL-4", "issue_type", "Story", "issue_type must be one of"),
        ("OIL-4", "issue_type", "Sub-task", "a Sub-task requires a Task as parent"),
        ("OIL-1", "issue_type", "Sub-task", "a Sub-task requires a Task as parent"),
        ("OIL-4", "assignee", "abc", "assignee must be a person id"),
        ("OIL-4", "reporter", "abc", "reporter must be a person id"),
        ("OIL-4", "reporter", "", "reporter_id must not be empty"),
        ("OIL-4", "due_date", "2026-13-45", "due must be a date"),
        ("OIL-4", "parent", "OIL-4", "an issue cannot be its own parent"),
        ("OIL-4", "parent", "OIL-2", "the parent of a Task must be an Epic"),
    ],
)
def test_domain_errors_are_a_422_fragment_and_change_nothing(
    detail_app: FastAPI, post, key: str, field: str, value: str, message: str
) -> None:
    before = load(detail_app, key)
    history_before = changes(detail_app, key)

    response = post(f"/issues/{key}/field/{field}", {"value": value})

    assert response.status_code == 422
    assert response.text.startswith('<div class="error" role="alert">')
    assert message in response.text
    assert load(detail_app, key) == before
    assert changes(detail_app, key) == history_before
    assert entries(detail_app) == []


def test_an_unknown_person_or_parent_is_a_404_and_changes_nothing(detail_app: FastAPI, post) -> None:
    before = load(detail_app, "OIL-4")

    assert post("/issues/OIL-4/field/assignee", {"value": "999"}).status_code == 404
    assert post("/issues/OIL-4/field/parent", {"value": "OIL-99"}).status_code == 404
    assert load(detail_app, "OIL-4") == before
    assert entries(detail_app) == []


@pytest.mark.parametrize("field", ["status", "assignee_id", "labels", "Summary", "due"])
def test_saving_an_unknown_field_is_a_404(detail_app: FastAPI, post, field: str) -> None:
    before = load(detail_app, "OIL-4")

    response = post(f"/issues/OIL-4/field/{field}", {"value": "x"})

    assert response.status_code == 404
    assert load(detail_app, "OIL-4") == before
    assert entries(detail_app) == []


def test_saving_for_an_unknown_issue_is_a_404(post) -> None:
    assert post("/issues/OIL-99/field/summary", {"value": "x"}).status_code == 404


def test_saving_without_the_csrf_token_is_refused(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/field/priority", {"value": "P1"}, csrf=False)

    assert response.status_code == 403
    assert load(detail_app, "OIL-4").priority == "P4"
    assert entries(detail_app) == []


def test_saving_is_a_post_only_endpoint(detail_client: TestClient) -> None:
    assert detail_client.get("/issues/OIL-4/field/priority").status_code == 405


def test_change_and_audit_entry_are_one_transaction(
    detail_app: FastAPI, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_oil.errors import ValidationError
    from ai_oil.web.routes import issue_detail

    def refuse(*args, **kwargs):
        raise ValidationError("audit unavailable")

    monkeypatch.setattr(issue_detail.audit, "log", refuse)

    response = post("/issues/OIL-4/field/priority", {"value": "P1"})

    assert response.status_code == 422
    assert load(detail_app, "OIL-4").priority == "P4"
    assert [c for c in changes(detail_app, "OIL-4") if c[0] == "priority"] == []


# --- status transitions (DM-16) ---------------------------------------------------------------------


def test_an_allowed_transition_returns_the_side_panel(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/transition", {"value": "In Progress"})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<aside id="side-panel"')
    assert selected(side(response.text)) == ["In Progress"]
    assert options(side(response.text))[:4] == ["In Progress", "Blocked", "Done", "Cancelled"]
    issue = load(detail_app, "OIL-4")
    assert issue.status == "In Progress"
    assert ("status", "New", "In Progress") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app)
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue.edit",
        "B",
        "user",
        "OIL-4",
    )
    assert entry.detail == {"status": "In Progress", "via": "web"}


def test_done_sets_the_resolved_time_and_reopening_clears_it(detail_app: FastAPI, post) -> None:
    done = post("/issues/OIL-4/transition", {"value": "Done"})
    resolved = load(detail_app, "OIL-4").resolved_at
    reopened = post("/issues/OIL-4/transition", {"value": "In Progress"})

    assert resolved is not None
    assert side_rows(done.text)["Resolved"] == local(resolved)
    assert side_rows(reopened.text)["Resolved"] == "—"
    assert load(detail_app, "OIL-4").resolved_at is None


@pytest.mark.parametrize(("target", "label"), [("Blocked", "Blocked reason"), ("Cancelled", "Cancel reason")])
def test_a_transition_that_needs_a_reason_asks_for_it_first(
    detail_app: FastAPI, post, target: str, label: str
) -> None:
    before = load(detail_app, "OIL-4")

    response = post("/issues/OIL-4/transition", {"value": target})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<aside id="side-panel"')
    form = re.search(r'<form class="inline-reason".*?</form>', response.text, re.DOTALL)
    assert form is not None
    assert 'hx-post="/issues/OIL-4/transition"' in form.group(0)
    assert f'<input type="hidden" name="value" value="{target}">' in form.group(0)
    assert 'name="reason"' in form.group(0)
    assert ">Set</button>" in form.group(0)
    assert 'hx-get="/issues/OIL-4/side"' in form.group(0)
    assert ">Cancel</button>" in form.group(0)
    assert '<select name="value" aria-label="Status"' not in response.text
    assert load(detail_app, "OIL-4") == before
    assert entries(detail_app) == []
    assert label not in side_rows(response.text)


@pytest.mark.parametrize(
    ("target", "label", "column"),
    [("Blocked", "Blocked reason", "blocked_reason"), ("Cancelled", "Cancel reason", "cancel_reason")],
)
def test_a_transition_with_a_reason_succeeds_and_audits_the_reason(
    detail_app: FastAPI, post, target: str, label: str, column: str
) -> None:
    response = post("/issues/OIL-4/transition", {"value": target, "reason": "  waiting for tooling  "})

    assert response.status_code == 200
    assert side_rows(response.text)[label] == "waiting for tooling"
    assert "inline-reason" not in response.text
    issue = load(detail_app, "OIL-4")
    assert (issue.status, getattr(issue, column)) == (target, "waiting for tooling")
    assert (column, None, "waiting for tooling") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app)
    assert entry.detail == {"status": target, "reason": "waiting for tooling", "via": "web"}


def test_a_blank_reason_asks_again(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/transition", {"value": "Blocked", "reason": "   "})

    assert response.status_code == 200
    assert 'class="inline-reason"' in response.text
    assert load(detail_app, "OIL-4").status == "New"
    assert entries(detail_app) == []


def test_leaving_blocked_clears_the_reason(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/transition", {"value": "In Progress"})

    assert "Blocked reason" not in side_rows(response.text)
    assert load(detail_app, "OIL-2").blocked_reason is None
    assert ("blocked_reason", "waiting for supplier", None) in changes(detail_app, "OIL-2")


def test_the_side_endpoint_renders_the_stored_panel_without_the_reason_form(post, get) -> None:
    post("/issues/OIL-4/transition", {"value": "Blocked"})

    response = get("/issues/OIL-4/side")

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<aside id="side-panel"')
    assert "inline-reason" not in response.text
    assert selected(response.text)[0] == "New"


@pytest.mark.parametrize(
    ("key", "target", "data", "message"),
    [
        ("OIL-4", "New", {}, "cannot move OIL-4 from New to New"),
        ("OIL-3", "Blocked", {"reason": "because"}, "cannot move OIL-3 from Done to Blocked"),
        ("OIL-3", "Blocked", {}, "cannot move OIL-3 from Done to Blocked"),
        ("OIL-5", "Done", {}, "cannot move OIL-5 from Cancelled to Done"),
        ("OIL-4", "Bogus", {}, "status must be one of"),
        ("OIL-4", "", {}, "status must be one of"),
    ],
)
def test_disallowed_transitions_are_a_422_fragment_and_change_nothing(
    detail_app: FastAPI, post, key: str, target: str, data: dict, message: str
) -> None:
    before = load(detail_app, key)
    history_before = changes(detail_app, key)

    response = post(f"/issues/{key}/transition", {"value": target, **data})

    assert response.status_code == 422
    assert response.text.startswith('<div class="error" role="alert">')
    assert message in response.text
    assert "<html" not in response.text
    assert load(detail_app, key) == before
    assert changes(detail_app, key) == history_before
    assert entries(detail_app) == []


def test_a_transition_of_an_unknown_issue_is_a_404(post) -> None:
    assert post("/issues/OIL-99/transition", {"value": "Done"}).status_code == 404


def test_a_transition_without_the_csrf_token_is_refused(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/transition", {"value": "Done"}, csrf=False)

    assert response.status_code == 403
    assert load(detail_app, "OIL-4").status == "New"
    assert entries(detail_app) == []


def test_a_transition_is_a_post_only_endpoint(detail_client: TestClient) -> None:
    assert detail_client.get("/issues/OIL-4/transition").status_code == 405


def test_a_domain_error_without_htmx_is_a_full_error_page(detail_client: TestClient, token: str) -> None:
    response = detail_client.post(
        "/issues/OIL-4/transition", data={"value": "New"}, headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 422
    assert "<html" in response.text
    assert "cannot move OIL-4 from New to New" in response.text
