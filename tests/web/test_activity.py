import re
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.connectors import manual
from ai_oil.domain import comments, people
from ai_oil.sources import linking, store
from ai_oil.sources.model import EntryData, IdentityRef, ObjectMeta, VersionData
from ai_oil.web.routes import issue_detail

HTMX = {"HX-Request": "true"}
NOW = datetime(2026, 10, 28, 13, 0)
BEA = IdentityRef("email", "bea@acme.com", "Bea Brandt")
CARL = IdentityRef("email", "carl@acme.com", "Carl Conrad")
ALEX = IdentityRef("email", "alex@example.com", "Alex")


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: NOW.astimezone())


def local(stamp: str) -> str:
    return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def seed(
    app: FastAPI,
    object_type: str,
    external_id: str,
    *,
    key: str | None = "OIL-4",
    title: str | None = None,
    display_ref: str | None = None,
    locator: str | None = None,
    status: str = "ok",
    error: str | None = None,
    parent_id: int | None = None,
    versions: tuple[str, ...] = (),
    entries: tuple[EntryData, ...] = (),
) -> int:
    """Store an object of a connector that is never instantiated (the pages only read it) and link it."""
    with app.state.engine.begin() as conn:
        connector_id = conn.execute(
            text("SELECT id FROM connector WHERE name = 'Seeded'")
        ).scalar_one_or_none()
        if connector_id is None:
            connector_id = conn.execute(
                text("INSERT INTO connector (type, name) VALUES ('seeded', 'Seeded') RETURNING id")
            ).scalar_one()
        meta = ObjectMeta(object_type, external_id, title=title, display_ref=display_ref, locator=locator)
        object_id = store.upsert_object(conn, connector_id, meta, parent_id=parent_id)
        for number, body in enumerate(versions, start=1):
            version_id = store.write_version(
                conn, object_id, VersionData(body, source_version_ref=f"r{number}")
            )
            conn.execute(
                text("UPDATE source_version SET fetched_at = :at WHERE id = :id"),
                {"at": f"2026-11-0{number}T12:00:00.000Z", "id": version_id},
            )
        for entry in entries:
            store.write_entry(conn, object_id, entry)
        conn.execute(
            text(
                "UPDATE source_object SET fetch_status = :status, fetch_error = :error,"
                " last_checked_at = '2026-11-09T08:00:00.000Z',"
                " last_changed_at = '2026-11-08T08:00:00.000Z' WHERE id = :id"
            ),
            {"status": status, "error": error, "id": object_id},
        )
        if key is not None and parent_id is None:
            linking.link_source(conn, key, object_id, actor_id=people.get_me(conn).id)
    return object_id


def email(external_id: str, at: str, body: str, author: IdentityRef = BEA, **kwargs) -> EntryData:
    return EntryData("email_message", external_id, at, body, author=author, **kwargs)


def timeline_items(fragment: str) -> list[str]:
    return re.findall(r'<details class="timeline-item[^"]*">.*?</details>', fragment, re.DOTALL)


def summary_of(item: str) -> str:
    match = re.search(r"<summary>(.*?)</summary>", item, re.DOTALL)
    assert match is not None
    return plain(match.group(1))


@pytest.fixture
def get(detail_client: TestClient):
    def send(path: str, *, htmx: bool = True):
        return detail_client.get(path, headers=HTMX if htmx else {})

    return send


@pytest.fixture
def thread(detail_app: FastAPI) -> int:
    """An email thread with a reply, two attachments and a later new version of one of them."""
    thread_id = seed(
        detail_app,
        "email_thread",
        "t1",
        title="Lead time",
        display_ref="Inductor lead time",
        entries=(
            email(
                "m1",
                "2026-11-02T09:30:00.000Z",
                "Full first mail\n> quoted",
                new_text="First mail",
                recipients=(ALEX, CARL),
                metadata={
                    "subject": "Inductor lead time",
                    "to": ["alex@example.com"],
                    "cc": ["carl@acme.com"],
                },
            ),
            email(
                "m2",
                "2026-11-03T10:00:00.000Z",
                "Reply <b>body</b>",
                ALEX,
                metadata={"subject": "RE: Inductor lead time"},
            ),
        ),
    )
    for number, name in enumerate(("spec.pdf", "quote.xlsx"), start=1):
        seed(
            detail_app,
            "attachment",
            f"m1#{number}",
            title=name,
            display_ref=name,
            parent_id=thread_id,
            versions=("one", "two") if number == 1 else ("only",),
        )
    return thread_id


