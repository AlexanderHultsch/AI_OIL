import html
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_oil import timeutil
from ai_oil.domain import history, issues, people
from ai_oil.governance import audit
from ai_oil.search import oql, saved_filters
from ai_oil.web.routes import issues as navigator

NOW = datetime(2026, 10, 28, 13, 0)
HTMX = {"HX-Request": "true"}
DEFAULT_QUERY = "statusCategory != Done ORDER BY updated DESC"


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: NOW.astimezone())


@pytest.fixture
def token(nav_app: FastAPI) -> str:
    return nav_app.state.csrf.token


@pytest.fixture
def post(nav_client: TestClient, token: str):
    def send(path: str, data: dict | None = None, *, htmx: bool = True, csrf: bool = True):
        headers = {**(HTMX if htmx else {}), **({"X-CSRF-Token": token} if csrf else {})}
        return nav_client.post(path, data=data or {}, headers=headers, follow_redirects=False)

    return send


def location_query(response) -> str:
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/issues"
    [query] = parse_qs(location.query)["q"]
    return query


def plain_header(cell: str) -> str:
    return re.sub(r"\s*[▲▼]$", "", re.sub(r"<[^>]+>", "", cell).strip())


def keys(page: str) -> list[str]:
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.DOTALL)
    assert body is not None
    return re.findall(r'<td class="key"><a href="/issues/([A-Z]+-\d+)"', body.group(1))


def header_cells(page: str) -> list[str]:
    head = re.search(r"<thead>(.*?)</thead>", page, re.DOTALL)
    assert head is not None
    return re.findall(r"<th>(.*?)</th>", head.group(1), re.DOTALL)


def sort_links(page: str) -> dict[str, str]:
    """Header text to the OQL its sort link points at."""
    links = {}
    for cell in header_cells(page):
        match = re.search(r'<a class="sort" href="([^"]+)">([^<]+)</a>', cell)
        if match:
            href = match.group(1).replace("&amp;", "&")
            links[match.group(2)] = parse_qs(urlsplit(href).query)["q"][0]
    return links


def selected(fragment: str) -> list[str]:
    return re.findall(r"<option[^>]* selected[^>]*>(.*?)</option>", fragment, re.DOTALL)


def issue_row(nav_app: FastAPI, key: str) -> issues.Issue:
    with nav_app.state.engine.begin() as conn:
        return issues.get_issue(conn, key)


def person_id(nav_app: FastAPI, name: str) -> int:
    with nav_app.state.engine.begin() as conn:
        return next(p.id for p in people.list_persons(conn) if p.display_name == name)


def person_id_first(nav_app: FastAPI, name: str) -> int:
    with nav_app.state.engine.begin() as conn:
        return min(p.id for p in people.list_persons(conn) if p.display_name == name)


def entries(nav_app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with nav_app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def changes(nav_app: FastAPI, key: str) -> list[tuple[str, str | None, str | None]]:
    with nav_app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, key)
        return [(e.field, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


# --- quick-filter chips (UI-11) ---------------------------------------------------------------------


def test_no_chip_chosen_gives_the_all_open_issues_query(nav_client: TestClient) -> None:
    response = nav_client.get("/issues/build", follow_redirects=False)

    assert location_query(response) == DEFAULT_QUERY
    assert response.headers["location"] == "/issues?q=statusCategory+%21%3D+Done+ORDER+BY+updated+DESC"


def test_empty_chip_values_count_as_not_chosen(nav_client: TestClient) -> None:
    response = nav_client.get(
        "/issues/build",
        params={"project": "", "status": "", "assignee": "", "priority": "", "label": ""},
        follow_redirects=False,
    )

    assert location_query(response) == DEFAULT_QUERY


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"project": "OIL"}, "project = OIL ORDER BY updated DESC"),
        ({"status": "Blocked"}, 'status = "Blocked" ORDER BY updated DESC'),
        (
            {"status": ["New", "In Progress"]},
            'status IN ("New", "In Progress") ORDER BY updated DESC',
        ),
        ({"status": ["New", "New"]}, 'status = "New" ORDER BY updated DESC'),
        ({"assignee": "@me"}, "assignee = me() ORDER BY updated DESC"),
        ({"assignee": "@unassigned"}, "assignee IS EMPTY ORDER BY updated DESC"),
        ({"assignee": "2"}, "assignee = 2 ORDER BY updated DESC"),
        ({"assignee": "Bea Brandt"}, 'assignee = "Bea Brandt" ORDER BY updated DESC'),
        ({"priority": ["P1", "P2"]}, "priority IN (P1, P2) ORDER BY updated DESC"),
        ({"priority": "P1"}, "priority = P1 ORDER BY updated DESC"),
        ({"label": "supplier"}, 'label = "supplier" ORDER BY updated DESC'),
        ({"label": "b-sample"}, 'label = "b-sample" ORDER BY updated DESC'),
        (
            {
                "project": "OIL",
                "status": ["New", "Blocked"],
                "assignee": "@me",
                "priority": ["P1", "P2"],
                "label": "supplier",
            },
            'project = OIL AND status IN ("New", "Blocked") AND assignee = me()'
            ' AND priority IN (P1, P2) AND label = "supplier" ORDER BY updated DESC',
        ),
        (
            {"project": "OIL", "priority": "P2"},
            "project = OIL AND priority = P2 ORDER BY updated DESC",
        ),
    ],
)
def test_chips_compose_oql(nav_client: TestClient, params: dict, expected: str) -> None:
    response = nav_client.get("/issues/build", params=params, follow_redirects=False)

    assert location_query(response) == expected
    oql.parse(expected)  # and it is valid OQL


