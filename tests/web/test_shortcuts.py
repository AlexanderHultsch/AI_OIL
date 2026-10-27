"""UI-4: the script holds the handlers and the pages carry what they rely on (the browser test runs them)."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_oil.web.app import STATIC_DIR


@pytest.fixture(scope="module")
def script() -> str:
    return (Path(STATIC_DIR) / "app.js").read_text(encoding="utf-8")


def test_the_script_is_served(client: TestClient, script: str) -> None:
    assert client.get("/static/app.js").text == script


@pytest.mark.parametrize("key", ["c", "/", "j", "k", "e", "m", "Enter"])
def test_the_script_handles_every_shortcut_key(script: str, key: str) -> None:
    assert f'case "{key}":' in script


def test_the_script_ignores_typing_and_modifier_keys(script: str) -> None:
    for fragment in (
        "event.ctrlKey",
        "event.altKey",
        "event.metaKey",
        '"input"',
        '"textarea"',
        '"select"',
        "isContentEditable",
    ):
        assert fragment in script
    assert "shiftKey" not in script.split("keydown")[1].split("switch")[0]  # "/" needs Shift on some layouts


def test_the_script_uses_the_ids_and_classes_the_pages_carry(script: str) -> None:
    for fragment in (
        "/issues/new",
        "global-search",
        "kb-selected",
        "data-issue-key",
        "edit-summary",
        "comment-body",
        "tab-comments",
    ):
        assert fragment in script


def test_every_page_has_the_search_input_and_the_help_line(client: TestClient) -> None:
    for path in ("/", "/issues", "/connectors", "/sync", "/people"):
        page = client.get(path).text
        assert re.search(r'<input type="search" id="global-search" name="q"', page), path
        assert "Keyboard shortcuts:" in page, path


def test_the_help_line_names_every_shortcut(client: TestClient) -> None:
    help_line = re.search(
        r'<footer class="shortcuts-help[^"]*">(.*?)</footer>', client.get("/").text, re.DOTALL
    )
    assert help_line is not None

    keys = re.findall(r"<kbd>(.*?)</kbd>", help_line.group(1))

    assert keys == ["c", "/", "j", "k", "Enter", "e", "m"]


def test_navigator_rows_carry_the_issue_key(nav_client: TestClient) -> None:
    page = nav_client.get("/issues").text

    keys = re.findall(r'<tr data-issue-key="([^"]+)">', page)
    assert keys
    assert keys == re.findall(r'<td class="key"><a href="/issues/([^"]+)">', page)


def test_the_issue_page_has_the_summary_edit_button(detail_client: TestClient) -> None:
    page = detail_client.get("/issues/OIL-4").text

    button = re.search(
        r'<div class="field field-summary" id="field-summary">.*?<button ([^>]*)>', page, re.DOTALL
    )
    assert button is not None
    assert 'id="edit-summary"' in button.group(1)
    assert 'hx-get="/issues/OIL-4/edit/summary"' in button.group(1)
    assert page.count('id="edit-summary"') == 1


def test_the_comments_tab_has_the_comment_textarea_and_the_tab_button_exists(
    detail_client: TestClient,
) -> None:
    page = detail_client.get("/issues/OIL-4").text
    tab = detail_client.get("/issues/OIL-4/activity/comments").text

    assert 'id="tab-comments"' in page
    assert len(re.findall(r'<textarea name="body" id="comment-body"', tab)) == 1