# --- the tabs ---------------------------------------------------------------------------------------


def test_the_page_renders_the_all_tab_by_default(
    detail_app: FastAPI, detail_client: TestClient, thread: int
) -> None:
    with detail_app.state.engine.begin() as conn:
        comments.add_comment(conn, "OIL-4", "A comment", author_id=people.get_me(conn).id)

    page = detail_client.get("/issues/OIL-4").text

    panel = re.search(r'<section class="panel" id="activity-panel">(.*?)</section>', page, re.DOTALL)
    assert panel is not None
    tabs = re.findall(r'<button [^>]*role="tab"[^>]*>(.*?)</button>', panel.group(1), re.DOTALL)
    assert tabs == ["All", "Comments", "History", "Source updates"]
    selected = re.findall(r'<button [^>]*aria-selected="true"[^>]*>(.*?)</button>', panel.group(1))
    assert selected == ["All"]
    assert 'class="timeline"' in panel.group(1)
    assert 'class="comments"' not in panel.group(1)
    summaries = [summary_of(item) for item in timeline_items(panel.group(1))]
    assert any("Comment by Alex" in line for line in summaries)
    assert any("Email from Alex: RE: Inductor lead time" in line for line in summaries)


def test_each_tab_endpoint_returns_a_fragment(detail_client: TestClient, get, thread: int) -> None:
    for tab, marker in (
        ("all", 'class="timeline"'),
        ("source-updates", 'class="timeline"'),
        ("comments", 'class="comments"'),
        ("history", 'class="history"'),
    ):
        response = get(f"/issues/OIL-4/activity/{tab}")

        assert response.status_code == 200
        assert marker in response.text
        assert "<html" not in response.text
        assert "<aside" not in response.text
        assert 'role="tablist"' not in response.text


def test_the_all_tab_merges_comments_history_entries_and_versions_newest_first(
    detail_app: FastAPI, get, thread: int
) -> None:
    with detail_app.state.engine.begin() as conn:
        comments.add_comment(conn, "OIL-4", "A comment", author_id=people.get_me(conn).id)

    items = timeline_items(get("/issues/OIL-4/activity/all").text)

    kinds = [re.search(r"kind-(\w+)", item).group(1) for item in items]  # type: ignore[union-attr]
    assert kinds[:3] == ["entry", "version", "entry"]  # November 2026 is after the frozen "now"
    assert sorted(kinds[3:]) == ["comment"] + ["history"] * (len(kinds) - 4)
    assert kinds.count("history") >= 2
    times = [re.search(r'timeline-time muted">(.*?)</span>', item).group(1) for item in items]  # type: ignore[union-attr]
    assert times == sorted(times, reverse=True)


def test_the_source_updates_tab_has_only_entries_and_versions(get, thread: int) -> None:
    fragment = get("/issues/OIL-4/activity/source-updates").text

    items = timeline_items(fragment)
    assert [re.search(r"kind-(\w+)", i).group(1) for i in items] == ["entry", "version", "entry"]  # type: ignore[union-attr]
    assert "Comment by" not in fragment
    assert "kind-history" not in fragment


def test_an_issue_without_activity_shows_the_empty_text(get) -> None:
    assert "No source updates yet." in get("/issues/OIL-5/activity/source-updates").text
    assert timeline_items(get("/issues/OIL-5/activity/source-updates").text) == []
    assert "No activity yet." not in get("/issues/OIL-5/activity/all").text  # its creation is history