def test_chip_values_are_quoted_not_interpreted(nav_client: TestClient) -> None:
    hostile = 'x" OR status = Done OR label = "y'
    response = nav_client.get(
        "/issues/build",
        params={"label": hostile, "assignee": 'O"Brien \\ Jr', "status": 'a"b'},
        follow_redirects=False,
    )

    query = location_query(response)
    parsed = oql.parse(query)
    assert query == (
        'status = "a\\"b" AND assignee = "O\\"Brien \\\\ Jr"'
        ' AND label = "x\\" OR status = Done OR label = \\"y" ORDER BY updated DESC'
    )
    assert isinstance(parsed.where, oql.And)
    assert [clause.field for clause in parsed.where.items] == ["status", "assignee", "label"]
    assert parsed.where.items[2].values[0].text == hostile


def test_a_chip_value_that_is_not_a_valid_word_is_quoted(nav_client: TestClient) -> None:
    response = nav_client.get("/issues/build", params={"project": "my project"}, follow_redirects=False)

    assert location_query(response) == 'project = "my project" ORDER BY updated DESC'


def test_following_the_redirect_runs_the_composed_query(nav_app: FastAPI, nav_client: TestClient) -> None:
    bea = person_id(nav_app, "Bea Brandt")
    response = nav_client.get(
        "/issues/build", params={"assignee": str(bea), "priority": "P1"}, follow_redirects=True
    )

    assert response.status_code == 200
    assert keys(response.text) == ["OIL-2"]
    assert 'class="oql-error"' not in response.text
    value = re.search(r'<input type="text" name="q" value="([^"]*)"', response.text)
    assert value is not None
    assert value.group(1) == f"assignee = {bea} AND priority = P1 ORDER BY updated DESC"


def test_two_persons_with_the_same_name_are_told_apart_by_id(
    nav_app: FastAPI, nav_client: TestClient
) -> None:
    with nav_app.state.engine.begin() as conn:
        twin = people.create_person(conn, "Bea Brandt")
        issues.update_issue(conn, "OIL-3", {"assignee_id": twin.id}, actor_id=people.get_me(conn).id)
    bea = person_id_first(nav_app, "Bea Brandt")
    page = nav_client.get("/issues").text

    chips = page[page.index('class="chips"') : page.index('class="oql-form"')]
    assert re.findall(r'<option value="(\d+)">Bea Brandt</option>', chips) == [str(bea), str(twin.id)]
    first = nav_client.get("/issues/build", params={"assignee": str(bea)}, follow_redirects=True)
    second = nav_client.get("/issues/build", params={"assignee": str(twin.id)}, follow_redirects=True)
    by_name = nav_client.get("/issues", params={"q": 'assignee = "Bea Brandt"'})

    assert 'class="oql-error"' not in first.text
    assert 'class="oql-error"' not in second.text
    assert sorted(keys(first.text)) == ["OIL-2"]
    assert keys(second.text) == ["OIL-3"]
    assert 'class="oql-error"' in by_name.text  # the name alone is ambiguous now


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"assignee": "@me"}, ["OIL-1"]),
        ({"assignee": "@unassigned"}, ["OIL-3", "OIL-4", "OIL-5"]),
        ({"label": "supplier"}, ["OIL-1"]),
        ({"status": ["Blocked", "In Progress"]}, ["OIL-2"]),
        ({"project": "OIL", "priority": ["P1", "P2"]}, ["OIL-2", "OIL-1"]),
        ({"status": "Done"}, ["OIL-4"]),
    ],
)
def test_composed_queries_find_the_right_issues(nav_client: TestClient, params: dict, expected: list) -> None:
    response = nav_client.get("/issues/build", params=params, follow_redirects=True)

    assert 'class="oql-error"' not in response.text
    assert sorted(keys(response.text)) == sorted(expected)


def test_the_page_offers_chips_and_keeps_the_oql_input_editable(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    chips = re.search(
        r'<form class="chips" action="/issues/build" method="get"[^>]*>(.*?)</form>', page, re.DOTALL
    )
    assert chips is not None
    form = chips.group(1)
    names = re.findall(r'<select name="(\w+)"', form)
    assert names == ["project", "status", "assignee", "priority", "label"]
    assert re.findall(r'<select name="status"[^>]* multiple', form)
    assert re.findall(r'<select name="priority"[^>]* multiple', form)
    options = {
        name: re.findall(r'<option value="([^"]*)"', block)
        for name, block in re.findall(r'<select name="(\w+)"[^>]*>(.*?)</select>', form, re.DOTALL)
    }
    assert options["project"] == ["", "OIL"]
    assert options["status"] == list(issues.STATUSES)
    assert options["assignee"] == ["", "@me", "@unassigned", "1", "2"]
    assert re.findall(r'<option value="\d+">([^<]*)</option>', form) == ["Alex", "Bea Brandt"]
    assert options["priority"] == list(issues.PRIORITIES)
    assert options["label"] == ["", "b-sample", "supplier"]
    oql_input = re.search(r'<input type="text" name="q" value="[^"]*"[^>]*>', page)
    assert oql_input is not None
    assert "readonly" not in oql_input.group(0)
    assert "disabled" not in oql_input.group(0)
    assert page.index('class="chips"') < page.index('class="oql-form"')


def test_archived_projects_are_not_offered_as_a_chip(nav_app: FastAPI, nav_client: TestClient) -> None:
    from ai_oil.domain import projects

    with nav_app.state.engine.begin() as conn:
        projects.create_project(conn, "OLD", "Retired")
        projects.update_project(conn, "OLD", archived=True)

    assert 'value="OLD"' not in nav_client.get("/issues").text


# --- sort by header (UI-13) -------------------------------------------------------------------------


def test_sortable_headers_are_links_to_the_reordered_effective_query(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    links = sort_links(page)
    assert list(links) == ["Key", "Type", "Summary", "Status", "Priority", "Assignee", "Due", "Updated"]
    assert links["Key"] == "statusCategory != Done ORDER BY key"
    assert links["Due"] == "statusCategory != Done ORDER BY due"
    assert links["Status"] == "statusCategory != Done ORDER BY status"
    for query in links.values():
        oql.parse(query)


def test_the_first_sort_column_shows_its_arrow_and_toggles(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    updated = [cell for cell in header_cells(page) if "Updated" in cell][0]
    assert "▼" in updated
    assert sort_links(page)["Updated"] == "statusCategory != Done ORDER BY updated"
    assert page.count("▼") == 1
    assert page.count("▲") == 0

    ascending = nav_client.get("/issues", params={"q": sort_links(page)["Updated"]}).text
    updated = [cell for cell in header_cells(ascending) if "Updated" in cell][0]
    assert "▲" in updated
    assert sort_links(ascending)["Updated"] == "statusCategory != Done ORDER BY updated DESC"
    assert ascending.count("▲") == 1
    assert ascending.count("▼") == 0


def test_other_columns_start_ascending_and_replace_the_ordering(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "status = New ORDER BY priority DESC, due"}).text

    links = sort_links(page)
    assert links["Priority"] == "status = New ORDER BY priority"
    assert links["Due"] == "status = New ORDER BY due"
    assert links["Key"] == "status = New ORDER BY key"
    assert "▼" in [cell for cell in header_cells(page) if "Priority" in cell][0]
    assert "▲" not in page


def test_only_the_first_order_item_counts(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "ORDER BY priority, due DESC"}).text

    assert sort_links(page)["Priority"] == "ORDER BY priority DESC"
    assert sort_links(page)["Due"] == "ORDER BY due"
    assert page.count("▲") == 1
    assert page.count("▼") == 0


def test_a_query_without_ordering_shows_no_arrow(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "priority = P1"}).text

    assert "▲" not in page
    assert "▼" not in page
    assert sort_links(page)["Updated"] == "priority = P1 ORDER BY updated"


