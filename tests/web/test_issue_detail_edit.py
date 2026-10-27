import re
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.domain import comments, history, issue_links, issues, people, projects
from ai_oil.governance import audit

NOW = datetime(2026, 10, 28, 13, 0)
HTMX = {"HX-Request": "true"}
ERROR_PREFIX = '<div class="error" role="alert">'


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


def entries(app: FastAPI, prefix: str | None = None) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def changes(app: FastAPI, key: str) -> list[tuple[str, str, str | None, str | None]]:
    with app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, key)
        return [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


def local(stamp: str) -> str:
    return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def side(page: str) -> str:
    match = re.search(r'<aside id="side-panel".*?</aside>', page, re.DOTALL)
    assert match is not None
    return match.group(0)


def chips(fragment: str, row: str) -> list[str]:
    """The names of the chips in the labels or components row of a side panel."""
    match = re.search(
        rf'<div class="field-side field-chips" id="field-{row}">(.*?)\n  </div>\n', fragment, re.DOTALL
    )
    assert match is not None
    return re.findall(r'<span class="chip-name">(.*?)</span>', match.group(1))


def options(fragment: str) -> list[str]:
    return re.findall(r"<option[^>]*>(.*?)</option>", fragment, re.DOTALL)


def with_conn(app: FastAPI, work) -> None:
    with app.state.engine.begin() as conn:
        work(conn)


# --- labels (UI-20, DM-4) ---------------------------------------------------------------------------


def test_the_page_shows_labels_and_components_rows_with_forms(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    with_conn(
        detail_app,
        lambda conn: (
            issues.add_label(conn, "OIL-4", "supplier", actor_id=people.get_me(conn).id),
            projects.create_component(conn, issues.get_issue(conn, "OIL-4").project_id, "Power"),
        ),
    )

    panel = side(detail_client.get("/issues/OIL-4").text)

    assert chips(panel, "labels") == ["supplier"]
    assert chips(panel, "components") == []
    assert 'hx-post="/issues/OIL-4/labels/remove"' in panel
    assert 'hx-post="/issues/OIL-4/labels"' in panel
    assert 'hx-post="/issues/OIL-4/components"' in panel
    assert re.search(r'<input type="text" name="name" list="label-options"', panel)
    assert component_select(panel) == ["Power"]


def test_adding_a_label_returns_the_side_panel_and_records_history_and_audit(
    detail_app: FastAPI, post
) -> None:
    response = post("/issues/OIL-4/labels", {"name": " supplier "})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<aside id="side-panel"')
    assert chips(response.text, "labels") == ["supplier"]
    assert load(detail_app, "OIL-4").labels == ("supplier",)
    assert ("label", "add", None, "supplier") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app, "issue.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue.edit",
        "B",
        "user",
        "OIL-4",
    )
    assert entry.detail == {"add_label": ["supplier"], "via": "web"}


def test_the_label_history_has_the_signed_in_person_as_actor(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/labels", {"name": "supplier"})

    with detail_app.state.engine.begin() as conn:
        issue = issues.get_issue(conn, "OIL-4")
        latest = history.issue_history(conn, issue.id)[-1]
        assert (latest.actor_type, latest.actor_id) == ("user", people.get_me(conn).id)


def test_the_label_input_offers_the_existing_labels(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-5/labels", {"name": "b-sample"})
    post("/issues/OIL-5/labels", {"name": "supplier"})

    response = post("/issues/OIL-4/labels", {"name": "emc"})

    datalist = re.search(r'<datalist id="label-options">(.*?)</datalist>', response.text, re.DOTALL)
    assert datalist is not None
    assert re.findall(r'<option value="([^"]*)">', datalist.group(1)) == ["b-sample", "emc", "supplier"]


def test_removing_a_label_returns_the_side_panel_and_records_history_and_audit(
    detail_app: FastAPI, post
) -> None:
    post("/issues/OIL-4/labels", {"name": "supplier"})
    post("/issues/OIL-4/labels", {"name": "emc"})

    response = post("/issues/OIL-4/labels/remove", {"name": "supplier"})

    assert response.status_code == 200
    assert chips(response.text, "labels") == ["emc"]
    assert load(detail_app, "OIL-4").labels == ("emc",)
    assert ("label", "remove", "supplier", None) in changes(detail_app, "OIL-4")
    assert entries(detail_app, "issue.")[0].detail == {"remove_label": ["supplier"], "via": "web"}


def test_a_label_that_is_already_there_or_not_there_writes_nothing(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/labels", {"name": "supplier"})
    history_before = changes(detail_app, "OIL-4")
    audit_before = entries(detail_app)

    assert post("/issues/OIL-4/labels", {"name": "supplier"}).status_code == 200
    assert post("/issues/OIL-4/labels/remove", {"name": "emc"}).status_code == 200
    assert post("/issues/OIL-4/labels/remove", {"name": "unknown"}).status_code == 200

    assert changes(detail_app, "OIL-4") == history_before
    assert entries(detail_app) == audit_before


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("two words", "must not contain spaces or commas"),
        ("a,b", "must not contain spaces or commas"),
        (" ", "must not be blank"),
    ],
)
def test_an_invalid_label_is_a_422_fragment_and_changes_nothing(
    detail_app: FastAPI, post, name: str, message: str
) -> None:
    before = load(detail_app, "OIL-4")

    response = post("/issues/OIL-4/labels", {"name": name})

    assert response.status_code == 422
    assert response.text.startswith(ERROR_PREFIX)
    assert message in response.text
    assert load(detail_app, "OIL-4") == before
    assert entries(detail_app) == []


def test_labels_of_an_unknown_issue_are_a_404(post) -> None:
    assert post("/issues/OIL-99/labels", {"name": "x"}).status_code == 404
    assert post("/issues/OIL-99/labels/remove", {"name": "x"}).status_code == 404


# --- components (UI-20, DM-3) -----------------------------------------------------------------------


@pytest.fixture
def components(detail_app: FastAPI) -> None:
    """OIL has Control, Power and an archived Old; ABC has Other."""

    def create(conn) -> None:
        oil = projects.get_project(conn, "OIL")
        abc = projects.get_project(conn, "ABC")
        for name in ("Power", "Control", "Old"):
            projects.create_component(conn, oil.id, name)
        projects.create_component(conn, abc.id, "Other")
        conn.execute(text("UPDATE component SET archived = 1 WHERE name = 'Old'"))

    with_conn(detail_app, create)


def component_select(fragment: str) -> list[str]:
    match = re.search(r'<select name="name" aria-label="Add component">(.*?)</select>', fragment, re.DOTALL)
    return options(match.group(1)) if match else []


@pytest.mark.usefixtures("components")
def test_the_component_select_offers_the_active_components_of_the_project(detail_client: TestClient) -> None:
    panel = side(detail_client.get("/issues/OIL-4").text)

    assert component_select(panel) == ["Control", "Power"]
    assert component_select(side(detail_client.get("/issues/ABC-1").text)) == ["Other"]


def test_there_is_no_component_form_when_the_project_has_none(detail_client: TestClient) -> None:
    panel = side(detail_client.get("/issues/OIL-4").text)

    assert component_select(panel) == []
    assert 'hx-post="/issues/OIL-4/components"' not in panel


@pytest.mark.usefixtures("components")
def test_adding_and_removing_a_component(detail_app: FastAPI, post) -> None:
    added = post("/issues/OIL-4/components", {"name": "Power"})

    assert added.status_code == 200
    assert added.text.lstrip().startswith('<aside id="side-panel"')
    assert chips(added.text, "components") == ["Power"]
    assert component_select(added.text) == ["Control"]
    assert load(detail_app, "OIL-4").components == ("Power",)
    assert ("component", "add", None, "Power") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app, "issue.")
    assert (entry.action, entry.action_class, entry.target) == ("issue.edit", "B", "OIL-4")
    assert entry.detail == {"add_component": ["Power"], "via": "web"}

    removed = post("/issues/OIL-4/components/remove", {"name": "Power"})

    assert removed.status_code == 200
    assert chips(removed.text, "components") == []
    assert component_select(removed.text) == ["Control", "Power"]
    assert load(detail_app, "OIL-4").components == ()
    assert ("component", "remove", "Power", None) in changes(detail_app, "OIL-4")
    assert entries(detail_app, "issue.")[0].detail == {"remove_component": ["Power"], "via": "web"}


@pytest.mark.usefixtures("components")
def test_a_component_of_another_project_is_a_404_and_changes_nothing(detail_app: FastAPI, post) -> None:
    before = load(detail_app, "OIL-4")

    assert post("/issues/OIL-4/components", {"name": "Other"}).status_code == 404
    assert post("/issues/OIL-4/components/remove", {"name": "Other"}).status_code == 404
    assert load(detail_app, "OIL-4") == before
    assert entries(detail_app) == []


@pytest.mark.usefixtures("components")
def test_adding_a_component_twice_writes_nothing_the_second_time(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/components", {"name": "Power"})
    audit_before = entries(detail_app)

    assert post("/issues/OIL-4/components", {"name": "Power"}).status_code == 200
    assert post("/issues/OIL-4/components/remove", {"name": "Control"}).status_code == 200

    assert entries(detail_app) == audit_before


# --- issue links (DM-14) ----------------------------------------------------------------------------


def links_section(page: str) -> str:
    match = re.search(r'<section class="panel" id="links-panel">.*?</section>', page, re.DOTALL)
    assert match is not None
    return match.group(0)


def link_lines(page: str) -> list[str]:
    """One plain-text line per link: label, key, summary, status and the remove button."""
    return [plain(item) for item in re.findall(r"<li>(.*?)</li>", links_section(page), re.DOTALL)]


def stored_links(app: FastAPI) -> list[tuple[str, str, str]]:
    with app.state.engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT a.key, l.link_type, b.key FROM issue_link l"
                " JOIN issue a ON a.id = l.from_issue_id JOIN issue b ON b.id = l.to_issue_id ORDER BY l.id"
            )
        ).all()
    return [tuple(row) for row in rows]


