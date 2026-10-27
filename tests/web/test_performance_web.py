"""NFR-3: the issue list and the issue detail page load in < 300 ms at 1,000 issues."""

import random
import shutil
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil.config import Config, load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import comments, issues, people, projects
from ai_oil.sources import activity, linking, store
from ai_oil.sources.model import EntryData, FetchResult, ObjectMeta, VersionData
from ai_oil.web.app import create_app
from ai_oil.web.routes.issues import MAX_ROWS

pytestmark = pytest.mark.performance

PORT = 8765
ISSUES = 1_000
BUSY_KEY = "PERF-500"
BUSY_SOURCES = 10
ENTRIES_PER_SOURCE = 8
BUSY_COMMENTS = 15
LIMIT = 0.300

_PERSONS = ("Bea Brandt", "Carl Conrad", "Dana Dietz", "Emil Eberle")
_TYPES = ("Epic", "Task", "Task", "Task")
_PRIORITIES = ("P1", "P2", "P3", "P3", "P4", "P4", "P5")
_LABELS = ("supplier", "b-sample", "emc", "tooling", "quality")
_OBJECT_TYPES = ("email_thread", "jira_issue", "confluence_page")


def _seed_busy_issue(conn, me_id: int) -> None:
    """One issue with 10 linked sources, two versions and eight entries each, and comments (~115 items)."""
    connector_id = conn.execute(
        text("INSERT INTO connector (type, name) VALUES ('perf', 'Perf') RETURNING id")
    ).scalar_one()
    for number in range(BUSY_SOURCES):
        object_type = _OBJECT_TYPES[number % len(_OBJECT_TYPES)]
        meta = ObjectMeta(object_type, f"obj-{number}", title=f"Source {number}", display_ref=f"SRC-{number}")
        object_id = store.upsert_object(conn, connector_id, meta)
        linking.link_source(conn, BUSY_KEY, object_id, actor_id=me_id)
        for revision in (1, 2):
            entry_type = {
                "email_thread": "email_message",
                "jira_issue": "jira_comment",
                "confluence_page": "confluence_comment",
            }[object_type]
            entries = tuple(
                EntryData(
                    entry_type,
                    f"e-{number}-{k}",
                    f"2026-10-{1 + k:02d}T10:{number:02d}:00.000Z",
                    f"Entry {k} of source {number}: the supplier confirmed the sample date",
                )
                for k in range(ENTRIES_PER_SOURCE)
            )
            state = store.object_state(conn, object_id)
            store.apply_fetch(
                conn,
                state,
                FetchResult(
                    version=VersionData(f"Source {number} text, revision {revision}"), entries=entries
                ),
            )
    for number in range(BUSY_COMMENTS):
        comments.add_comment(conn, BUSY_KEY, f"Comment {number} on the busy issue", author_id=me_id)
    for number in range(5):
        issues.update_issue(conn, BUSY_KEY, {"priority": _PRIORITIES[number]}, actor_id=me_id)


def _seed_perf(path: Path) -> None:
    """An initialised data folder with 1,000 issues of varied fields and one busy issue."""
    engine = create_db_engine(load_config(path).db_path)
    rng = random.Random(42)
    try:
        with engine.begin() as conn:
            me = people.get_me(conn)
            person_ids = [me.id] + [people.create_person(conn, name).id for name in _PERSONS]
            projects.create_project(conn, "PERF", "Performance")
            projects.create_project(conn, "OTHER", "Another project")
            for number in range(1, ISSUES + 1):
                issues.create_issue(
                    conn,
                    "PERF",
                    f"Perf issue {number} for the supplier review",
                    f"Description of perf issue {number}",
                    actor_id=me.id,
                    issue_type=rng.choice(_TYPES),
                    priority=rng.choice(_PRIORITIES),
                    assignee_id=rng.choice(person_ids) if rng.random() < 0.9 else None,
                    reporter_id=rng.choice(person_ids),
                    due_date=date(2026, 8, 1) + timedelta(days=rng.randint(0, 300))
                    if rng.random() < 0.8
                    else None,
                    labels=rng.sample(_LABELS, rng.randint(0, 3)),
                )
                draw = rng.random()
                if draw < 0.25:
                    issues.transition_issue(conn, f"PERF-{number}", "Done", actor_id=me.id)
                elif draw < 0.45:
                    issues.transition_issue(conn, f"PERF-{number}", "In Progress", actor_id=me.id)
                elif draw < 0.50:
                    issues.transition_issue(
                        conn, f"PERF-{number}", "Blocked", actor_id=me.id, reason="waiting for samples"
                    )
            _seed_busy_issue(conn, me.id)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def perf_app(home_templates, tmp_path_factory: pytest.TempPathFactory) -> Iterator[FastAPI]:
    path = tmp_path_factory.mktemp("perf-web") / "home"
    shutil.copytree(home_templates.get("perf-web", _seed_perf), path)
    config: Config = load_config(path)
    engine = create_db_engine(config.db_path)
    try:
        yield create_app(config, engine, port=PORT)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def perf_client(perf_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(perf_app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield client


def _best_of_three(client: TestClient, path: str) -> tuple[float, str]:
    best = float("inf")
    body = ""
    for _ in range(3):
        started = time.perf_counter()
        response = client.get(path)
        best = min(best, time.perf_counter() - started)
        assert response.status_code == 200
        body = response.text
    return best, body


def test_dataset_has_the_specified_shape(perf_app: FastAPI) -> None:
    with perf_app.state.engine.begin() as conn:
        issue_count = conn.execute(text("SELECT count(*) FROM issue")).scalar_one()
        open_count = conn.execute(
            text("SELECT count(*) FROM issue WHERE status NOT IN ('Done', 'Cancelled')")
        ).scalar_one()
        linked = len(linking.linked_object_ids(conn, BUSY_KEY))
        items = activity.issue_activity(conn, BUSY_KEY, limit=1_000)

    assert issue_count == ISSUES
    assert open_count > MAX_ROWS
    assert linked == BUSY_SOURCES
    assert 100 <= len(items) <= 150


def test_issue_list_loads_in_under_300_ms(perf_client: TestClient) -> None:
    seconds, body = _best_of_three(perf_client, "/issues")

    rows = body.count('<tr data-issue-key="')
    print(f"\nNFR-3 issue list: {seconds * 1000:.1f} ms for {rows} rows (limit {LIMIT * 1000:.0f} ms)")
    assert rows == MAX_ROWS
    assert seconds < LIMIT


def test_issue_detail_loads_in_under_300_ms(perf_client: TestClient) -> None:
    seconds, body = _best_of_three(perf_client, f"/issues/{BUSY_KEY}")

    print(
        f"\nNFR-3 issue detail: {seconds * 1000:.1f} ms with {BUSY_SOURCES} sources"
        f" (limit {LIMIT * 1000:.0f} ms)"
    )
    assert body.count('<li class="source" id="source-') == BUSY_SOURCES
    assert seconds < LIMIT