@pytest.mark.parametrize("column", navigator.SORTABLE_COLUMNS)
def test_clicking_each_sortable_header_sorts_the_table(nav_client: TestClient, column: str) -> None:
    all_columns = ",".join(saved_filters.COLUMNS)
    page = nav_client.get("/issues", params={"columns": all_columns}).text
    label = saved_filters.COLUMNS[column]

    ascending = nav_client.get("/issues", params={"q": sort_links(page)[label], "columns": all_columns})
    descending_link = sort_links(ascending.text)[label]
    descending = nav_client.get("/issues", params={"q": descending_link, "columns": all_columns})

    assert 'class="oql-error"' not in ascending.text
    assert 'class="oql-error"' not in descending.text
    assert descending_link.endswith(f"ORDER BY {column} DESC")
    assert (
        sorted(keys(ascending.text)) == sorted(keys(descending.text)) == ["OIL-1", "OIL-2", "OIL-3", "OIL-5"]
    )
    assert "▲" in [cell for cell in header_cells(ascending.text) if label in cell][0]
    assert "▼" in [cell for cell in header_cells(descending.text) if label in cell][0]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("statusCategory != Done ORDER BY key", ["OIL-1", "OIL-2", "OIL-3", "OIL-5"]),
        ("statusCategory != Done ORDER BY key DESC", ["OIL-5", "OIL-3", "OIL-2", "OIL-1"]),
        ("statusCategory != Done ORDER BY summary", ["OIL-5", "OIL-1", "OIL-2", "OIL-3"]),
        ("statusCategory != Done ORDER BY summary DESC", ["OIL-3", "OIL-2", "OIL-1", "OIL-5"]),
        ("statusCategory != Done ORDER BY due", ["OIL-1", "OIL-2", "OIL-3", "OIL-5"]),
        ("statusCategory != Done ORDER BY priority", ["OIL-2", "OIL-1", "OIL-5", "OIL-3"]),
        ("statusCategory != Done ORDER BY assignee", ["OIL-1", "OIL-2", "OIL-5", "OIL-3"]),
    ],
)
def test_following_a_sort_link_orders_the_rows(nav_client: TestClient, query: str, expected: list) -> None:
    assert keys(nav_client.get("/issues", params={"q": query}).text) == expected


def test_the_columns_param_is_kept_in_sort_links(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"columns": "key,status,due"}).text

    hrefs = re.findall(r'<a class="sort" href="([^"]+)"', page)
    assert len(hrefs) == 3
    for href in hrefs:
        query = parse_qs(urlsplit(href.replace("&amp;", "&")).query)
        assert query["columns"] == ["key,status,due"]


def test_no_columns_param_in_sort_links_when_none_was_given(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    for href in re.findall(r'<a class="sort" href="([^"]+)"', page):
        assert "columns" not in parse_qs(urlsplit(href.replace("&amp;", "&")).query)


def test_sort_links_keep_the_columns_of_an_active_filter(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(
            conn, "Mine", "priority IN (P1, P2)", ["summary", "priority", "due"]
        )

    page = nav_client.get("/issues", params={"filter": created.id}).text

    assert list(sort_links(page)) == ["Summary", "Priority", "Due"]
    for href in re.findall(r'<a class="sort" href="([^"]+)"', page):
        assert parse_qs(urlsplit(href.replace("&amp;", "&")).query)["columns"] == ["summary,priority,due"]
    followed = nav_client.get(
        "/issues", params={"q": sort_links(page)["Due"], "columns": "summary,priority,due"}
    )
    assert [plain_header(h) for h in header_cells(followed.text)] == ["Summary", "Priority", "Due"]
    assert "▲" in [cell for cell in header_cells(followed.text) if "Due" in cell][0]
    assert followed.text.count("<tr") == 1 + 2  # header and the two P1/P2 issues


def test_sort_links_follow_href_with_the_filters_columns(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "priority IN (P1, P2)", ["key", "due"])
    page = nav_client.get("/issues", params={"filter": created.id}).text
    href = re.search(r'<a class="sort" href="([^"]+)">Due</a>', page)
    assert href is not None

    followed = nav_client.get(href.group(1).replace("&amp;", "&"))

    assert [plain_header(h) for h in header_cells(followed.text)] == ["Key", "Due"]


def test_labels_sources_and_source_changed_are_plain_headers(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"columns": "labels,sources,source_changed,key"}).text

    assert header_cells(page)[:3] == ["Labels", "Sources", "Source changed"]
    assert header_cells(page)[3].startswith('<a class="sort"')


def test_sort_links_use_the_filters_query(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "due"])

    page = nav_client.get("/issues", params={"filter": created.id}).text

    assert sort_links(page) == {"Key": "assignee = me() ORDER BY key", "Due": "assignee = me() ORDER BY due"}