RELATION_CASES = [
    # relation, stored (from, type, to), label on OIL-4, label on OIL-5
    ("blocks", ("OIL-4", "blocks", "OIL-5"), "blocks", "is blocked by"),
    ("is_blocked_by", ("OIL-5", "blocks", "OIL-4"), "is blocked by", "blocks"),
    ("relates_to", ("OIL-4", "relates_to", "OIL-5"), "relates to", "relates to"),
    ("duplicates", ("OIL-4", "duplicates", "OIL-5"), "duplicates", "is duplicated by"),
    ("is_duplicated_by", ("OIL-5", "duplicates", "OIL-4"), "is duplicated by", "duplicates"),
]


def test_the_page_lists_no_links_and_a_form_with_the_five_relations(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-4").text
    section = links_section(page)

    assert "No links" in section
    assert options(section) == ["blocks", "is blocked by", "relates to", "duplicates", "is duplicated by"]
    assert re.findall(r'<option value="([^"]*)"', section) == [
        "blocks",
        "is_blocked_by",
        "relates_to",
        "duplicates",
        "is_duplicated_by",
    ]
    assert 'hx-post="/issues/OIL-4/links"' in section
    assert 'hx-target="#links-panel"' in section
    assert page.index('id="field-acceptance_criteria"') < page.index('id="links-panel"')
    assert page.index('id="links-panel"') < page.index('id="sources-panel"')


@pytest.mark.parametrize(("relation", "stored", "label_here", "label_there"), RELATION_CASES)
def test_each_relation_stores_the_right_link_and_shows_the_right_label_on_both_issues(
    detail_app: FastAPI,
    detail_client: TestClient,
    post,
    relation: str,
    stored: tuple[str, str, str],
    label_here: str,
    label_there: str,
) -> None:
    response = post("/issues/OIL-4/links", {"relation": relation, "other_key": "OIL-5"})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<section class="panel" id="links-panel">')
    assert stored_links(detail_app) == [stored]
    assert link_lines(response.text) == [f"{label_here} OIL-5 Dropped item Cancelled ×"]
    assert link_lines(detail_client.get("/issues/OIL-4").text) == [
        f"{label_here} OIL-5 Dropped item Cancelled ×"
    ]
    assert link_lines(detail_client.get("/issues/OIL-5").text) == [f"{label_there} OIL-4 Plain item New ×"]


@pytest.mark.parametrize(("relation", "stored", "label_here", "label_there"), RELATION_CASES)
def test_adding_a_link_writes_history_on_both_issues_and_the_audit_entry(
    detail_app: FastAPI, post, relation: str, stored: tuple[str, str, str], label_here: str, label_there: str
) -> None:
    post("/issues/OIL-4/links", {"relation": relation, "other_key": "OIL-5"})

    assert [c for c in changes(detail_app, "OIL-4") if c[0] == "issue_link"] == [
        ("issue_link", "add", None, f"{label_here} OIL-5")
    ]
    assert [c for c in changes(detail_app, "OIL-5") if c[0] == "issue_link"] == [
        ("issue_link", "add", None, f"{label_there} OIL-4")
    ]
    [entry] = entries(detail_app, "issue_link.")
    from_key, link_type, to_key = stored
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue_link.add",
        "B",
        "user",
        from_key,
    )
    assert entry.detail == {"from": from_key, "type": link_type, "to": to_key, "link_id": 1, "via": "web"}


