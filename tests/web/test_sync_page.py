import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil import backup, timeutil
from ai_oil.domain import people
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import ConnectorConfig, FetchResult, ObjectMeta, ObjectState, VersionData


class FakeFetcher:
    """Returns one version per fetch; objects named in `failing` raise."""

    type: ClassVar[str] = "fakefetch"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 1
    failing: ClassVar[set[str]] = set()

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config

    def fetch(self, state: ObjectState) -> FetchResult:
        if state.external_id in self.failing:
            raise RuntimeError("boom")
        return FetchResult(version=VersionData("Some text"), entries=(), cursor="u1")


@pytest.fixture(autouse=True)
def fakefetch_registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FakeFetcher, "failing", set())
    registry.register_connector_type(FakeFetcher)
    yield
    registry.unregister_connector_type("fakefetch")


T0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)


def freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: moment)


@pytest.fixture
def post(client: TestClient, csrf_token: str):
    def send(path: str, *, csrf: bool = True):
        headers = {"X-CSRF-Token": csrf_token} if csrf else {}
        return client.post(path, headers=headers, follow_redirects=False)

    return send


def local(stamp: str) -> str:
    return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def run_block(page: str, run_id: int) -> str:
    match = re.search(
        rf'<tbody class="sync-run" id="run-{run_id}">.*?</tbody>\s*(?=<tbody class="sync-run"|</table>)',
        page,
        re.DOTALL,
    )
    assert match is not None, f"no block for run {run_id}"
    return match.group(0)


def run_ids(page: str) -> list[int]:
    return [int(i) for i in re.findall(r'<tbody class="sync-run" id="run-(\d+)">', page)]


def add_run(
    app: FastAPI,
    started_at: str,
    *,
    trigger: str = "cli",
    scope: str = "all",
    status: str = "ok",
    stats: dict | None = None,
) -> int:
    with app.state.engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO sync_run (trigger, scope, started_at, status, stats_json)"
                " VALUES (:trigger, :scope, :at, :status, :stats) RETURNING id"
            ),
            {
                "trigger": trigger,
                "scope": scope,
                "at": started_at,
                "status": status,
                "stats": json.dumps(stats or {}),
            },
        ).scalar_one()


def add_object(
    app: FastAPI, connector_name: str, external_id: str, *, display_ref: str | None = None, link: bool = False
) -> int:
    with app.state.engine.begin() as conn:
        connector_id = conn.execute(
            text("SELECT id FROM connector WHERE name = :name"), {"name": connector_name}
        ).scalar_one_or_none()
        if connector_id is None:
            connector_id = registry.create_connector(conn, "fakefetch", connector_name).id
        meta = ObjectMeta("jira_issue", external_id, title=f"Title {external_id}", display_ref=display_ref)
        object_id = store.upsert_object(conn, connector_id, meta)
        if link:
            linking.link_source(conn, "OIL-1", object_id, actor_id=people.get_me(conn).id)
    return object_id


def add_result(
    app: FastAPI,
    run_id: int,
    object_id: int,
    outcome: str,
    *,
    error: str | None = None,
    duration_ms: int | None = 120,
) -> None:
    with app.state.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sync_result (sync_run_id, source_object_id, outcome, error, duration_ms)"
                " VALUES (:run, :object, :outcome, :error, :duration)"
            ),
            {"run": run_id, "object": object_id, "outcome": outcome, "error": error, "duration": duration_ms},
        )


def rows(app: FastAPI, sql: str, **params: object):
    with app.state.engine.begin() as conn:
        return conn.execute(text(sql), params).all()


# --- the log -----------------------------------------------------------------------------------------------


def test_the_page_is_in_the_navigation_and_has_the_button(client: TestClient) -> None:
    page = client.get("/sync").text

    assert '<a href="/sync" class="active" aria-current="page">Sync log</a>' in page
    assert "No sync has run yet." in page
    assert 'action="/sync/all"' in page
    assert ">Sync all now</button>" in page


def test_runs_are_listed_newest_first(app: FastAPI, client: TestClient) -> None:
    oldest = add_run(app, "2026-10-20T08:00:00.000Z")
    newest = add_run(app, "2026-10-26T08:00:00.000Z")
    middle = add_run(app, "2026-10-23T08:00:00.000Z")

    assert run_ids(client.get("/sync").text) == [newest, middle, oldest]