def test_sort_links_follow_a_plain_text_search(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "zebra"}).text

    assert sort_links(page)["Key"] == 'text ~ "zebra" ORDER BY key'


def test_a_query_that_does_not_parse_shows_no_table_and_no_links(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "status = Blokced"}).text

    assert 'class="sort"' not in page
    assert "<table" not in page


# --- inline edits (UI-12) ---------------------------------------------------------------------------


def test_rows_carry_inline_controls(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "key = OIL-1"}).text

    status = re.search(r'<select name="value" aria-label="Status"([^>]*)>(.*?)</select>', page, re.DOTALL)
    assert status is not None
    assert 'hx-post="/issues/OIL-1/inline/status"' in status.group(1)
    assert 'hx-trigger="change"' in status.group(1)
    assert 'hx-target="closest tr"' in status.group(1)
    assert 'hx-swap="outerHTML"' in status.group(1)
    assert re.findall(r'<option value="([^"]*)"( selected)?>', status.group(2)) == [
        ("New", " selected"),
        ("Ready to Proceed", ""),
        ("In Progress", ""),
        ("Blocked", ""),
        ("Done", ""),
        ("Cancelled", ""),
    ]
    priority = re.search(r'<select name="value" aria-label="Priority"([^>]*)>(.*?)</select>', page, re.DOTALL)
    assert priority is not None
    assert 'hx-post="/issues/OIL-1/inline/priority"' in priority.group(1)
    assert re.findall(r'<option value="([^"]*)"( selected)?>', priority.group(2)) == [
        ("P1", ""),
        ("P2", " selected"),
        ("P3", ""),
        ("P4", ""),
        ("P5", ""),
    ]
    assignee = re.search(r'<select name="value" aria-label="Assignee"([^>]*)>(.*?)</select>', page, re.DOTALL)
    assert assignee is not None
    assert 'hx-post="/issues/OIL-1/inline/assignee"' in assignee.group(1)
    assert [m[1] for m in re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', assignee.group(2))] == [
        "—",
        "Alex",
        "Bea Brandt",
    ]
    assert selected(assignee.group(2)) == ["Alex"]
    due = re.search(r'<input type="date" name="value" value="2026-10-15"([^>]*)>', page)
    assert due is not None
    assert 'hx-post="/issues/OIL-1/inline/due"' in due.group(1)
    assert 'hx-trigger="change"' in due.group(1)
    assert "hx-vals" in due.group(1)


def test_closed_issues_offer_only_reopening(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "key = OIL-4"}).text

    status = re.search(r'<select name="value" aria-label="Status"[^>]*>(.*?)</select>', page, re.DOTALL)
    assert status is not None
    assert re.findall(r'<option value="([^"]*)"', status.group(1)) == ["Done", "In Progress"]