def test_a_link_line_has_a_link_to_the_other_issue(detail_client: TestClient, post) -> None:
    response = post("/issues/OIL-4/links", {"relation": "blocks", "other_key": " oil-5 "})

    assert '<a class="link-key" href="/issues/OIL-5">OIL-5</a>' in response.text


@pytest.mark.parametrize(
    ("direction", "viewed", "other"), [("outward", "OIL-4", "OIL-5"), ("inward", "OIL-5", "OIL-4")]
)
@pytest.mark.parametrize("link_type", ["blocks", "relates_to", "duplicates"])
def test_removing_a_link_from_either_side(
    detail_app: FastAPI,
    detail_client: TestClient,
    post,
    link_type: str,
    direction: str,
    viewed: str,
    other: str,
) -> None:
    def link(conn) -> None:
        issue_links.link_issues(conn, "OIL-4", link_type, "OIL-5", actor_id=people.get_me(conn).id)

    with_conn(detail_app, link)
    page = detail_client.get(f"/issues/{viewed}").text
    form = re.search(r'<form class="link-remove".*?</form>', page, re.DOTALL)
    assert form is not None
    values = dict(re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', form.group(0)))
    assert values == {"link_type": link_type, "other_key": other, "direction": direction}
    assert f'hx-post="/issues/{viewed}/links/remove"' in form.group(0)

    response = post(f"/issues/{viewed}/links/remove", values)

    assert response.status_code == 200
    assert "No links" in response.text
    assert stored_links(detail_app) == []
    assert [c for c in changes(detail_app, "OIL-4") if c[:2] == ("issue_link", "remove")] != []
    assert [c for c in changes(detail_app, "OIL-5") if c[:2] == ("issue_link", "remove")] != []
    entry = entries(detail_app, "issue_link.remove")[0]
    assert (entry.action, entry.action_class, entry.actor_type) == ("issue_link.remove", "B", "user")
    assert entry.detail["via"] == "web"
    assert entry.detail["type"] == link_type
    assert {entry.detail["from"], entry.detail["to"]} == {"OIL-4", "OIL-5"}
    assert entry.target == entry.detail["from"]


def test_removing_an_outward_link_names_the_viewed_issue_as_the_start_in_the_audit(
    detail_app: FastAPI, post
) -> None:
    post("/issues/OIL-4/links", {"relation": "blocks", "other_key": "OIL-5"})

    post("/issues/OIL-4/links/remove", {"link_type": "blocks", "other_key": "OIL-5", "direction": "outward"})
    assert entries(detail_app, "issue_link.remove")[0].detail == {
        "from": "OIL-4",
        "type": "blocks",
        "to": "OIL-5",
        "via": "web",
    }

    post("/issues/OIL-4/links", {"relation": "blocks", "other_key": "OIL-5"})
    post("/issues/OIL-5/links/remove", {"link_type": "blocks", "other_key": "OIL-4", "direction": "inward"})
    assert entries(detail_app, "issue_link.remove")[0].detail == {
        "from": "OIL-4",
        "type": "blocks",
        "to": "OIL-5",
        "via": "web",
    }


@pytest.mark.parametrize(
    ("data", "status", "message"),
    [
        ({"relation": "blocks", "other_key": "OIL-4"}, 422, "cannot be linked to itself"),
        ({"relation": "blocks", "other_key": "oil-4"}, 422, "cannot be linked to itself"),
        ({"relation": "blocks", "other_key": "OIL-99"}, 404, "OIL-99"),
        ({"relation": "blocks", "other_key": " "}, 422, "must not be blank"),
        ({"relation": "causes", "other_key": "OIL-5"}, 422, "relation must be one of"),
        ({"other_key": "OIL-5"}, 422, "relation must be one of"),
    ],
)
def test_link_errors_are_error_fragments_and_change_nothing(
    detail_app: FastAPI, post, data: dict, status: int, message: str
) -> None:
    response = post("/issues/OIL-4/links", data)

    assert response.status_code == status
    assert response.text.startswith(ERROR_PREFIX)
    assert message in response.text
    assert stored_links(detail_app) == []
    assert entries(detail_app) == []


def test_a_link_that_exists_or_contradicts_an_existing_one_is_a_422(detail_app: FastAPI, post) -> None:
    post("/issues/OIL-4/links", {"relation": "blocks", "other_key": "OIL-5"})

    again = post("/issues/OIL-4/links", {"relation": "blocks", "other_key": "OIL-5"})
    reverse = post("/issues/OIL-4/links", {"relation": "is_blocked_by", "other_key": "OIL-5"})

    assert again.status_code == 422
    assert "already exists" in again.text
    assert reverse.status_code == 422
    assert stored_links(detail_app) == [("OIL-4", "blocks", "OIL-5")]
    assert len(entries(detail_app, "issue_link.")) == 1


@pytest.mark.parametrize(
    ("data", "status"),
    [
        ({"link_type": "blocks", "other_key": "OIL-5", "direction": "outward"}, 404),
        ({"link_type": "blocks", "other_key": "OIL-5", "direction": "sideways"}, 422),
        ({"link_type": "causes", "other_key": "OIL-5", "direction": "outward"}, 422),
        ({"link_type": "blocks", "other_key": "OIL-99", "direction": "outward"}, 404),
    ],
)
def test_removing_a_link_that_does_not_exist_or_with_bad_input_is_an_error(
    detail_app: FastAPI, post, data: dict, status: int
) -> None:
    response = post("/issues/OIL-4/links/remove", data)

    assert response.status_code == status
    assert response.text.startswith(ERROR_PREFIX)
    assert entries(detail_app) == []


def test_a_link_to_an_unknown_issue_page_is_a_404(post) -> None:
    assert post("/issues/OIL-99/links", {"relation": "blocks", "other_key": "OIL-4"}).status_code == 404


# --- comments (DM-19) -------------------------------------------------------------------------------


def comment_articles(fragment: str) -> list[dict[str, str]]:
    found = []
    for match in re.finditer(
        r'<article class="comment" id="comment-(\d+)">(.*?)</article>', fragment, re.DOTALL
    ):
        body = re.search(r'<div class="comment-body text">(.*?)</div>', match.group(2), re.DOTALL)
        author = re.search(r'<span class="comment-author">(.*?)</span>', match.group(2))
        meta = re.search(r'<span class="muted">(.*?)</span>', match.group(2))
        assert body is not None and author is not None and meta is not None
        found.append(
            {"id": match.group(1), "author": author.group(1), "meta": meta.group(1), "body": body.group(1)}
        )
    return found


def add_comment(app: FastAPI, key: str, body: str, *, author: str = "Alex") -> comments.Comment:
    author_id = person_id(app, author)
    with app.state.engine.begin() as conn:
        return comments.add_comment(conn, key, body, author_id=author_id)


def test_the_page_has_the_activity_tabs_with_comments_selected(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-4").text
    panel = re.search(r'<section class="panel" id="activity-panel">(.*?)</section>', page, re.DOTALL)
    assert panel is not None

    tabs = re.findall(r"<button [^>]*role=\"tab\"[^>]*>(.*?)</button>", panel.group(1), re.DOTALL)
    assert tabs == ["Comments", "History"]
    comments_tab = re.search(r"<button [^>]*>Comments</button>", panel.group(1))
    history_tab = re.search(r"<button [^>]*>History</button>", panel.group(1))
    assert comments_tab is not None and history_tab is not None
    assert 'aria-selected="true"' in comments_tab.group(0)
    assert 'aria-selected="false"' in history_tab.group(0)
    for tab, button in (("comments", comments_tab), ("history", history_tab)):
        assert f'hx-get="/issues/OIL-4/activity/{tab}"' in button.group(0)
        assert 'hx-target="#activity-content"' in button.group(0)
    assert re.search(r'<div id="activity-content"[^>]*>.*?class="comments"', panel.group(1), re.DOTALL)
    assert "No comments" in panel.group(1)
    assert '<section class="panel" id="sources-panel">' in page


def test_adding_a_comment_returns_the_comments_tab_and_records_history_and_audit(
    detail_app: FastAPI, post
) -> None:
    response = post("/issues/OIL-4/comments", {"body": "  First note\nsecond line  "})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="comments">')
    assert "<html" not in response.text
    [comment] = comment_articles(response.text)
    with detail_app.state.engine.begin() as conn:
        [stored] = comments.list_comments(conn, "OIL-4")
    assert comment == {
        "id": str(stored.id),
        "author": "Alex",
        "meta": local(stored.created_at),
        "body": "First note\nsecond line",
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", comment["meta"])
    assert 'class="comment-body text"' in response.text
    assert ("comment", "add", None, "First note\nsecond line") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app, "comment.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "comment.add",
        "B",
        "user",
        "OIL-4",
    )
    assert entry.detail == {"comment_id": stored.id, "text": "First note\nsecond line", "via": "web"}


def test_comments_are_listed_oldest_first_with_the_author_name(detail_app: FastAPI, post) -> None:
    add_comment(detail_app, "OIL-4", "by Bea", author="Bea Brandt")
    post("/issues/OIL-4/comments", {"body": "by me"})

    response = post("/issues/OIL-4/comments", {"body": "also by me"})

    articles = comment_articles(response.text)
    assert [(a["author"], a["body"]) for a in articles] == [
        ("Bea Brandt", "by Bea"),
        ("Alex", "by me"),
        ("Alex", "also by me"),
    ]


def test_the_comment_body_is_escaped(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/comments", {"body": "<script>alert(1)</script> & <b>bold</b>"})

    assert "<script>" not in response.text
    assert "<b>" not in response.text
    [comment] = comment_articles(response.text)
    assert comment["body"] == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;"


def test_the_page_shows_the_comments_of_this_issue_only(
    detail_app: FastAPI, detail_client: TestClient
) -> None:
    add_comment(detail_app, "OIL-4", "mine")
    add_comment(detail_app, "OIL-5", "somebody else's")

    page = detail_client.get("/issues/OIL-4").text

    assert [a["body"] for a in comment_articles(page)] == ["mine"]


def test_a_blank_comment_is_a_422_and_changes_nothing(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/comments", {"body": "  \n "})

    assert response.status_code == 422
    assert response.text.startswith(ERROR_PREFIX)
    assert "comment body must not be blank" in response.text
    assert entries(detail_app) == []
    with detail_app.state.engine.begin() as conn:
        assert comments.list_comments(conn, "OIL-4") == []


def test_every_comment_has_edit_and_delete_controls(detail_app: FastAPI, get) -> None:
    comment = add_comment(detail_app, "OIL-4", "note")

    tab = get("/issues/OIL-4/activity/comments").text

    assert f'hx-get="/issues/OIL-4/comments/{comment.id}/edit"' in tab
    assert f'hx-target="#comment-{comment.id}"' in tab
    delete = re.search(rf'<button [^>]*hx-post="/issues/OIL-4/comments/{comment.id}/delete"[^>]*>', tab)
    assert delete is not None
    assert 'hx-confirm="Delete this comment?"' in delete.group(0)
    assert 'hx-target="#activity-content"' in delete.group(0)
    form = re.search(r'<form class="comment-add"[^>]*>(.*?)</form>', tab, re.DOTALL)
    assert form is not None
    assert 'hx-post="/issues/OIL-4/comments"' in tab
    assert '<textarea name="body"' in form.group(1)
    assert ">Comment</button>" in form.group(1)


def test_the_comment_editor_is_a_textarea_with_save_and_cancel(detail_app: FastAPI, get) -> None:
    comment = add_comment(detail_app, "OIL-4", "a <b> note")

    response = get(f"/issues/OIL-4/comments/{comment.id}/edit")

    assert response.status_code == 200
    assert response.text.lstrip().startswith(f'<form class="comment editor" id="comment-{comment.id}">')
    assert re.search(r'<textarea name="body"[^>]*>a &lt;b&gt; note</textarea>', response.text)
    save = re.search(r"<button [^>]*>Save</button>", response.text)
    cancel = re.search(r"<button [^>]*>Cancel</button>", response.text)
    assert save is not None and cancel is not None
    assert f'hx-post="/issues/OIL-4/comments/{comment.id}/edit"' in save.group(0)
    assert 'hx-target="#activity-content"' in save.group(0)
    assert 'hx-get="/issues/OIL-4/activity/comments"' in cancel.group(0)


def test_editing_a_comment_returns_the_tab_and_records_history_and_audit(detail_app: FastAPI, post) -> None:
    comment = add_comment(detail_app, "OIL-4", "old text")

    response = post(f"/issues/OIL-4/comments/{comment.id}/edit", {"body": " new text "})

    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="comments">')
    [shown] = comment_articles(response.text)
    assert shown["body"] == "new text"
    assert shown["meta"].endswith("(edited)")
    assert ("comment", "set", "old text", "new text") in changes(detail_app, "OIL-4")
    [entry] = entries(detail_app, "comment.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "comment.edit",
        "B",
        "user",
        "OIL-4",
    )
    assert entry.detail == {"comment_id": comment.id, "text": "new text", "via": "web"}


def test_saving_an_unchanged_comment_writes_neither_history_nor_audit(detail_app: FastAPI, post) -> None:
    comment = add_comment(detail_app, "OIL-4", "same")
    history_before = changes(detail_app, "OIL-4")

    response = post(f"/issues/OIL-4/comments/{comment.id}/edit", {"body": "same"})

    assert response.status_code == 200
    assert "(edited)" not in response.text
    assert changes(detail_app, "OIL-4") == history_before
    assert entries(detail_app) == []


def test_deleting_a_comment_removes_it_from_the_tab_and_records_history_and_audit(
    detail_app: FastAPI, post
) -> None:
    keep = add_comment(detail_app, "OIL-4", "keep")
    gone = add_comment(detail_app, "OIL-4", "gone")

    response = post(f"/issues/OIL-4/comments/{gone.id}/delete")

    assert response.status_code == 200
    assert [a["id"] for a in comment_articles(response.text)] == [str(keep.id)]
    assert ("comment", "delete", "gone", None) in changes(detail_app, "OIL-4")
    with detail_app.state.engine.begin() as conn:
        assert comments.get_comment(conn, gone.id).deleted_at is not None
    [entry] = entries(detail_app, "comment.")
    assert (entry.action, entry.action_class, entry.target) == ("comment.delete", "B", "OIL-4")
    assert entry.detail == {"comment_id": gone.id, "via": "web"}


def test_deleting_a_deleted_comment_again_writes_nothing_more(detail_app: FastAPI, post) -> None:
    comment = add_comment(detail_app, "OIL-4", "once")
    post(f"/issues/OIL-4/comments/{comment.id}/delete")
    history_before = changes(detail_app, "OIL-4")

    response = post(f"/issues/OIL-4/comments/{comment.id}/delete")

    assert response.status_code == 200
    assert changes(detail_app, "OIL-4") == history_before
    assert len(entries(detail_app, "comment.")) == 1


def test_editing_a_deleted_comment_is_a_422(detail_app: FastAPI, post, get) -> None:
    comment = add_comment(detail_app, "OIL-4", "will go")
    post(f"/issues/OIL-4/comments/{comment.id}/delete")
    audit_before = entries(detail_app)

    edit = post(f"/issues/OIL-4/comments/{comment.id}/edit", {"body": "revived"})
    editor = get(f"/issues/OIL-4/comments/{comment.id}/edit")

    for response in (edit, editor):
        assert response.status_code == 422
        assert response.text.startswith(ERROR_PREFIX)
        assert "is deleted and cannot be edited" in response.text
    assert entries(detail_app) == audit_before
    with detail_app.state.engine.begin() as conn:
        assert comments.get_comment(conn, comment.id).body == "will go"


def test_a_comment_of_another_issue_is_a_404_everywhere(detail_app: FastAPI, post, get) -> None:
    comment = add_comment(detail_app, "OIL-5", "belongs to OIL-5")
    path = f"/issues/OIL-4/comments/{comment.id}"

    assert get(f"{path}/edit").status_code == 404
    assert post(f"{path}/edit", {"body": "hijacked"}).status_code == 404
    assert post(f"{path}/delete").status_code == 404
    assert post("/issues/OIL-4/comments/999/delete").status_code == 404
    with detail_app.state.engine.begin() as conn:
        stored = comments.get_comment(conn, comment.id)
    assert (stored.body, stored.deleted_at, stored.edited_at) == ("belongs to OIL-5", None, None)
    assert entries(detail_app) == []


def test_a_comment_and_its_audit_entry_are_one_transaction(
    detail_app: FastAPI, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_oil.errors import ValidationError
    from ai_oil.web.routes import issue_detail

    def refuse(*args, **kwargs):
        raise ValidationError("audit unavailable")

    monkeypatch.setattr(issue_detail.audit, "log", refuse)

    assert post("/issues/OIL-4/comments", {"body": "lost"}).status_code == 422
    with detail_app.state.engine.begin() as conn:
        assert comments.list_comments(conn, "OIL-4") == []


def test_posting_a_comment_without_the_csrf_token_is_refused(detail_app: FastAPI, post) -> None:
    response = post("/issues/OIL-4/comments", {"body": "sneaky"}, csrf=False)

    assert response.status_code == 403
    assert entries(detail_app) == []
    with detail_app.state.engine.begin() as conn:
        assert comments.list_comments(conn, "OIL-4") == []


# --- history tab (UI-23, DM-30 .. DM-33) ------------------------------------------------------------


def history_groups(fragment: str) -> list[tuple[str, list[str]]]:
    """Per change set: the plain text of its head and of each event line."""
    groups = []
    for block in re.findall(r'<div class="change-set">(.*?)</ul>\s*</div>', fragment, re.DOTALL):
        head = re.search(r'<div class="change-head">(.*?)</div>', block, re.DOTALL)
        assert head is not None
        groups.append(
            (plain(head.group(1)), [plain(line) for line in re.findall(r"<li>(.*?)</li>", block, re.DOTALL)])
        )
    return groups


def test_tab_switching_returns_only_the_fragment(detail_client: TestClient, get) -> None:
    for tab, marker in (("comments", 'class="comments"'), ("history", 'class="history"')):
        response = get(f"/issues/OIL-4/activity/{tab}")

        assert response.status_code == 200
        assert marker in response.text
        assert "<html" not in response.text
        assert "<aside" not in response.text
        assert 'role="tablist"' not in response.text


def test_an_unknown_tab_or_issue_is_a_404(get) -> None:
    assert get("/issues/OIL-4/activity/sources").status_code == 404
    assert get("/issues/OIL-99/activity/history").status_code == 404
    assert get("/issues/OIL-99/activity/comments").status_code == 404


def test_the_history_groups_events_by_change_set_newest_first(detail_app: FastAPI, get) -> None:
    def work(conn) -> None:
        me = people.get_me(conn)
        issues.update_issue(conn, "OIL-4", {"priority": "P1", "summary": "Plain item!"}, actor_id=me.id)
        issues.transition_issue(conn, "OIL-4", "In Progress", actor_id=me.id)

    with_conn(detail_app, work)

    groups = history_groups(get("/issues/OIL-4/activity/history").text)

    stamp = local(timeutil.utcnow_iso())
    assert groups == [
        (f"{stamp} You", ["status set: New → In Progress"]),
        (f"{stamp} You", ["summary set: Plain item → Plain item!", "priority set: P4 → P1"]),
        (f"{stamp} You", ["issue create: — → OIL-4"]),
    ]


def test_person_ids_are_resolved_to_current_names_and_none_is_a_dash(detail_app: FastAPI, get) -> None:
    bea = person_id(detail_app, "Bea Brandt")
    me = person_id(detail_app, "Alex")

    def work(conn) -> None:
        issues.update_issue(conn, "OIL-4", {"assignee_id": bea, "reporter_id": bea}, actor_id=me)
        issues.update_issue(conn, "OIL-4", {"assignee_id": None}, actor_id=me)

    with_conn(detail_app, work)

    groups = history_groups(get("/issues/OIL-4/activity/history").text)

    assert groups[0][1] == ["assignee set: Bea Brandt → —"]
    assert sorted(groups[1][1]) == ["assignee set: — → Bea Brandt", "reporter set: Alex → Bea Brandt"]


def test_a_renamed_person_shows_the_new_name_in_old_entries(detail_app: FastAPI, get) -> None:
    bea = person_id(detail_app, "Bea Brandt")

    def work(conn) -> None:
        issues.update_issue(conn, "OIL-4", {"assignee_id": bea}, actor_id=people.get_me(conn).id)
        conn.execute(text("UPDATE person SET display_name = 'Beatrice Brandt' WHERE id = :id"), {"id": bea})

    with_conn(detail_app, work)

    assert history_groups(get("/issues/OIL-4/activity/history").text)[0][1] == [
        "assignee set: — → Beatrice Brandt"
    ]


def test_the_actor_is_you_a_name_sync_or_system(detail_app: FastAPI, get) -> None:
    bea = person_id(detail_app, "Bea Brandt")

    def work(conn) -> None:
        issues.update_issue(conn, "OIL-4", {"priority": "P1"}, actor_id=bea)
        issues.update_issue(conn, "OIL-4", {"priority": "P2"}, actor_id=None, actor_type="sync")
        issues.update_issue(conn, "OIL-4", {"priority": "P3"}, actor_id=None, actor_type="system")

    with_conn(detail_app, work)

    groups = history_groups(get("/issues/OIL-4/activity/history").text)
    stamp = local(timeutil.utcnow_iso())

    assert [head for head, _ in groups[:3]] == [f"{stamp} system", f"{stamp} sync", f"{stamp} Bea Brandt"]
    assert groups[3][0] == f"{stamp} You"


def test_an_accepted_proposal_is_marked_with_its_number(detail_app: FastAPI, get) -> None:
    def work(conn) -> None:
        conn.execute(
            text(
                "INSERT INTO proposal (id, kind, payload_json, rationale, dedup_key)"
                " VALUES (123, 'field_change', '{}', 'why', 'k')"
            )
        )
        issues.update_issue(
            conn, "OIL-4", {"priority": "P1"}, actor_id=people.get_me(conn).id, proposal_id=123
        )

    with_conn(detail_app, work)

    groups = history_groups(get("/issues/OIL-4/activity/history").text)
    stamp = local(timeutil.utcnow_iso())

    assert groups[0] == (f"{stamp} You (accepted AI proposal #123)", ["priority set: P4 → P1"])
    assert "proposal" not in groups[1][0]


def test_timestamps_are_shown_in_local_time(detail_app: FastAPI, get) -> None:
    with_conn(
        detail_app,
        lambda conn: issues.transition_issue(conn, "OIL-4", "Done", actor_id=people.get_me(conn).id),
    )
    resolved = load(detail_app, "OIL-4").resolved_at
    assert resolved is not None

    [group] = [
        g
        for g in history_groups(get("/issues/OIL-4/activity/history").text)
        if "status set" in " ".join(g[1])
    ]

    assert f"resolved_at set: — → {local(resolved)}" in group[1]
    assert "status set: New → Done" in group[1]


def test_long_text_values_are_shortened_to_200_characters(detail_app: FastAPI, get) -> None:
    long_text = "x" * 199 + "ab" + "y" * 50
    comment = add_comment(detail_app, "OIL-4", long_text)

    def work(conn) -> None:
        issues.update_issue(conn, "OIL-4", {"description": "d" * 250}, actor_id=people.get_me(conn).id)

    with_conn(detail_app, work)

    lines = [
        line for _, group in history_groups(get("/issues/OIL-4/activity/history").text) for line in group
    ]

    assert f"description set: Nothing special → {'d' * 200}…" in lines
    assert f"comment add: — → {'x' * 199}a…" in lines
    assert comment.body == long_text
    with detail_app.state.engine.begin() as conn:
        assert issues.get_issue(conn, "OIL-4").description == "d" * 250


def test_a_text_of_exactly_200_characters_is_not_shortened(detail_app: FastAPI, get) -> None:
    with_conn(
        detail_app,
        lambda conn: issues.update_issue(
            conn, "OIL-4", {"description": "d" * 200}, actor_id=people.get_me(conn).id
        ),
    )

    lines = [
        line for _, group in history_groups(get("/issues/OIL-4/activity/history").text) for line in group
    ]

    assert f"description set: Nothing special → {'d' * 200}" in lines


def test_history_values_are_escaped(detail_app: FastAPI, get) -> None:
    with_conn(
        detail_app,
        lambda conn: issues.update_issue(
            conn, "OIL-4", {"summary": "<img src=x onerror=alert(1)>"}, actor_id=people.get_me(conn).id
        ),
    )

    response = get("/issues/OIL-4/activity/history")

    assert "<img" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


def test_the_history_shows_what_the_web_forms_wrote(detail_app: FastAPI, post, get) -> None:
    post("/issues/OIL-4/labels", {"name": "supplier"})
    post("/issues/OIL-4/links", {"relation": "blocks", "other_key": "OIL-5"})
    post("/issues/OIL-4/comments", {"body": "hello"})

    groups = history_groups(get("/issues/OIL-4/activity/history").text)

    assert [lines for _, lines in groups[:3]] == [
        ["comment add: — → hello"],
        ["issue_link add: — → blocks OIL-5"],
        ["label add: — → supplier"],
    ]