def test_unknown_tabs_and_issues_are_404(get) -> None:
    assert get("/issues/OIL-4/activity/sources").status_code == 404
    assert get("/issues/OIL-99/activity/all").status_code == 404
    assert get("/issues/OIL-99/activity/source-updates").status_code == 404


# --- the items --------------------------------------------------------------------------------------


def test_an_item_is_one_line_with_time_kind_title_and_actor(get, thread: int) -> None:
    items = timeline_items(get("/issues/OIL-4/activity/source-updates").text)

    assert summary_of(items[0]) == (
        f"{local('2026-11-03T10:00:00.000Z')} ✉ Email Email from Alex: RE: Inductor lead time"
    )
    assert summary_of(items[2]) == (
        f"{local('2026-11-02T09:30:00.000Z')} ✉ Email Email from Bea Brandt: Inductor lead time"
    )
    assert "Bea Brandt</span>" not in items[2]  # the name is already in the title
    assert items[0].startswith('<details class="timeline-item kind-entry">')


def test_an_expanded_email_shows_recipients_body_and_attachment_names(get, thread: int) -> None:
    items = timeline_items(get("/issues/OIL-4/activity/source-updates").text)
    first = items[2]
    detail = re.search(r'<div class="timeline-detail">(.*)</div>\s*</details>', first, re.DOTALL)
    assert detail is not None

    text_of = plain(detail.group(1))
    assert "To: Alex" in text_of
    assert "Cc: Carl Conrad" in text_of
    assert "Attachments: spec.pdf, quote.xlsx" in text_of
    assert '<div class="timeline-body text">First mail</div>' in first
    assert f'<a class="timeline-source" href="/sources/{thread}">Open source page</a>' in first
    assert "timeline-compare" not in first


def test_a_version_links_to_the_diff_with_the_previous_one(get, detail_app: FastAPI, thread: int) -> None:
    items = timeline_items(get("/issues/OIL-4/activity/source-updates").text)
    version_item = items[1]
    with detail_app.state.engine.begin() as conn:
        attachment = conn.execute(
            text("SELECT id FROM source_object WHERE external_id = 'm1#1'")
        ).scalar_one()

    assert summary_of(version_item) == (
        f"{local('2026-11-02T12:00:00.000Z')} ⟳ New version New version v2 of spec.pdf"
    )
    assert (
        f'<a class="timeline-compare" href="/sources/{attachment}/diff?a=1&amp;b=2">Compare with previous</a>'
        in version_item
    )
    assert f'<a class="timeline-source" href="/sources/{attachment}">Open source page</a>' in version_item
    assert "timeline-body" not in version_item


def test_a_jira_comment_expands_to_its_full_text(detail_app: FastAPI, get) -> None:
    seed(
        detail_app,
        "jira_issue",
        "HWDEV-1187",
        display_ref="HWDEV-1187",
        entries=(
            EntryData("jira_comment", "c1", "2026-11-04T08:00:00.000Z", "Line one\nLine two", author=BEA),
        ),
    )

    [item] = timeline_items(get("/issues/OIL-4/activity/source-updates").text)

    assert summary_of(item).endswith("◆ Jira comment Jira comment by Bea Brandt on HWDEV-1187")
    assert '<div class="timeline-body text">Line one\nLine two</div>' in item


def test_comment_and_history_items_have_no_source_link(detail_app: FastAPI, get) -> None:
    with detail_app.state.engine.begin() as conn:
        comments.add_comment(conn, "OIL-4", "Note to self", author_id=people.get_me(conn).id)

    items = timeline_items(get("/issues/OIL-4/activity/all").text)

    assert items
    assert all("timeline-source" not in item and "timeline-compare" not in item for item in items)
    assert any('<div class="timeline-body text">Note to self</div>' in item for item in items)
    assert not any("comment add" in item for item in items)


