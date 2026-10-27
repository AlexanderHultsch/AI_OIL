import contextlib
import io
import itertools
import re
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.cli.main import main
from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade

REAL_NOW = timeutil._now
_TMP_NUMBERS = itertools.count()


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Keyring backend that keeps credentials in a dict, so tests never touch a real credential store."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("password not found") from None


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory unique to each test.

    Replaces pytest's own fixture, which scans the whole base directory for the next free number on every
    call and so gets slower with every test of the session; the name is numbered from a counter instead.
    """
    name = re.sub(r"\W", "_", request.node.name)[:30]
    return tmp_path_factory.mktemp(f"{name}-{next(_TMP_NUMBERS)}", numbered=False)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory keyring and a per-test data folder for every test."""
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    monkeypatch.setenv("AI_OIL_HOME", str(tmp_path / "home"))
    yield
    keyring.set_keyring(previous)


@pytest.fixture(scope="session")
def migrated_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly migrated database as one self-contained file; tests only ever work on copies of it."""
    path = tmp_path_factory.mktemp("migrated-db") / "template.db"
    template_engine = create_db_engine(path)
    try:
        upgrade(template_engine)
    finally:
        template_engine.dispose()
    if path.with_name(path.name + "-wal").exists():
        raise RuntimeError("the template database must be a single file without a write-ahead log")
    return path


@pytest.fixture
def engine_template(migrated_db_template: Path) -> Path:
    """The database file each test's `engine` starts from; a test module may override it with a seeded one."""
    return migrated_db_template


@pytest.fixture
def engine(tmp_path, engine_template: Path) -> Iterator[sqlalchemy.Engine]:
    db_path = tmp_path / "test.db"
    shutil.copyfile(engine_template, db_path)
    db_engine = create_db_engine(db_path)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def conn(engine: sqlalchemy.Engine) -> Iterator[sqlalchemy.Connection]:
    with engine.begin() as connection:
        yield connection


class HomeTemplates:
    """Initialised (and optionally seeded) data folders, built once per session and clock, copied per test."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._built: dict[tuple[str, datetime | None], Path] = {}

    def get(self, name: str, seed: Callable[[Path], None] | None = None) -> Path:
        """The template folder for `name`, built under the clock the calling test runs with.

        Data written with a frozen clock carries that instant, so each distinct frozen instant gets its own
        template; a live clock shares one. `seed` receives the freshly initialised folder.
        """
        clock = None if timeutil._now is REAL_NOW else timeutil._now()
        key = (name, clock)
        if key not in self._built:
            path = self._root / f"{name}-{len(self._built)}"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--home", str(path), "init", "--name", "Alex", "--email", "alex@example.com"])
                if code != 0:
                    raise RuntimeError(f"oil init failed with exit code {code}")
                if seed is not None:
                    seed(path)
            if any(path.rglob("*-wal")) or any(path.rglob("*-shm")):
                raise RuntimeError("a template folder must not hold a write-ahead log")
            self._built[key] = path
        return self._built[key]


@pytest.fixture(scope="session")
def home_templates(tmp_path_factory: pytest.TempPathFactory) -> HomeTemplates:
    return HomeTemplates(tmp_path_factory.mktemp("home-templates"))