def test_the_row_is_split_into_its_own_template(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    assert page.count("<tr") == 1 + 4  # header and four open issues
    for field in navigator.INLINE_FIELDS:
        assert page.count(f'hx-post="/issues/OIL-3/inline/{field}"') == 1


def test_changing_the_status_returns_the_row_and_records_history_and_audit(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-1/inline/status", {"value": "In Progress", "columns": "key,status,due"})

    assert response.status_code == 200
    assert response.text.lstrip().startswith("<tr")
    assert response.text.rstrip().endswith("</tr>")
    assert "<html" not in response.text
    assert selected(response.text) == ["In Progress"]
    assert response.text.count("<td") == 3
    assert 'hx-post="/issues/OIL-1/inline/status"' in response.text
    assert issue_row(nav_app, "OIL-1").status == "In Progress"
    assert ("status", "New", "In Progress") in changes(nav_app, "OIL-1")
    [entry] = entries(nav_app, "issue.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue.edit",
        "B",
        "user",
        "OIL-1",
    )
    assert entry.detail == {"status": "In Progress", "via": "web"}


def test_the_history_actor_is_me(nav_app: FastAPI, post) -> None:
    post("/issues/OIL-1/inline/priority", {"value": "P4"})

    with nav_app.state.engine.begin() as conn:
        me = people.get_me(conn)
        issue = issues.get_issue(conn, "OIL-1")
        latest = history.issue_history(conn, issue.id)[-1]
    assert (latest.actor_type, latest.actor_id) == ("user", me.id)
    assert (latest.field, latest.old_value, latest.new_value) == ("priority", "P2", "P4")


def test_the_returned_row_uses_the_posted_columns(post) -> None:
    default = post("/issues/OIL-1/inline/priority", {"value": "P3"})
    chosen = post("/issues/OIL-1/inline/priority", {"value": "P3", "columns": "summary,priority"})

    assert default.text.count("<td") == 10
    assert chosen.text.count("<td") == 2
    assert 'hx-vals=\'{"columns": "summary,priority"}\'' in chosen.text
    assert selected(chosen.text) == ["P3"]


def test_priority_edit(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/inline/priority", {"value": "P5", "columns": "priority"})

    assert response.status_code == 200
    assert selected(response.text) == ["P5"]
    assert issue_row(nav_app, "OIL-2").priority == "P5"
    assert changes(nav_app, "OIL-2")[-1] == ("priority", "P1", "P5")
    assert entries(nav_app, "issue.")[0].detail == {"priority": "P5", "via": "web"}


def test_assignee_edit_and_unassign(nav_app: FastAPI, post) -> None:
    bea = person_id(nav_app, "Bea Brandt")

    response = post("/issues/OIL-3/inline/assignee", {"value": str(bea), "columns": "assignee"})

    assert selected(response.text) == ["Bea Brandt"]
    assert issue_row(nav_app, "OIL-3").assignee_id == bea
    assert changes(nav_app, "OIL-3")[-1] == ("assignee", None, str(bea))

    cleared = post("/issues/OIL-3/inline/assignee", {"value": "", "columns": "assignee"})

    assert cleared.status_code == 200
    assert selected(cleared.text) == ["—"]
    assert issue_row(nav_app, "OIL-3").assignee_id is None
    assert [e.detail for e in entries(nav_app, "issue.")] == [
        {"assignee": None, "via": "web"},
        {"assignee": str(bea), "via": "web"},
    ]


def test_assigning_a_merged_person_in_the_table_stores_and_audits_the_survivor(
    nav_app: FastAPI, post
) -> None:
    bea = person_id(nav_app, "Bea Brandt")
    with nav_app.state.engine.begin() as conn:
        twin = people.create_person(conn, "Bea B.")
        people.merge_persons(conn, twin.id, bea, actor_id=people.get_me(conn).id)

    response = post("/issues/OIL-3/inline/assignee", {"value": str(twin.id), "columns": "assignee"})

    assert response.status_code == 200
    assert issue_row(nav_app, "OIL-3").assignee_id == bea
    assert changes(nav_app, "OIL-3")[-1] == ("assignee", None, str(bea))
    assert entries(nav_app, "issue.")[0].detail == {"assignee": str(bea), "via": "web"}


def test_due_date_edit(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-3/inline/due", {"value": "2026-12-01"})

    assert response.status_code == 200
    assert 'value="2026-12-01"' in response.text
    assert issue_row(nav_app, "OIL-3").revised_due_date.isoformat() == "2026-12-01"
    assert changes(nav_app, "OIL-3")[-1] == ("due_date", "2026-11-25", "2026-12-01")
    assert entries(nav_app, "issue.")[0].detail == {"due": "2026-12-01", "via": "web"}


def test_setting_a_first_due_date(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-5/inline/due", {"value": "2026-11-30"})

    assert response.status_code == 200
    assert issue_row(nav_app, "OIL-5").revised_due_date.isoformat() == "2026-11-30"
    assert issue_row(nav_app, "OIL-5").original_due_date.isoformat() == "2026-11-30"


def test_an_overdue_due_date_is_marked_in_the_returned_row(post) -> None:
    overdue = post("/issues/OIL-3/inline/due", {"value": "2026-10-01"})
    later = post("/issues/OIL-3/inline/due", {"value": "2026-11-30"})

    assert '<td class="overdue">' in overdue.text
    assert "overdue" not in later.text


def test_an_edit_that_changes_nothing_writes_nothing(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-1/inline/priority", {"value": "P2", "columns": "priority"})

    assert response.status_code == 200
    assert selected(response.text) == ["P2"]
    assert entries(nav_app, "issue.") == []
    assert [c for c in changes(nav_app, "OIL-1") if c[0] == "priority"] == []


@pytest.mark.parametrize("target", ["Blocked", "Cancelled"])
def test_status_needing_a_reason_returns_the_reason_form_and_changes_nothing(
    nav_app: FastAPI, post, target: str
) -> None:
    before = issue_row(nav_app, "OIL-1")

    response = post("/issues/OIL-1/inline/status", {"value": target, "columns": "key,status"})

    assert response.status_code == 200
    assert response.text.lstrip().startswith("<tr")
    form = re.search(r"<form([^>]*)>(.*?)</form>", response.text, re.DOTALL)
    assert form is not None
    assert 'hx-post="/issues/OIL-1/inline/status"' in form.group(1)
    assert 'hx-target="closest tr"' in form.group(1)
    assert 'hx-swap="outerHTML"' in form.group(1)
    assert f'<input type="hidden" name="value" value="{target}">' in form.group(2)
    assert '<input type="hidden" name="columns" value="key,status">' in form.group(2)
    assert re.search(r'<input type="text" name="reason"', form.group(2))
    assert re.search(r'<button type="submit">Set</button>', form.group(2))
    assert "<select" not in response.text
    assert issue_row(nav_app, "OIL-1") == before
    assert entries(nav_app, "issue.") == []


def test_posting_the_reason_applies_the_status(nav_app: FastAPI, post) -> None:
    post("/issues/OIL-1/inline/status", {"value": "Blocked"})

    response = post(
        "/issues/OIL-1/inline/status",
        {"value": "Blocked", "reason": " waiting for PCB ", "columns": "status"},
    )

    assert response.status_code == 200
    assert selected(response.text) == ["Blocked"]
    assert "<form" not in response.text
    issue = issue_row(nav_app, "OIL-1")
    assert (issue.status, issue.blocked_reason) == ("Blocked", "waiting for PCB")
    [entry] = entries(nav_app, "issue.")
    assert entry.detail == {"status": "Blocked", "reason": "waiting for PCB", "via": "web"}
    assert ("status", "New", "Blocked") in changes(nav_app, "OIL-1")


def test_cancelling_with_a_reason(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-3/inline/status", {"value": "Cancelled", "reason": "descoped"})

    assert response.status_code == 200
    issue = issue_row(nav_app, "OIL-3")
    assert (issue.status, issue.cancel_reason) == ("Cancelled", "descoped")


def test_a_blank_reason_shows_the_form_again(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-1/inline/status", {"value": "Blocked", "reason": "   "})

    assert response.status_code == 200
    assert 'class="inline-reason"' in response.text
    assert issue_row(nav_app, "OIL-1").status == "New"


def test_a_reason_is_not_needed_for_other_statuses(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-2/inline/status", {"value": "In Progress"})

    assert response.status_code == 200
    assert issue_row(nav_app, "OIL-2").status == "In Progress"
    assert issue_row(nav_app, "OIL-2").blocked_reason is None


@pytest.mark.parametrize(
    ("key", "field", "data", "message"),
    [
        ("OIL-4", "status", {"value": "New"}, "cannot move OIL-4 from Done to New"),
        ("OIL-4", "status", {"value": "Blocked"}, "cannot move OIL-4 from Done to Blocked"),
        (
            "OIL-2",
            "status",
            {"value": "Blocked", "reason": "again"},
            "cannot move OIL-2 from Blocked to Blocked",
        ),
        ("OIL-1", "status", {"value": "Bogus"}, "status must be one of"),
        ("OIL-1", "priority", {"value": "P9"}, "priority must be one of"),
        ("OIL-1", "assignee", {"value": "abc"}, "assignee must be a person id"),
        ("OIL-1", "due", {"value": ""}, "the due date cannot be removed once set"),
        ("OIL-1", "due", {"value": "2026-13-45"}, "due must be a date"),
        ("OIL-1", "due", {"value": "tomorrow"}, "due must be a date"),
    ],
)
def test_domain_errors_are_a_422_fragment_and_change_nothing(
    nav_app: FastAPI, post, key: str, field: str, data: dict, message: str
) -> None:
    before = issue_row(nav_app, key)
    history_before = changes(nav_app, key)

    response = post(f"/issues/{key}/inline/{field}", data)

    assert response.status_code == 422
    assert response.text.startswith('<div class="error" role="alert">')
    assert message in response.text
    assert "<html" not in response.text
    assert issue_row(nav_app, key) == before
    assert changes(nav_app, key) == history_before
    assert entries(nav_app, "issue.") == []


def test_a_domain_error_without_htmx_is_a_full_error_page(post) -> None:
    response = post("/issues/OIL-1/inline/due", {"value": ""}, htmx=False)

    assert response.status_code == 422
    assert "<html" in response.text
    assert "the due date cannot be removed" in response.text


def test_an_unknown_person_is_a_404_and_changes_nothing(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-1/inline/assignee", {"value": "999"})

    assert response.status_code == 404
    assert issue_row(nav_app, "OIL-1").assignee_id == person_id(nav_app, "Alex")
    assert entries(nav_app, "issue.") == []


@pytest.mark.parametrize("field", ["summary", "labels", "assignee_id", "due_date", "Status", ""])
def test_unknown_inline_fields_are_a_404(nav_app: FastAPI, post, field: str) -> None:
    before = issue_row(nav_app, "OIL-1")

    response = post(f"/issues/OIL-1/inline/{field}", {"value": "x"})

    assert response.status_code == 404
    assert issue_row(nav_app, "OIL-1") == before


def test_an_unknown_issue_is_a_404(post) -> None:
    response = post("/issues/OIL-99/inline/priority", {"value": "P1"})

    assert response.status_code == 404
    assert "OIL-99" in response.text


def test_inline_edit_without_the_csrf_token_is_refused(nav_app: FastAPI, post) -> None:
    response = post("/issues/OIL-1/inline/priority", {"value": "P5"}, csrf=False)

    assert response.status_code == 403
    assert issue_row(nav_app, "OIL-1").priority == "P2"
    assert entries(nav_app, "issue.") == []


def test_change_and_audit_entry_are_one_transaction(
    nav_app: FastAPI, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_oil.errors import ValidationError

    def refuse(*args, **kwargs):
        raise ValidationError("audit unavailable")

    monkeypatch.setattr(navigator.audit, "log", refuse)

    response = post("/issues/OIL-1/inline/priority", {"value": "P5"})

    assert response.status_code == 422
    assert issue_row(nav_app, "OIL-1").priority == "P2"
    assert [c for c in changes(nav_app, "OIL-1") if c[0] == "priority"] == []


def test_inline_edit_is_a_post_only_endpoint(nav_client: TestClient) -> None:
    assert nav_client.get("/issues/OIL-1/inline/priority").status_code == 405


def test_the_edited_row_stays_in_sync_with_the_page(nav_app: FastAPI, nav_client: TestClient, post) -> None:
    post("/issues/OIL-3/inline/priority", {"value": "P1"})

    page = nav_client.get("/issues", params={"q": "priority = P1 ORDER BY key"}).text

    assert keys(page) == ["OIL-2", "OIL-3"]


# --- saving filters and column sets (UI-13, SRCH-8) -------------------------------------------------


def test_the_navigator_offers_save_as_filter(nav_client: TestClient) -> None:
    page = nav_client.get("/issues", params={"q": "priority = P1", "columns": "key,status"}).text

    form = re.search(
        r'<form class="save-filter" action="/filters" method="post">(.*?)</form>', page, re.DOTALL
    )
    assert form is not None
    assert re.search(r'<input type="hidden" name="csrf_token" value="[^"]+">', form.group(1))
    assert '<input type="hidden" name="query" value="priority = P1">' in form.group(1)
    assert '<input type="hidden" name="columns" value="key,status">' in form.group(1)
    assert re.search(r'<input type="text" name="name"', form.group(1))
    assert "Save as filter" in form.group(1)


def test_save_as_filter_is_not_offered_when_the_query_is_invalid(nav_client: TestClient) -> None:
    assert "save-filter" not in nav_client.get("/issues", params={"q": "status = Blokced"}).text


def test_saving_a_filter(nav_app: FastAPI, post) -> None:
    response = post("/filters", {"name": "P1 items", "query": "priority = P1", "columns": "key, status ,due"})

    with nav_app.state.engine.begin() as conn:
        created = saved_filters.get_filter(conn, "P1 items")
    assert response.status_code == 303
    assert response.headers["location"] == f"/issues?filter={created.id}"
    assert (created.query, created.columns, created.built_in) == (
        "priority = P1",
        ("key", "status", "due"),
        False,
    )
    [entry] = entries(nav_app, "filter.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "filter.create",
        "B",
        "user",
        str(created.id),
    )
    assert entry.detail == {
        "name": "P1 items",
        "query": "priority = P1",
        "columns": ["key", "status", "due"],
        "via": "web",
    }


def test_a_saved_filter_opens_with_its_query_and_columns(nav_client: TestClient, post) -> None:
    response = post("/filters", {"name": "Mine", "query": "assignee = me()", "columns": "key,summary"})

    page = nav_client.get(response.headers["location"])

    assert keys(page.text) == ["OIL-1"]
    assert header_cells(page.text)[0].startswith('<a class="sort"')
    assert "Mine" in page.text


def test_saving_without_columns_uses_the_defaults(nav_app: FastAPI, post) -> None:
    post("/filters", {"name": "Plain", "query": "priority = P1"})

    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, "Plain").columns == saved_filters.DEFAULT_COLUMNS


def test_saving_the_navigators_current_state_round_trips(
    nav_app: FastAPI, nav_client: TestClient, post
) -> None:
    page = nav_client.get("/issues", params={"q": "zebra", "columns": "key,summary"}).text
    query = re.search(r'<input type="hidden" name="query" value="([^"]*)"', page)
    columns = re.search(r'<input type="hidden" name="columns" value="([^"]*)"', page)
    assert query is not None
    assert columns is not None
    response = post(
        "/filters", {"name": "Zebra", "query": html.unescape(query.group(1)), "columns": columns.group(1)}
    )

    with nav_app.state.engine.begin() as conn:
        saved = saved_filters.get_filter(conn, "Zebra")
    assert response.status_code == 303
    assert saved.query == 'text ~ "zebra"'
    assert saved.columns == ("key", "summary")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "", "query": "priority = P1"}, "must not be blank"),
        ({"name": "   ", "query": "priority = P1"}, "must not be blank"),
        ({"name": "Bad", "query": "priority ="}, "expected a value"),
        ({"name": "Bad", "query": "priority = P1", "columns": "key,bogus"}, "unknown column 'bogus'"),
        ({"name": "Blocked", "query": "priority = P1"}, "already exists"),
        ({"name": "Dup", "query": "priority = P1"}, None),
    ],
)
def test_saving_a_filter_can_fail(nav_app: FastAPI, post, data: dict, message: str | None) -> None:
    post("/filters", {"name": "Dup", "query": "priority = P2"})
    with nav_app.state.engine.begin() as conn:
        count = len(saved_filters.list_filters(conn))
    entries_before = len(entries(nav_app, "filter."))

    response = post("/filters", data)

    assert response.status_code == 422
    assert response.text.startswith('<div class="error"')
    if message is not None:
        assert message in html.unescape(response.text)
    with nav_app.state.engine.begin() as conn:
        assert len(saved_filters.list_filters(conn)) == count
    assert len(entries(nav_app, "filter.")) == entries_before


def test_saving_a_filter_without_csrf_is_refused(nav_app: FastAPI, post) -> None:
    response = post("/filters", {"name": "X", "query": "priority = P1"}, csrf=False)

    assert response.status_code == 403
    with nav_app.state.engine.begin() as conn:
        assert "X" not in [f.name for f in saved_filters.list_filters(conn)]


def test_the_form_field_token_works_for_plain_forms(
    nav_app: FastAPI, nav_client: TestClient, token: str
) -> None:
    response = nav_client.post(
        "/filters",
        data={"name": "Form", "query": "priority = P1", "csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_the_filters_page_offers_delete_for_custom_filters_only(nav_client: TestClient, post) -> None:
    created = post("/filters", {"name": "Mine", "query": "priority = P1"})
    filter_id = created.headers["location"].rsplit("=", 1)[1]

    page = nav_client.get("/filters").text

    assert re.findall(r'<form action="/filters/(\d+)/delete" method="post">', page) == [filter_id]
    assert page.count("Delete</button>") == 1
    assert re.search(r'<input type="hidden" name="csrf_token" value="[^"]+">', page)


def test_deleting_a_filter(nav_app: FastAPI, post) -> None:
    created = post("/filters", {"name": "Mine", "query": "priority = P1"})
    filter_id = created.headers["location"].rsplit("=", 1)[1]

    response = post(f"/filters/{filter_id}/delete")

    assert response.status_code == 303
    assert response.headers["location"] == "/filters"
    with nav_app.state.engine.begin() as conn:
        assert "Mine" not in [f.name for f in saved_filters.list_filters(conn)]
    deleted = entries(nav_app, "filter.delete")
    assert len(deleted) == 1
    assert (deleted[0].action_class, deleted[0].actor_type, deleted[0].target) == ("B", "user", filter_id)
    assert deleted[0].detail == {"name": "Mine", "via": "web"}


def test_a_built_in_filter_cannot_be_deleted(nav_app: FastAPI, nav_client: TestClient, post) -> None:
    nav_client.get("/issues")
    with nav_app.state.engine.begin() as conn:
        built_in = saved_filters.get_filter(conn, "Overdue")

    response = post(f"/filters/{built_in.id}/delete", htmx=False)

    assert response.status_code == 422
    assert "cannot be deleted" in response.text
    assert "<html" in response.text
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, "Overdue").id == built_in.id
    assert entries(nav_app, "filter.") == []


def test_deleting_an_unknown_filter_is_a_404(post) -> None:
    assert post("/filters/999/delete").status_code == 404


def test_deleting_without_csrf_is_refused(nav_app: FastAPI, post) -> None:
    created = post("/filters", {"name": "Mine", "query": "priority = P1"})
    filter_id = created.headers["location"].rsplit("=", 1)[1]

    assert post(f"/filters/{filter_id}/delete", csrf=False).status_code == 403
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, "Mine").id == int(filter_id)


def test_the_column_chooser_appears_with_an_active_filter(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "status", "labels"])

    page = nav_client.get("/issues", params={"filter": created.id}).text

    chooser = re.search(r'<details class="columns-chooser">(.*?)</details>', page, re.DOTALL)
    assert chooser is not None
    boxes = re.findall(r'<input type="checkbox" name="columns" value="(\w+)"( checked)?>', chooser.group(1))
    assert [box[0] for box in boxes] == list(saved_filters.COLUMNS)
    assert [box[0] for box in boxes if box[1]] == ["key", "status", "labels"]
    assert f'<input type="hidden" name="filter" value="{created.id}">' in chooser.group(1)
    assert re.search(r'<form action="/issues" method="get">', chooser.group(1))
    assert re.search(r'<button type="submit">Apply</button>', chooser.group(1))
    save = re.search(
        r'<button type="button" hx-post="([^"]+)" hx-include="closest form">Save to filter</button>',
        chooser.group(1),
    )
    assert save is not None
    assert save.group(1) == f"/filters/{created.id}/columns"


def test_the_column_chooser_reflects_a_columns_override(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "status"])

    page = nav_client.get("/issues", params={"filter": created.id, "columns": "summary,due"}).text

    checked = re.findall(r'<input type="checkbox" name="columns" value="(\w+)" checked>', page)
    assert checked == ["summary", "due"]


def test_no_column_chooser_without_an_active_filter(nav_client: TestClient) -> None:
    assert "columns-chooser" not in nav_client.get("/issues", params={"q": "priority = P1"}).text


def test_apply_sends_the_checked_columns_to_the_navigator(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()")

    response = nav_client.get(f"/issues?filter={created.id}&columns=key&columns=reporter&columns=due")

    assert [re.sub(r"<[^>]+>|\s*[▲▼]", "", h).strip() for h in header_cells(response.text)] == [
        "Key",
        "Reporter",
        "Due",
    ]


def test_saving_the_column_set_to_a_filter(nav_app: FastAPI, post) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()")

    response = post(f"/filters/{created.id}/columns", {"columns": "key,status,due"}, htmx=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/issues?filter={created.id}"
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, created.id).columns == ("key", "status", "due")
        assert saved_filters.get_filter(conn, created.id).query == "assignee = me()"
    [entry] = entries(nav_app, "filter.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "filter.update",
        "B",
        "user",
        str(created.id),
    )
    assert entry.detail == {"name": "Mine", "columns": ["key", "status", "due"], "via": "web"}


def test_saving_checked_boxes_sends_repeated_values(nav_app: FastAPI, post) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()")

    response = post(f"/filters/{created.id}/columns", {"columns": ["summary", "key", "labels"]}, htmx=False)

    assert response.status_code == 303
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, created.id).columns == ("summary", "key", "labels")


def test_htmx_saving_of_columns_redirects_through_a_header(nav_app: FastAPI, post) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()")

    response = post(f"/filters/{created.id}/columns", {"columns": ["key", "due"]})

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/issues?filter={created.id}"
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, created.id).columns == ("key", "due")


def test_a_built_in_filters_columns_can_be_saved(nav_app: FastAPI, nav_client: TestClient, post) -> None:
    nav_client.get("/issues")
    with nav_app.state.engine.begin() as conn:
        built_in = saved_filters.get_filter(conn, "Blocked")

    response = post(f"/filters/{built_in.id}/columns", {"columns": "key,status"}, htmx=False)

    assert response.status_code == 303
    with nav_app.state.engine.begin() as conn:
        changed = saved_filters.get_filter(conn, "Blocked")
    assert changed.columns == ("key", "status")
    assert changed.query == built_in.query


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "choose at least one column"),
        ({"columns": ""}, "choose at least one column"),
        ({"columns": " , "}, "choose at least one column"),
        ({"columns": "key,bogus"}, "unknown column 'bogus'"),
    ],
)
def test_saving_bad_columns_fails_and_changes_nothing(
    nav_app: FastAPI, post, data: dict, message: str
) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "due"])

    response = post(f"/filters/{created.id}/columns", data)

    assert response.status_code == 422
    assert message in html.unescape(response.text)
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, created.id).columns == ("key", "due")
    assert entries(nav_app, "filter.") == []


def test_saving_columns_of_an_unknown_filter_is_a_404(post) -> None:
    assert post("/filters/999/columns", {"columns": "key"}).status_code == 404


def test_saving_columns_without_csrf_is_refused(nav_app: FastAPI, post) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "due"])

    assert post(f"/filters/{created.id}/columns", {"columns": "key"}, csrf=False).status_code == 403
    with nav_app.state.engine.begin() as conn:
        assert saved_filters.get_filter(conn, created.id).columns == ("key", "due")
