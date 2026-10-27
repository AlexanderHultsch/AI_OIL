from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
import sqlalchemy

from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine


@pytest.fixture
def home(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """An initialised data folder whose 'me' person is Alex <alex@example.com>."""
    path = tmp_path / "cli-home"
    assert main(["--home", str(path), "init", "--name", "Alex", "--email", "alex@example.com"]) == 0
    capsys.readouterr()
    return path


@pytest.fixture
def oil(home: Path) -> Callable[..., int]:
    """Run `oil` against the initialised data folder and return the exit code."""

    def run(*argv: str) -> int:
        return main(["--home", str(home), *argv])

    return run


@pytest.fixture
def db(home: Path) -> Callable[[], AbstractContextManager[sqlalchemy.Connection]]:
    """Open a connection to the database of the initialised data folder, for checking effects."""

    @contextmanager
    def connect() -> Iterator[sqlalchemy.Connection]:
        engine = create_db_engine(load_config(home).db_path)
        try:
            with engine.connect() as conn:
                yield conn
        finally:
            engine.dispose()

    return connect