def test_the_limit_note_appears_only_when_items_were_cut(
    monkeypatch: pytest.MonkeyPatch, detail_app: FastAPI, get
) -> None:
    seed(
        detail_app,
        "jira_issue",
        "HWDEV-1",
        display_ref="HWDEV-1",
        entries=tuple(
            EntryData("jira_comment", f"c{n}", f"2026-11-0{n}T08:00:00.000Z", f"c{n}", author=BEA)
            for n in range(1, 4)
        ),
    )
    monkeypatch.setattr(issue_detail, "ACTIVITY_LIMIT", 2)

    cut = get("/issues/OIL-4/activity/source-updates").text
    monkeypatch.setattr(issue_detail, "ACTIVITY_LIMIT", 3)
    exact = get("/issues/OIL-4/activity/source-updates").text

    assert len(timeline_items(cut)) == 2
    assert "Showing the latest 2 items." in cut
    assert [summary_of(i).split(" ")[-1] for i in timeline_items(cut)] == ["HWDEV-1"] * 2
    assert "c3" in cut and "c2" in cut and "c1</div>" not in cut
    assert len(timeline_items(exact)) == 3
    assert "Showing the latest" not in exact


def test_the_default_limit_is_200() -> None:
    assert issue_detail.ACTIVITY_LIMIT == 200


def test_entry_text_and_titles_are_escaped_in_the_timeline(detail_app: FastAPI, get) -> None:
    seed(
        detail_app,
        "email_thread",
        "t2",
        display_ref="Evil",
        entries=(
            email(
                "m1",
                "2026-11-02T09:30:00.000Z",
                "<script>alert(1)</script>",
                IdentityRef("email", "x@acme.com", "<i>Mallory</i>"),
                metadata={"subject": "<img src=x onerror=alert(2)>"},
            ),
        ),
    )

    fragment = get("/issues/OIL-4/activity/all").text

    assert "<script>" not in fragment
    assert "<img" not in fragment
    assert "<i>" not in fragment
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment
    assert "&lt;i&gt;Mallory&lt;/i&gt;" in fragment


# --- the source page --------------------------------------------------------------------------------


def make_note(app: FastAPI, key: str = "OIL-4") -> int:
    with app.state.engine.begin() as conn:
        note = manual.create_note(conn, "Meeting note", "Line one\nLine two\nLine three")
        linking.link_source(conn, key, note, actor_id=people.get_me(conn).id)
        manual.edit_note(conn, note, "Line one\nLine 2\nLine three\nLine four")
    return note


