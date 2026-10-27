from collections.abc import Iterator

import keyring
import keyring.backend
import keyring.errors
import pytest
import sqlalchemy

from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade


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


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory keyring and a per-test data folder for every test."""
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    monkeypatch.setenv("AI_OIL_HOME", str(tmp_path / "home"))
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def engine(tmp_path) -> Iterator[sqlalchemy.Engine]:
    db_engine = create_db_engine(tmp_path / "test.db")
    upgrade(db_engine)
    yield db_engine
    db_engine.dispose()