def test_only_the_latest_fifty_runs_are_shown(app: FastAPI, client: TestClient) -> None:
    ids = [
        add_run(app, f"2026-09-{day:02d}T{hour:02d}:00:00.000Z") for day in range(1, 29) for hour in (6, 7)
    ]
    ids += [add_run(app, f"2026-10-{day:02d}T06:00:00.000Z") for day in range(1, 6)]
    assert len(ids) == 61

    shown = run_ids(client.get("/sync").text)

    assert len(shown) == 50
    assert shown[:5] == ids[:-6:-1]
    assert ids[0] not in shown


def test_a_run_shows_start_trigger_scope_status_duration_and_stats(app: FastAPI, client: TestClient) -> None:
    run_id = add_run(
        app,
        "2026-10-26T08:15:00.000Z",
        trigger="ui",
        scope="issue:OIL-2",
        status="partial",
        stats={
            "checked": 12,
            "new": 2,
            "changed": 3,
            "unchanged": 6,
            "errors": 1,
            "new_versions": 4,
            "new_entries": 9,
            "duration_ms": 2500,
        },
    )

    block = run_block(client.get("/sync").text, run_id)
    cells = [
        plain(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", block.split("sync-details")[0], re.DOTALL)
    ]

    assert cells == [
        local("2026-10-26T08:15:00.000Z"),
        "ui",
        "issue:OIL-2",
        "partial",
        "2.5 s",
        "12",
        "5",
        "4",
        "9",
        "1",
    ]
    assert '<span class="badge status-partial">partial</span>' in block


def test_a_short_run_is_shown_in_milliseconds_and_a_running_one_has_dashes(
    app: FastAPI, client: TestClient
) -> None:
    quick = add_run(app, "2026-10-26T08:00:00.000Z", stats={"checked": 1, "duration_ms": 340})
    running = add_run(app, "2026-10-26T09:00:00.000Z", status="running")

    page = client.get("/sync").text

    assert "340 ms" in run_block(page, quick)
    cells = [
        plain(c)
        for c in re.findall(
            r"<td[^>]*>(.*?)</td>", run_block(page, running).split("sync-details")[0], re.DOTALL
        )
    ]
    assert cells[3:] == ["running", "—", "—", "—", "—", "—", "—"]


def test_the_results_list_everything_but_unchanged_with_source_connector_error_and_duration(
    app: FastAPI, client: TestClient
) -> None:
    run_id = add_run(app, "2026-10-26T08:00:00.000Z", stats={"checked": 4})
    kept = add_object(app, "Jira main", "HWDEV-1", display_ref="HWDEV-1")
    failed = add_object(app, "Jira main", "HWDEV-2", display_ref="HWDEV-2")
    quiet = add_object(app, "Confluence", "page-9", display_ref="Quiet page")
    titled = add_object(app, "Confluence", "page-10")
    add_result(app, run_id, kept, "changed", duration_ms=1500)
    add_result(app, run_id, failed, "error", error="RuntimeError: boom <b>", duration_ms=30)
    add_result(app, run_id, quiet, "unchanged")
    add_result(app, run_id, titled, "new", duration_ms=None)

    block = run_block(client.get("/sync").text, run_id)

    assert "3 results other than unchanged" in block
    assert "Quiet page" not in block
    results = re.findall(r'<table class="sync-results">.*?<tbody>(.*?)</tbody>', block, re.DOTALL)[0]
    cells = [
        [plain(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
        for row in re.findall(r"<tr>(.*?)</tr>", results, re.DOTALL)
    ]
    assert cells == [
        ["changed", "HWDEV-1", "Jira main", "", "1.5 s"],
        ["error", "HWDEV-2", "Jira main", "RuntimeError: boom &lt;b&gt;", "30 ms"],
        ["new", "Title page-10", "Confluence", "", "—"],
    ]
    assert f'<a href="/sources/{kept}">HWDEV-1</a>' in block
    assert "boom <b>" not in block  # escaped
    assert "boom &lt;b&gt;" in block


def test_a_run_without_other_results_has_an_empty_details_element(app: FastAPI, client: TestClient) -> None:
    run_id = add_run(app, "2026-10-26T08:00:00.000Z", stats={"checked": 1})
    add_result(app, run_id, add_object(app, "Jira main", "HWDEV-1"), "unchanged")

    block = run_block(client.get("/sync").text, run_id)

    assert "0 results other than unchanged" in block
    assert '<table class="sync-results">' not in block


def test_details_are_collapsed_unless_the_run_is_asked_for(app: FastAPI, client: TestClient) -> None:
    first = add_run(app, "2026-10-25T08:00:00.000Z")
    second = add_run(app, "2026-10-26T08:00:00.000Z")

    collapsed = client.get("/sync").text
    opened = client.get(f"/sync?run={first}").text

    assert "<details open>" not in collapsed
    assert "<details open>" in run_block(opened, first)
    assert "<details open>" not in run_block(opened, second)


def test_an_unknown_run_parameter_opens_nothing_and_a_bad_one_is_refused(
    app: FastAPI, client: TestClient
) -> None:
    add_run(app, "2026-10-25T08:00:00.000Z")

    assert "<details open>" not in client.get("/sync?run=999").text
    assert client.get("/sync?run=abc").status_code == 422


# --- Sync all now ------------------------------------------------------------------------------------------


def test_sync_all_now_runs_a_sync_with_trigger_ui_and_redirects_to_the_log(
    app: FastAPI, client: TestClient, post
) -> None:
    object_id = add_object(app, "Jira main", "HWDEV-1", display_ref="HWDEV-1", link=True)

    response = post("/sync/all")

    assert (response.status_code, response.headers["location"]) == (303, "/sync")
    [run] = rows(app, "SELECT id, trigger, scope, status FROM sync_run")
    assert tuple(run)[1:] == ("ui", "all", "ok")
    [result] = rows(
        app, "SELECT source_object_id, outcome FROM sync_result WHERE sync_run_id = :id", id=run[0]
    )
    assert tuple(result) == (object_id, "new")
    page = client.get("/sync").text
    block = run_block(page, run[0])
    assert '<span class="badge status-ok">ok</span>' in block
    assert "<td>ui</td>" in block
    assert f'<a href="/sources/{object_id}">HWDEV-1</a>' in block


def test_a_failing_object_makes_the_run_partial_and_shows_its_error(
    app: FastAPI, client: TestClient, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeFetcher, "failing", {"HWDEV-2"})
    add_object(app, "Jira main", "HWDEV-1", link=True)
    add_object(app, "Jira main", "HWDEV-2", link=True)

    post("/sync/all")

    [run_id] = [r[0] for r in rows(app, "SELECT id FROM sync_run")]
    block = run_block(client.get("/sync").text, run_id)
    assert '<span class="badge status-partial">partial</span>' in block
    assert "RuntimeError: boom" in block


def test_sync_all_now_with_nothing_to_sync_still_logs_a_run(app: FastAPI, client: TestClient, post) -> None:
    assert post("/sync/all").status_code == 303

    [run_id] = [r[0] for r in rows(app, "SELECT id FROM sync_run")]
    assert run_ids(client.get("/sync").text) == [run_id]


def test_sync_all_now_needs_the_csrf_token(app: FastAPI, post) -> None:
    response = post("/sync/all", csrf=False)

    assert response.status_code == 403
    assert rows(app, "SELECT id FROM sync_run") == []


def test_sync_all_is_a_post_only_endpoint(client: TestClient) -> None:
    assert client.get("/sync/all").status_code == 405


# --- Backup now --------------------------------------------------------------------------------------------


def test_the_page_has_the_backup_button(client: TestClient) -> None:
    page = client.get("/sync").text

    assert 'action="/sync/backup"' in page
    assert ">Backup now</button>" in page
    assert "Backup written" not in page


def test_backup_now_writes_a_backup_and_the_log_shows_its_path(
    app: FastAPI, client: TestClient, post, set_backup_folder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "bk"
    set_backup_folder(app, folder)
    freeze(monkeypatch, T0)

    response = post("/sync/backup")

    written = folder / "ai_oil-20261026-091500.db"
    assert written.is_file()
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"/sync?backup={quote(str(written), safe='')}",
    )
    page = client.get(response.headers["location"]).text
    assert f"Backup written: {written}" in plain(page)
    assert "Backup written" not in client.get("/sync").text
    with app.state.engine.begin() as conn:
        [entry] = audit.list_entries(conn, action_prefix="backup.create")
    assert entry.target == str(written)


def test_backup_now_ignores_the_age_of_the_last_backup(
    app: FastAPI, post, set_backup_folder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "bk"
    set_backup_folder(app, folder)
    freeze(monkeypatch, T0)
    post("/sync/backup")
    freeze(monkeypatch, T0 + timedelta(minutes=1))

    post("/sync/backup")

    assert backups_in(folder) == ["ai_oil-20261026-091500.db", "ai_oil-20261026-091600.db"]


def test_backup_now_without_a_folder_is_a_422_error_page(app: FastAPI, post) -> None:
    response = post("/sync/backup")

    assert response.status_code == 422
    assert "no backup folder configured" in response.text


def test_backup_now_needs_the_csrf_token_and_is_post_only(app: FastAPI, client: TestClient, post) -> None:
    assert post("/sync/backup", csrf=False).status_code == 403
    assert client.get("/sync/backup").status_code == 405


def test_the_backup_parameters_are_escaped(client: TestClient) -> None:
    page = client.get("/sync?backup=<b>x</b>&backup_error=<i>y</i>").text

    assert "Backup written: &lt;b&gt;x&lt;/b&gt;" in page
    assert "Automatic backup failed: &lt;i&gt;y&lt;/i&gt;" in page
    assert "<b>x</b>" not in page and "<i>y</i>" not in page


# --- the automatic backup after "Sync all now" -------------------------------------------------------------


def backups_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.glob(backup.BACKUP_PATTERN))


def test_sync_all_now_writes_a_backup_when_due_and_says_so(
    app: FastAPI, client: TestClient, post, set_backup_folder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "bk"
    set_backup_folder(app, folder)
    freeze(monkeypatch, T0)

    response = post("/sync/all")

    written = folder / "ai_oil-20261026-091500.db"
    assert backups_in(folder) == [written.name]
    assert response.status_code == 303
    assert response.headers["location"] == f"/sync?backup={quote(str(written), safe='')}"
    assert f"Backup written: {written}" in plain(client.get(response.headers["location"]).text)


def test_a_recent_backup_is_not_repeated_after_the_next_sync(
    app: FastAPI, client: TestClient, post, set_backup_folder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "bk"
    set_backup_folder(app, folder)
    freeze(monkeypatch, T0)
    post("/sync/all")
    freeze(monkeypatch, T0 + timedelta(hours=1))

    response = post("/sync/all")

    assert (response.status_code, response.headers["location"]) == (303, "/sync")
    assert backups_in(folder) == ["ai_oil-20261026-091500.db"]
    assert "Backup written" not in client.get("/sync").text

    freeze(monkeypatch, T0 + timedelta(hours=25))
    response = post("/sync/all")

    assert "ai_oil-20261027-101500.db" in response.headers["location"]
    assert len(backups_in(folder)) == 2


def test_a_failing_automatic_backup_is_shown_and_the_sync_is_still_recorded(
    app: FastAPI, client: TestClient, post, set_backup_folder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_backup_folder(app, tmp_path / "bk")

    def explode(*args: object, **kwargs: object) -> Path:
        raise RuntimeError("disk full & <b>")

    monkeypatch.setattr(backup, "create_backup", explode)
    add_object(app, "Jira main", "HWDEV-1", display_ref="HWDEV-1", link=True)

    response = post("/sync/all")

    assert response.status_code == 303
    [run] = rows(app, "SELECT id, trigger, scope, status FROM sync_run")
    assert tuple(run)[1:] == ("ui", "all", "ok")
    page = client.get(response.headers["location"]).text
    assert "Automatic backup failed: disk full &amp; &lt;b&gt;" in page
    assert "Backup written" not in page
    assert run_ids(page) == [run[0]]


def test_sync_all_now_without_a_backup_folder_mentions_no_backup(client: TestClient, post) -> None:
    response = post("/sync/all")

    assert response.headers["location"] == "/sync"
    assert "backup failed" not in client.get("/sync").text