def test_the_source_page_shows_a_note_with_two_versions(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    note = make_note(detail_app)

    page = detail_client.get(f"/sources/{note}").text

    assert f"<h1>#{note} note Meeting note</h1>" in page
    body = plain(page)
    assert "Title Meeting note" in body
    assert "Connector Manual (manual)" in body
    assert "Status ok, checked" in body
    assert re.search(r'<dt>Linked to</dt>\s*<dd><a href="/issues/OIL-4">OIL-4</a></dd>', page)
    assert "Counts 2 versions · 0 entries · 0 children" in body
    assert "Latest version" in body and "v2, fetched" in body
    assert '<pre class="source-text text">Line one\nLine 2\nLine three\nLine four</pre>' in page
    assert "Locator" not in body
    assert "Open in source" not in page
    versions = re.search(r'<table class="versions">(.*?)</table>', page, re.DOTALL)
    assert versions is not None
    rows = [plain(r) for r in re.findall(r"<tr>(.*?)</tr>", versions.group(1), re.DOTALL)[1:]]
    assert len(rows) == 2
    assert rows[0].startswith("v1 ") and rows[1].startswith("v2 ")
    with detail_app.state.engine.begin() as conn:
        hashes = [v.content_hash[:12] for v in store.list_versions(conn, note)]
    assert rows[0].endswith(hashes[0]) and rows[1].endswith(hashes[1])
    assert "No entries stored." in page
    form = re.search(
        rf'<form class="version-compare" method="get" action="/sources/{note}/diff">(.*?)</form>',
        page,
        re.DOTALL,
    )
    assert form is not None
    selects = re.findall(r'<select name="(a|b)"[^>]*>(.*?)</select>', form.group(1), re.DOTALL)
    assert [name for name, _ in selects] == ["a", "b"]
    for name, options in selects:
        assert re.findall(r'<option value="(\d+)"', options) == ["1", "2"]
        assert re.findall(r'<option value="(\d+)" selected', options) == (["1"] if name == "a" else ["2"])
    assert ">Compare</button>" in form.group(1)


def test_a_single_version_has_no_compare_form(detail_app: FastAPI, detail_client: TestClient) -> None:
    with detail_app.state.engine.begin() as conn:
        note = manual.create_note(conn, "Once", "text")

    page = detail_client.get(f"/sources/{note}").text

    assert "version-compare" not in page
    assert "Linked to —" in plain(page)


def test_the_source_page_lists_entries_newest_first_and_the_children(
    detail_app: FastAPI, detail_client: TestClient, thread: int
) -> None:
    page = detail_client.get(f"/sources/{thread}").text

    assert f"<h1>#{thread} email_thread Inductor lead time</h1>" in page
    assert "Counts 0 versions · 2 entries · 2 children" in plain(page)
    entries = re.search(r'<ul class="entry-list">(.*?)</ul>', page, re.DOTALL)
    assert entries is not None
    items = timeline_items(entries.group(1))
    assert [summary_of(i) for i in items] == [
        f"{local('2026-11-03T10:00:00.000Z')} email_message out Alex",
        f"{local('2026-11-02T09:30:00.000Z')} email_message in Bea Brandt",
    ]
    assert '<div class="entry-text text">Reply &lt;b&gt;body&lt;/b&gt;</div>' in items[0]
    assert '<div class="entry-text text">Full first mail\n&gt; quoted</div>' in items[1]
    children = re.search(r'<ul class="child-list">(.*?)</ul>', page, re.DOTALL)
    assert children is not None
    with detail_app.state.engine.begin() as conn:
        ids = [c.id for c in store.list_children(conn, thread)]
    links = re.findall(r'<a href="/sources/(\d+)">(.*?)</a>', children.group(1))
    assert links == [(str(ids[0]), f"#{ids[0]} spec.pdf"), (str(ids[1]), f"#{ids[1]} quote.xlsx")]
    assert "attachment · ok" in plain(children.group(1))


def test_a_child_page_links_back_to_its_parent_and_the_parents_issues(
    detail_app: FastAPI, detail_client: TestClient, thread: int
) -> None:
    with detail_app.state.engine.begin() as conn:
        child = store.list_children(conn, thread)[0].id

    page = detail_client.get(f"/sources/{child}").text

    assert f'Part of</dt><dd><a href="/sources/{thread}">#{thread} Inductor lead time</a>' in page
    assert '<a href="/issues/OIL-4">OIL-4</a>' in page
    assert "Counts 2 versions · 0 entries · 0 children" in plain(page)


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        (
            "https://jira.example.com/browse/HW-1",
            '<a class="source-open" href="https://jira.example.com/browse/HW-1"'
            ' target="_blank" rel="noopener">Open in source</a>',
        ),
        (
            "http://wiki.example.com/p",
            '<a class="source-open" href="http://wiki.example.com/p"'
            ' target="_blank" rel="noopener">Open in source</a>',
        ),
        ("outlook:store:entry", "<code>outlook:store:entry</code>"),
        ("C:\\Share\\spec.xlsx", "<code>C:\\Share\\spec.xlsx</code>"),
        ("javascript:alert(1)", "<code>javascript:alert(1)</code>"),
    ],
)
def test_open_in_source_only_for_http_locators(
    detail_app: FastAPI, detail_client: TestClient, locator: str, expected: str
) -> None:
    source = seed(detail_app, "file", "f1", display_ref="spec", locator=locator)

    page = detail_client.get(f"/sources/{source}").text

    assert expected in page
    assert ("Open in source" in page) == locator.startswith(("http://", "https://"))


