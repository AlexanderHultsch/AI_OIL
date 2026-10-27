from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import issues, people, projects
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
