import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil.connectors import manual
from ai_oil.domain import people
from ai_oil.governance import audit
from ai_oil.sources import linking, store
from ai_oil.sources.model import ObjectMeta


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


@pytest.fixture
def post(detail_client: TestClient, detail_app: FastAPI):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        headers = {"X-CSRF-Token": detail_app.state.csrf.token} if csrf else {}
        return detail_client.post(path, data=data or {}, headers=headers, follow_redirects=False)

    return send


@pytest.fixture
def note(detail_app: FastAPI) -> int:
    with detail_app.state.engine.begin() as conn:
        note_id = manual.create_note(conn, "Meeting note", "Line one\nLine two")
        linking.link_source(conn, "OIL-4", note_id, actor_id=people.get_me(conn).id)
    return note_id


def versions(app: FastAPI, source_id: int) -> list[str]:
    with app.state.engine.begin() as conn:
        return [v.text for v in store.list_versions(conn, source_id)]


def title_of(app: FastAPI, source_id: int) -> str | None:
    with app.state.engine.begin() as conn:
        return store.get_object(conn, source_id).title


def edits(app: FastAPI) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix="note.edit")


def test_a_note_page_has_the_form_prefilled_with_title_and_latest_text(
    detail_app: FastAPI, detail_client: TestClient, note: int
) -> None:
    with detail_app.state.engine.begin() as conn:
        manual.edit_note(conn, note, "Line one\nLine 2 <b>&\nLine three")

    page = detail_client.get(f"/sources/{note}").text

    assert "<h2>Edit note</h2>" in page
    form = re.search(
        rf'<form class="note-edit" action="/sources/{note}/note" method="post">.*?</form>', page, re.DOTALL
    )
    assert form is not None
    assert 'name="csrf_token"' in form.group(0)
    assert re.search(r'<input type="text" name="title" value="Meeting note"', form.group(0))
    area = re.search(r'<textarea name="text"[^>]*>\n(.*?)</textarea>', form.group(0), re.DOTALL)
    assert area is not None
    assert area.group(1) == "Line one\nLine 2 &lt;b&gt;&amp;\nLine three"
    assert ">Save note</button>" in form.group(0)


def test_the_full_text_is_prefilled_even_beyond_the_display_limit(
    detail_app: FastAPI, detail_client: TestClient, note: int
) -> None:
    body = "\n".join(f"row {n}" for n in range(1, 451))
    with detail_app.state.engine.begin() as conn:
        manual.edit_note(conn, note, body)

    page = detail_client.get(f"/sources/{note}").text

    assert "… (50 more lines)" in page
    assert page.count("row 450") == 1
    assert re.search(r"<textarea[^>]*>\n" + re.escape(body) + "</textarea>", page)


def test_other_source_types_have_no_note_form(detail_app: FastAPI, detail_client: TestClient) -> None:
    with detail_app.state.engine.begin() as conn:
        connector = manual.ensure_manual_connector(conn)
        link = store.upsert_object(
            conn,
            connector.id,
            ObjectMeta("web_link", "https://example.com/a", display_ref="example.com/a"),
        )

    page = detail_client.get(f"/sources/{link}").text

    assert "Edit note" not in page
    assert "note-edit" not in page


def test_editing_the_text_stores_a_new_version_and_audits_it(
    detail_app: FastAPI, detail_client: TestClient, post, note: int
) -> None:
    response = post(
        f"/sources/{note}/note", {"title": "Meeting note", "text": "Line one\nLine two\nLine three"}
    )

    assert (response.status_code, response.headers["location"]) == (303, f"/sources/{note}")
    assert versions(detail_app, note) == ["Line one\nLine two", "Line one\nLine two\nLine three"]
    [entry] = edits(detail_app)
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "note: Meeting note")
    with detail_app.state.engine.begin() as conn:
        version_id = store.list_versions(conn, note)[-1].id
    assert entry.detail == {
        "source_object_id": note,
        "version_id": version_id,
        "changed": True,
        "via": "web",
    }
    page = detail_client.get(f"/sources/{note}").text
    assert '<pre class="source-text text">Line one\nLine two\nLine three</pre>' in page
    assert "Counts 2 versions" in plain(page)


def test_editing_only_the_title_is_a_change_without_a_new_version(
    detail_app: FastAPI, detail_client: TestClient, post, note: int
) -> None:
    response = post(f"/sources/{note}/note", {"title": "Supplier call", "text": "Line one\nLine two"})

    assert response.status_code == 303
    assert title_of(detail_app, note) == "Supplier call"
    assert len(versions(detail_app, note)) == 1
    [entry] = edits(detail_app)
    assert entry.target == "note: Supplier call"
    assert entry.detail == {"source_object_id": note, "version_id": None, "changed": True, "via": "web"}
    assert f"<h1>#{note} note Supplier call</h1>" in detail_client.get(f"/sources/{note}").text


def test_saving_without_a_change_is_audited_as_unchanged(detail_app: FastAPI, post, note: int) -> None:
    post(f"/sources/{note}/note", {"title": "Meeting note", "text": "Line one\nLine two"})

    assert len(versions(detail_app, note)) == 1
    [entry] = edits(detail_app)
    assert entry.detail["changed"] is False
    assert entry.detail["via"] == "web"


def test_the_line_breaks_of_a_browser_form_do_not_make_a_new_version(
    detail_app: FastAPI, post, note: int
) -> None:
    post(f"/sources/{note}/note", {"title": "Meeting note", "text": "Line one\r\nLine two"})

    assert versions(detail_app, note) == ["Line one\nLine two"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"title": "Meeting note", "text": "  \n"}, "a note needs text"),
        ({"title": "Meeting note", "text": ""}, "a note needs text"),
        ({"title": "   ", "text": "New text"}, "a note needs a title"),
        ({"title": "", "text": "New text"}, "a note needs a title"),
        ({}, "a note needs text"),
    ],
)
def test_blank_text_or_title_is_a_422_error_page_and_stores_nothing(
    detail_app: FastAPI, post, note: int, data: dict, message: str
) -> None:
    response = post(f"/sources/{note}/note", data)

    assert response.status_code == 422
    assert message in response.text
    assert versions(detail_app, note) == ["Line one\nLine two"]
    assert title_of(detail_app, note) == "Meeting note"
    assert edits(detail_app) == []


def test_a_source_that_is_not_a_note_is_a_422(detail_app: FastAPI, post) -> None:
    with detail_app.state.engine.begin() as conn:
        connector = manual.ensure_manual_connector(conn)
        link = store.upsert_object(
            conn, connector.id, ObjectMeta("web_link", "https://example.com/a", display_ref="example.com/a")
        )
        conn.execute(text("UPDATE source_object SET title = 'Kept' WHERE id = :id"), {"id": link})

    response = post(f"/sources/{link}/note", {"title": "Changed", "text": "Some text"})

    assert response.status_code == 422
    assert "not a note" in response.text
    assert title_of(detail_app, link) == "Kept"
    assert edits(detail_app) == []


def test_an_unknown_source_is_a_404(post) -> None:
    assert post("/sources/999/note", {"title": "T", "text": "X"}).status_code == 404


def test_the_form_needs_the_csrf_token(detail_app: FastAPI, post, note: int) -> None:
    response = post(f"/sources/{note}/note", {"title": "T", "text": "New text"}, csrf=False)

    assert response.status_code == 403
    assert len(versions(detail_app, note)) == 1