def test_the_source_page_shows_status_and_error(detail_app: FastAPI, detail_client: TestClient) -> None:
    source = seed(
        detail_app, "jira_issue", "HW-1", display_ref="HW-1", status="error", error="401 <unauthorised>"
    )

    page = detail_client.get(f"/sources/{source}").text

    assert (
        f"Status error, checked {local('2026-11-09T08:00:00.000Z')},"
        f" changed {local('2026-11-08T08:00:00.000Z')}" in plain(page)
    )
    assert '<dd class="fetch-error">401 &lt;unauthorised&gt;</dd>' in page


def test_the_latest_version_is_cut_after_400_lines(detail_app: FastAPI, detail_client: TestClient) -> None:
    long_text = "\n".join(f"row {n}" for n in range(1, 451))
    source = seed(detail_app, "note", "n1", title="Long", display_ref="Long", versions=(long_text,))

    page = detail_client.get(f"/sources/{source}").text

    shown = re.search(r'<pre class="source-text text">(.*?)</pre>', page, re.DOTALL)
    assert shown is not None
    lines = shown.group(1).splitlines()
    assert len(lines) == 401
    assert lines[399] == "row 400"
    assert lines[400] == "… (50 more lines)"
    assert "row 401" not in shown.group(1)


def test_an_unknown_source_is_a_404_page(detail_client: TestClient) -> None:
    for path in ("/sources/9999", "/sources/9999/diff?a=1&b=2"):
        response = detail_client.get(path)

        assert response.status_code == 404
        assert "<html" in response.text
        assert "source object 9999 not found" in response.text


def test_the_panel_links_resolve_to_source_pages(
    detail_app: FastAPI, detail_client: TestClient, thread: int
) -> None:
    make_note(detail_app)
    page = detail_client.get("/issues/OIL-4").text

    hrefs = re.findall(r'class="source-label" href="(/sources/\d+)"', page)

    assert len(hrefs) == 2
    for href in hrefs:
        assert detail_client.get(href).status_code == 200


def test_entry_text_is_escaped_on_the_source_page(detail_app: FastAPI, detail_client: TestClient) -> None:
    source = seed(
        detail_app,
        "jira_issue",
        "HW-9",
        display_ref="HW-9",
        title="<script>title()</script>",
        versions=("<script>version()</script>",),
        entries=(
            EntryData(
                "jira_comment", "c1", "2026-11-04T08:00:00.000Z", "<script>alert(1)</script>", author=BEA
            ),
        ),
    )

    page = detail_client.get(f"/sources/{source}").text

    assert "<script>alert" not in page
    assert "<script>version" not in page
    assert "<script>title" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;script&gt;version()&lt;/script&gt;" in page


# --- the diff page ----------------------------------------------------------------------------------


