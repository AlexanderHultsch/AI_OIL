import html as htmllib
import re
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from ai_oil import timeutil
from ai_oil.domain import issues, people
from ai_oil.search import saved_filters
from ai_oil.web.routes import issues as navigator

NOW = datetime(2026, 10, 28, 13, 0)  # naive local time; the fixture's due dates are relative to it


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: NOW.astimezone())


def local(iso: str) -> str:
    return timeutil.parse_iso(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def plain_header(cell: str) -> str:
    """The header text without its sort link and arrow (UI-13)."""
    return re.sub(r"\s*[▲▼]$", "", re.sub(r"<[^>]+>", "", cell).strip())


def plain_cell(cell: str) -> str:
    """The shown value of an inline-edit cell (UI-12): the selected option or the date input's value."""
    selected = re.search(r"<option[^>]* selected[^>]*>(.*?)</option>", cell, re.DOTALL)
    if selected is not None:
        return selected.group(1)
    date_input = re.search(r'<input type="date"[^>]* value="([^"]*)"', cell)
    if date_input is not None:
        return date_input.group(1) or "—"
    return cell


def table(page: str) -> tuple[list[str], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Headers, then per issue key the inner HTML and the tag attributes of every cell by header.

    Sort links in the headers and the inline-edit controls in the cells are reduced to what they show.
    """
    head = re.search(r"<thead>(.*?)</thead>", page, re.DOTALL)
    assert head is not None, "no table"
    headers = [plain_header(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", head.group(1), re.DOTALL)]
    cells: dict[str, dict[str, str]] = {}
    attributes: dict[str, dict[str, str]] = {}
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.DOTALL)
    assert body is not None
    for row in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.DOTALL):
        found = re.findall(r"<td([^>]*)>(.*?)</td>", row, re.DOTALL)
        assert len(found) == len(headers)
        key = re.search(r'href="/issues/([A-Z]+-\d+)"', row)
        assert key is not None
        cells[key.group(1)] = {h: plain_cell(c[1].strip()) for h, c in zip(headers, found, strict=True)}
        attributes[key.group(1)] = {h: c[0].strip() for h, c in zip(headers, found, strict=True)}
    return headers, cells, attributes


def keys(page: str) -> list[str]:
    return list(table(page)[1])


def query_value(page: str) -> str:
    match = re.search(r'<input type="text" name="q" value="([^"]*)"', page)
    assert match is not None
    return htmllib.unescape(match.group(1))


def test_default_page_shows_open_issues_only(nav_client: TestClient) -> None:
    response = nav_client.get("/issues")

    assert response.status_code == 200
    assert set(keys(response.text)) == {"OIL-1", "OIL-2", "OIL-3", "OIL-5"}
    assert "OIL-4" not in response.text
    assert query_value(response.text) == "statusCategory != Done ORDER BY updated DESC"
    assert "All open issues" in response.text
    assert "4 issues" in response.text
    assert 'class="oql-error"' not in response.text


def test_default_columns_and_headers(nav_client: TestClient) -> None:
    headers, _, _ = table(nav_client.get("/issues").text)

    assert headers == [
        "Key",
        "Type",
        "Summary",
        "Status",
        "Priority",
        "Assignee",
        "Due",
        "Updated",
        "Sources",
        "Source changed",
    ]


def test_oql_query(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "priority = P1"})

    assert keys(response.text) == ["OIL-2"]
    assert query_value(response.text) == "priority = P1"
    assert "1 issue<" in response.text
    assert "All open issues" not in response.text


def test_empty_result(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "priority = P5"})

    assert response.status_code == 200
    assert keys(response.text) == []
    assert "0 issues" in response.text


def test_plain_text_becomes_a_text_query(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "inductor lead time"})

    assert response.status_code == 200
    assert query_value(response.text) == 'text ~ "inductor lead time"'
    assert keys(response.text) == ["OIL-1"]
    assert 'class="oql-error"' not in response.text


def test_plain_text_with_quotes_and_backslashes_is_escaped(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": 'say "zebra" \\ now'})

    assert response.status_code == 200
    assert query_value(response.text) == 'text ~ "say \\"zebra\\" \\\\ now"'
    assert 'class="oql-error"' not in response.text


def test_single_plain_word(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "zebra"})

    assert query_value(response.text) == 'text ~ "zebra"'
    assert keys(response.text) == ["OIL-5"]


def test_oql_that_parses_is_not_treated_as_text(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": 'text ~ "gerber"'})

    assert query_value(response.text) == 'text ~ "gerber"'
    assert keys(response.text) == ["OIL-3"]


@pytest.mark.parametrize(
    "query",
    ["status = Blokced", "priority > ", "foo ~ bar", "status in", "a or b", "x != y", "ORDER BY"],
)
def test_oql_errors_show_the_caret_block_and_no_table(nav_client: TestClient, query: str) -> None:
    response = nav_client.get("/issues", params={"q": query})

    assert response.status_code == 200
    assert '<pre class="oql-error">' in response.text
    assert "error:" in response.text
    assert "<table" not in response.text
    assert "issues</p>" not in response.text
    assert query_value(response.text) == query.strip()


def test_oql_error_block_content(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "status = Blokced"})

    block = re.search(r'<pre class="oql-error">(.*?)</pre>', response.text, re.DOTALL)
    assert block is not None
    lines = htmllib.unescape(block.group(1)).split("\n")
    assert lines[0].startswith("error: status 'Blokced' — did you mean 'Blocked'?")
    assert lines[1] == "status = Blokced"
    assert lines[2] == " " * 9 + "^"


def test_filter_uses_the_filters_query_and_columns(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "status", "labels"])

    response = nav_client.get("/issues", params={"filter": created.id})

    headers, cells, _ = table(response.text)
    assert headers == ["Key", "Status", "Labels"]
    assert list(cells) == ["OIL-1"]
    assert cells["OIL-1"]["Labels"] == "b-sample, supplier"
    assert query_value(response.text) == "assignee = me()"
    assert "Mine" in response.text


def test_built_in_filter_by_id(nav_app: FastAPI, nav_client: TestClient) -> None:
    nav_client.get("/issues")
    with nav_app.state.engine.begin() as conn:
        blocked = saved_filters.get_filter(conn, "Blocked")

    response = nav_client.get(f"/issues?filter={blocked.id}")

    assert keys(response.text) == ["OIL-2"]
    assert query_value(response.text) == "status = Blocked ORDER BY updated DESC"


def test_unknown_filter_is_a_404_page(nav_client: TestClient) -> None:
    response = nav_client.get("/issues?filter=999")

    assert response.status_code == 404
    assert 'class="error"' in response.text


def test_filter_wins_over_q(nav_client: TestClient) -> None:
    nav_client.get("/issues")  # creates the built-ins
    blocked = nav_client.get("/filters").text
    filter_id = re.search(r'href="/issues\?filter=(\d+)">Blocked<', blocked)
    assert filter_id is not None

    response = nav_client.get("/issues", params={"filter": filter_id.group(1), "q": "priority = P5"})

    assert query_value(response.text) == "status = Blocked ORDER BY updated DESC"


def test_columns_override(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"columns": "summary, key,bogus,reporter,summary"})

    headers, cells, _ = table(response.text)
    assert headers == ["Summary", "Key", "Reporter"]
    assert cells["OIL-1"]["Reporter"] == "Alex"


def test_columns_override_with_only_unknown_ids_falls_back(nav_client: TestClient) -> None:
    headers, _, _ = table(nav_client.get("/issues", params={"columns": "bogus"}).text)

    assert headers[0] == "Key"
    assert len(headers) == 10


def test_columns_override_wins_over_the_filters_columns(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        created = saved_filters.create_filter(conn, "Mine", "assignee = me()", ["key", "status"])

    response = nav_client.get("/issues", params={"filter": created.id, "columns": "key,created"})

    assert table(response.text)[0] == ["Key", "Created"]


def test_cells(nav_client: TestClient) -> None:
    _, cells, _ = table(nav_client.get("/issues").text)

    first = cells["OIL-1"]
    assert first["Key"] == '<a href="/issues/OIL-1">OIL-1</a>'
    assert first["Summary"] == '<a href="/issues/OIL-1">Power stage redesign</a>'
    assert (first["Type"], first["Status"], first["Priority"]) == ("Task", "New", "P2")
    assert first["Assignee"] == "Alex"
    assert first["Due"] == "2026-10-15"
    assert cells["OIL-2"]["Assignee"] == "Bea Brandt"
    assert cells["OIL-3"]["Assignee"] == "—"
    assert cells["OIL-5"]["Due"] == "—"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", first["Updated"])


def test_updated_and_created_are_shown_in_local_time(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        conn.execute(
            text("UPDATE issue SET created_at = :c, updated_at = :u WHERE key = 'OIL-3'"),
            {"c": "2026-01-02T03:04:05.000Z", "u": "2026-10-27T23:30:00.000Z"},
        )

    _, cells, _ = table(nav_client.get("/issues", params={"columns": "key,updated,created"}).text)

    assert cells["OIL-3"]["Updated"] == local("2026-10-27T23:30:00.000Z")
    assert cells["OIL-3"]["Created"] == local("2026-01-02T03:04:05.000Z")


def test_reporter_and_labels_columns(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "key = OIL-4", "columns": "key,reporter,labels"})

    _, cells, _ = table(response.text)
    assert cells["OIL-4"]["Reporter"] == "Bea Brandt"
    assert cells["OIL-4"]["Labels"] == ""


def test_overdue_class_on_open_issues_only(nav_client: TestClient) -> None:
    response = nav_client.get("/issues", params={"q": "key IN (OIL-1, OIL-3, OIL-4) ORDER BY key"})

    _, cells, attributes = table(response.text)
    assert cells["OIL-1"]["Due"] == "2026-10-15"
    assert attributes["OIL-1"]["Due"] == 'class="overdue"'
    assert attributes["OIL-3"]["Due"] == ""  # due in the future
    assert cells["OIL-4"]["Due"] == "2026-10-01"
    assert attributes["OIL-4"]["Due"] == ""  # past due but Done


def test_due_today_is_not_overdue(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        conn.execute(text("UPDATE issue SET revised_due_date = '2026-10-28' WHERE key = 'OIL-3'"))

    _, _, attributes = table(nav_client.get("/issues", params={"q": "key = OIL-3"}).text)

    assert attributes["OIL-3"]["Due"] == ""


def test_source_columns(nav_client: TestClient) -> None:
    response = nav_client.get("/issues")

    _, cells, _ = table(response.text)
    assert cells["OIL-1"]["Sources"] == "2"
    assert cells["OIL-1"]["Source changed"].startswith(local("2026-10-27T08:30:00.000Z"))
    assert cells["OIL-3"]["Sources"] == "0"
    assert cells["OIL-3"]["Source changed"] == "—"
    assert cells["OIL-5"]["Source changed"] == "—"


def test_fetch_error_marker(nav_client: TestClient) -> None:
    _, cells, _ = table(nav_client.get("/issues").text)

    # OIL-1: a linked object is in error; OIL-2: only a child (attachment) of the linked thread is
    assert (
        '<span class="fetch-error" title="1 source with fetch errors">⚠</span>'
        in cells["OIL-1"]["Source changed"]
    )
    assert 'class="fetch-error"' in cells["OIL-2"]["Source changed"]
    assert cells["OIL-2"]["Sources"] == "1"
    assert cells["OIL-2"]["Source changed"].startswith(local("2026-10-23T09:00:00.000Z"))
    assert "fetch-error" not in cells["OIL-3"]["Source changed"]


def test_fetch_error_marker_counts_sources(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        conn.execute(text("UPDATE source_object SET fetch_status = 'error'"))

    _, cells, _ = table(nav_client.get("/issues").text)

    assert 'title="2 sources with fetch errors"' in cells["OIL-1"]["Source changed"]


def test_removed_links_are_ignored(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        conn.execute(text("UPDATE issue_source_link SET removed_at = '2026-10-28T00:00:00.000Z'"))

    _, cells, _ = table(nav_client.get("/issues").text)

    assert cells["OIL-1"]["Sources"] == "0"
    assert cells["OIL-1"]["Source changed"] == "—"


def test_truncation_note(nav_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(navigator, "MAX_ROWS", 3)

    response = nav_client.get("/issues")

    assert len(keys(response.text)) == 3
    assert "Showing the first 3 issues — refine the query." in response.text
    assert "3 issues" in response.text


def test_no_truncation_note_at_the_limit(nav_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(navigator, "MAX_ROWS", 4)

    response = nav_client.get("/issues")

    assert len(keys(response.text)) == 4
    assert "Showing the first" not in response.text


def test_the_default_limit_is_500() -> None:
    assert navigator.MAX_ROWS == 500


def test_more_issues_than_the_limit_are_cut_at_500(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        me = people.get_me(conn)
        for number in range(520):
            issues.create_issue(conn, "OIL", f"Bulk {number}", "Bulk issue", actor_id=me.id)

    response = nav_client.get("/issues")

    assert len(keys(response.text)) == 500
    assert "Showing the first 500 issues — refine the query." in response.text


def test_html_is_escaped(nav_app: FastAPI, nav_client: TestClient) -> None:
    with nav_app.state.engine.begin() as conn:
        conn.execute(text("UPDATE issue SET summary = '<b>x</b>' WHERE key = 'OIL-3'"))

    page = nav_client.get("/issues", params={"q": 'summary ~ "<b>"'}).text

    assert "<b>x</b>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page
    assert 'value="summary ~ &#34;&lt;b&gt;&#34;"' in page


def test_issues_page_marks_navigation_active(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    assert '<a href="/issues" class="active" aria-current="page">Issues</a>' in page


def test_filters_page(nav_client: TestClient, nav_app: FastAPI) -> None:
    with nav_app.state.engine.begin() as conn:
        saved_filters.create_filter(conn, "Aardvark", "priority = P1")

    response = nav_client.get("/filters")

    assert response.status_code == 200
    assert '<a href="/filters" class="active" aria-current="page">Filters</a>' in response.text
    links = re.findall(r'<a href="/issues\?filter=(\d+)">([^<]+)</a>', response.text)
    assert [name for _, name in links] == [*saved_filters.BUILT_INS, "Aardvark"]
    assert response.text.count('<span class="badge">built-in</span>') == 6
    for query in saved_filters.BUILT_INS.values():
        assert f"<code>{htmllib.escape(query)}</code>" in response.text
    assert "<code>priority = P1</code>" in response.text
    for filter_id, _ in links:
        assert nav_client.get(f"/issues?filter={filter_id}").status_code == 200


def test_filters_page_links_work_for_every_built_in(nav_client: TestClient) -> None:
    page = nav_client.get("/filters").text

    for filter_id, name in re.findall(r'<a href="/issues\?filter=(\d+)">([^<]+)</a>', page):
        response = nav_client.get(f"/issues?filter={filter_id}")
        assert response.status_code == 200, name
        assert 'class="oql-error"' not in response.text, name


def test_my_open_issues_and_overdue_filters(nav_client: TestClient) -> None:
    page = nav_client.get("/filters").text
    ids = {name: fid for fid, name in re.findall(r'<a href="/issues\?filter=(\d+)">([^<]+)</a>', page)}

    mine = nav_client.get(f"/issues?filter={ids['My open issues']}")
    overdue = nav_client.get(f"/issues?filter={ids['Overdue']}")

    assert keys(mine.text) == ["OIL-1"]
    assert keys(overdue.text) == ["OIL-1", "OIL-2"]


def test_query_count_does_not_grow_with_the_number_of_rows(nav_app: FastAPI, nav_client: TestClient) -> None:
    statements: list[str] = []

    @event.listens_for(nav_app.state.engine, "before_cursor_execute")
    def count(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    def measure() -> tuple[int, int]:
        statements.clear()
        response = nav_client.get("/issues", params={"columns": ",".join(saved_filters.COLUMNS)})
        assert response.status_code == 200
        return len(statements), len(keys(response.text))

    measure()  # warm-up: the built-in filters are inserted on the first request
    few_queries, few_rows = measure()

    with nav_app.state.engine.begin() as conn:
        me = people.get_me(conn)
        bea = people.create_person(conn, "Carl Conrad")
        source_ids = list(conn.execute(text("SELECT id FROM source_object")).scalars())
        for number in range(30):
            issue = issues.create_issue(
                conn,
                "OIL",
                f"More {number}",
                "More",
                actor_id=me.id,
                assignee_id=bea.id if number % 2 else me.id,
                reporter_id=bea.id,
                due_date=date(2026, 10, 1),
                labels=[f"l{number}"],
            )
            conn.execute(
                text(
                    "INSERT INTO issue_source_link (issue_id, source_object_id, link_method)"
                    " VALUES (:issue, :source, 'manual')"
                ),
                {"issue": issue.id, "source": source_ids[number % len(source_ids)]},
            )
    many_queries, many_rows = measure()

    assert few_rows == 4
    assert many_rows == 34
    assert many_queries == few_queries
