from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import issues, people, projects
from ai_oil.sources import linking, store
from ai_oil.sources.model import ObjectMeta
from ai_oil.web.app import create_app

PORT = 8765


@pytest.fixture
def web_home(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """An initialised data folder with two projects (one archived) and a few issues."""
    path = tmp_path / "web-home"
    assert main(["--home", str(path), "init", "--name", "Alex", "--email", "alex@example.com"]) == 0
    capsys.readouterr()
    engine = create_db_engine(load_config(path).db_path)
    try:
        with engine.begin() as conn:
            me = people.get_me(conn)
            projects.create_project(conn, "OIL", "Open issues")
            projects.create_project(conn, "ABC", "Alphabet")
            projects.create_project(conn, "OLD", "Retired")
            projects.update_project(conn, "OLD", archived=True)
            issues.create_issue(conn, "OIL", "First", "Not started", actor_id=me.id)
            issues.create_issue(conn, "OIL", "Second", "Not started either", actor_id=me.id)
            issues.create_issue(conn, "OIL", "Third", "Running", actor_id=me.id)
            issues.transition_issue(conn, "OIL-3", "In Progress", actor_id=me.id)
            issues.create_issue(conn, "OIL", "Fourth", "Finished", actor_id=me.id)
            issues.transition_issue(conn, "OIL-4", "Done", actor_id=me.id)
            issues.create_issue(conn, "OLD", "Hidden", "In an archived project", actor_id=me.id)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def app(web_home: Path) -> Iterator[FastAPI]:
    engine = create_db_engine(load_config(web_home).db_path)
    try:
        yield create_app(load_config(web_home), engine, port=PORT)
    finally:
        engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


@pytest.fixture
def csrf_token(app: FastAPI) -> str:
    return app.state.csrf.token


@pytest.fixture
def nav_home(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data folder for the navigator: due dates, people, labels and linked sources (one in error)."""
    path = tmp_path / "nav-home"
    assert main(["--home", str(path), "init", "--name", "Alex", "--email", "alex@example.com"]) == 0
    capsys.readouterr()
    engine = create_db_engine(load_config(path).db_path)
    try:
        with engine.begin() as conn:
            me = people.get_me(conn)
            bea = people.create_person(conn, "Bea Brandt")
            projects.create_project(conn, "OIL", "Open issues")

            def make(summary: str, description: str, **kwargs) -> None:
                issues.create_issue(conn, "OIL", summary, description, actor_id=me.id, **kwargs)

            make(
                "Power stage redesign",
                "Inductor lead time is too long",
                priority="P2",
                assignee_id=me.id,
                due_date=date(2026, 10, 15),
                labels=["supplier", "b-sample"],
            )
            make(
                "Quote tooling",
                "Tooling cost quote",
                priority="P1",
                assignee_id=bea.id,
                due_date=date(2026, 10, 20),
            )
            make("Release gerber files", "Gerber release for B-sample", due_date=date(2026, 11, 25))
            make("Finished item", "Done long ago", reporter_id=bea.id, due_date=date(2026, 10, 1))
            make("Another quote", "Contains the word zebra")
            issues.transition_issue(conn, "OIL-2", "Blocked", actor_id=me.id, reason="waiting")
            issues.transition_issue(conn, "OIL-4", "Done", actor_id=me.id)

            connector_id = conn.execute(
                text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
            ).scalar_one()

            def source(
                object_type: str, external_id: str, changed: str | None, *, parent_id: int | None = None
            ) -> int:
                meta = ObjectMeta(object_type, external_id, title=external_id, display_ref=external_id)
                object_id = store.upsert_object(conn, connector_id, meta, parent_id=parent_id)
                conn.execute(
                    text("UPDATE source_object SET last_changed_at = :at WHERE id = :id"),
                    {"at": changed, "id": object_id},
                )
                return object_id

            email = source("email_thread", "thread-1", "2026-10-20T10:00:00.000Z")
            jira = source("jira_issue", "HWDEV-1187", "2026-10-27T08:30:00.000Z")
            conn.execute(text("UPDATE source_object SET fetch_status = 'error' WHERE id = :id"), {"id": jira})
            thread = source("email_thread", "thread-2", "2026-10-22T09:00:00.000Z")
            attachment = source("attachment", "thread-2#1", "2026-10-23T09:00:00.000Z", parent_id=thread)
            conn.execute(
                text("UPDATE source_object SET fetch_status = 'error' WHERE id = :id"), {"id": attachment}
            )
            for key, object_id in (("OIL-1", email), ("OIL-1", jira), ("OIL-2", thread)):
                linking.link_source(conn, key, object_id, actor_id=me.id)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def nav_app(nav_home: Path) -> Iterator[FastAPI]:
    engine = create_db_engine(load_config(nav_home).db_path)
    try:
        yield create_app(load_config(nav_home), engine, port=PORT)
    finally:
        engine.dispose()


@pytest.fixture
def nav_client(nav_app: FastAPI) -> TestClient:
    return TestClient(nav_app, base_url=f"http://127.0.0.1:{PORT}")


@pytest.fixture
def detail_home(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data folder for the issue detail page: a hierarchy, due dates, reasons and a merged person."""
    path = tmp_path / "detail-home"
    assert main(["--home", str(path), "init", "--name", "Alex", "--email", "alex@example.com"]) == 0
    capsys.readouterr()
    engine = create_db_engine(load_config(path).db_path)
    try:
        with engine.begin() as conn:
            me = people.get_me(conn)
            bea = people.create_person(conn, "Bea Brandt")
            twin = people.create_person(conn, "Bea B.")
            people.merge_persons(conn, twin.id, bea.id, actor_id=me.id)
            projects.create_project(conn, "OIL", "Open issues")
            projects.create_project(conn, "ABC", "Alphabet")

            def make(summary: str, description: str, **kwargs) -> None:
                project = kwargs.pop("project", "OIL")
                issues.create_issue(conn, project, summary, description, actor_id=me.id, **kwargs)

            make("Platform", "The platform epic", issue_type="Epic")
            make(
                "Power stage redesign",
                "Inductor lead time is too long",
                parent_key="OIL-1",
                priority="P2",
                assignee_id=me.id,
                reporter_id=bea.id,
                due_date=date(2026, 10, 15),
                acceptance_criteria="Board passes EMC",
            )
            issues.update_issue(conn, "OIL-2", {"due_date": date(2026, 10, 20)}, actor_id=me.id)
            issues.transition_issue(conn, "OIL-2", "Blocked", actor_id=me.id, reason="waiting for supplier")
            make("Finished item", "Done long ago", due_date=date(2026, 10, 1))
            issues.transition_issue(conn, "OIL-3", "Done", actor_id=me.id)
            make("Plain item", "Nothing special")
            make("Dropped item", "Not pursued")
            issues.transition_issue(conn, "OIL-5", "Cancelled", actor_id=me.id, reason="descoped")
            make("Late item", "Past due", due_date=date(2026, 10, 1))
            make("Escaping <b>", "<script>alert(1)</script>\nsecond line")
            make("Alphabet epic", "Epic of another project", project="ABC", issue_type="Epic")
    finally:
        engine.dispose()
    return path


@pytest.fixture
def detail_app(detail_home: Path) -> Iterator[FastAPI]:
    engine = create_db_engine(load_config(detail_home).db_path)
    try:
        yield create_app(load_config(detail_home), engine, port=PORT)
    finally:
        engine.dispose()


@pytest.fixture
def detail_client(detail_app: FastAPI) -> TestClient:
    return TestClient(detail_app, base_url=f"http://127.0.0.1:{PORT}")