def test_the_diff_page_shows_both_versions_side_by_side(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    note = make_note(detail_app)

    response = detail_client.get(f"/sources/{note}/diff?a=1&b=2")

    assert response.status_code == 200
    page = response.text
    assert "<h1>Compare v1 and v2</h1>" in page
    assert f'<a href="/sources/{note}">#{note} note Meeting note</a>' in page
    table = re.search(r'<table class="diff".*?</table>', page, re.DOTALL)
    assert table is not None
    assert ">v1</th>" in table.group(0) and ">v2</th>" in table.group(0)
    assert '<span class="diff_sub">Line&nbsp;two</span>' in table.group(0)
    assert '<span class="diff_add">Line&nbsp;2</span>' in table.group(0)
    assert '<span class="diff_add">Line&nbsp;four</span>' in table.group(0)  # only in v2
    assert "No differences." not in page


def test_removed_lines_are_marked_when_the_versions_are_swapped(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    note = make_note(detail_app)

    page = detail_client.get(f"/sources/{note}/diff?a=2&b=1").text

    assert '<span class="diff_sub">Line&nbsp;four</span>' in page
    assert '<span class="diff_add">Line&nbsp;four</span>' not in page


def test_identical_versions_say_there_are_no_differences(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    note = make_note(detail_app)

    page = detail_client.get(f"/sources/{note}/diff?a=2&b=2").text

    assert "No differences." in page
    assert "<table" not in page
    assert "Compare v2 and v2" in page


def test_versions_differing_only_in_trailing_blanks_still_show_a_table(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    source = seed(detail_app, "note", "n1", title="Same", versions=("a\nb", "a\nb "), key=None)

    page = detail_client.get(f"/sources/{source}/diff?a=1&b=2").text

    assert "<table" in page
    assert "No differences." not in page


@pytest.mark.parametrize("query", ["a=1&b=9", "a=0&b=1", "a=x&b=1", "a=1", "b=2", "", "a=1&b="])
def test_unknown_or_missing_versions_are_a_422_error_page(
    detail_app: FastAPI, detail_client: TestClient, query: str
) -> None:
    note = make_note(detail_app)

    response = detail_client.get(f"/sources/{note}/diff?{query}")

    assert response.status_code == 422
    assert '<div class="error" role="alert">' in response.text
    assert "<html" in response.text
    assert "<table" not in response.text


def test_the_error_names_the_missing_version(detail_app: FastAPI, detail_client: TestClient) -> None:
    note = make_note(detail_app)

    response = detail_client.get(f"/sources/{note}/diff?a=1&b=9")

    assert f"source object {note} has no version 9" in response.text


def test_the_error_is_a_fragment_for_htmx_requests(detail_app: FastAPI, detail_client: TestClient) -> None:
    note = make_note(detail_app)

    response = detail_client.get(f"/sources/{note}/diff?a=1&b=9", headers=HTMX)

    assert response.status_code == 422
    assert response.text.startswith('<div class="error" role="alert">')


def test_version_text_is_escaped_on_the_diff_page(detail_app: FastAPI, detail_client: TestClient) -> None:
    source = seed(
        detail_app,
        "note",
        "n1",
        title="Evil",
        versions=("<script>one()</script>", "<script>two()</script>"),
    )

    page = detail_client.get(f"/sources/{source}/diff?a=1&b=2").text

    assert "<script>one" not in page
    assert "<script>two" not in page
    assert "&lt;script&gt;" in page


def test_the_compare_form_of_the_source_page_leads_to_the_diff(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    note = make_note(detail_app)

    response = detail_client.get(f"/sources/{note}/diff", params={"a": "1", "b": "2"})

    assert response.status_code == 200
    assert 'class="diff"' in response.text


def test_a_changed_word_inside_a_line_is_marked_as_changed(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    source = seed(
        detail_app,
        "note",
        "n1",
        title="Words",
        versions=("The quick brown fox jumps over\nsame", "The quick brown cat jumps over\nsame"),
    )

    page = detail_client.get(f"/sources/{source}/diff?a=1&b=2").text

    assert '<span class="diff_chg">fox</span>' in page
    assert '<span class="diff_chg">cat</span>' in page


def test_a_new_comment_is_exactly_one_item_on_the_all_tab(detail_app: FastAPI, get) -> None:
    before = len(timeline_items(get("/issues/OIL-4/activity/all").text))
    with detail_app.state.engine.begin() as conn:
        comments.add_comment(conn, "OIL-4", "Only once", author_id=people.get_me(conn).id)

    items = timeline_items(get("/issues/OIL-4/activity/all").text)

    assert len(items) == before + 1
    assert len([i for i in items if "Only once" in i]) == 1
    assert "kind-comment" in [i for i in items if "Only once" in i][0]


def test_the_history_tab_still_lists_the_comment_event(detail_app: FastAPI, get) -> None:
    with detail_app.state.engine.begin() as conn:
        comments.add_comment(conn, "OIL-4", "Only once", author_id=people.get_me(conn).id)

    fragment = get("/issues/OIL-4/activity/history").text

    assert '<span class="event-field">comment</span> add' in fragment
    assert "Only once" in fragment
