import io
import sys

import keyring
import keyring.errors
import pytest

from ai_oil.cli.main import main
from ai_oil.errors import ConfigError
from ai_oil.secrets import delete_secret, get_secret, set_secret


def _raise_no_keyring(*_args: object) -> None:
    raise keyring.errors.NoKeyringError("x")


def test_round_trip() -> None:
    set_secret("jira", "token-123")
    assert get_secret("jira") == "token-123"
    delete_secret("jira")
    assert get_secret("jira") is None


def test_delete_absent_does_not_raise() -> None:
    delete_secret("never-set")


def test_get_absent_is_none() -> None:
    assert get_secret("never-set") is None


def test_set_without_store_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "set_password", _raise_no_keyring)
    with pytest.raises(ConfigError) as exc_info:
        set_secret("jira", "s3cr3t")
    message = str(exc_info.value)
    assert "NoKeyringError" in message
    assert "GOV-8" in message
    assert "s3cr3t" not in message


def test_get_without_store_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_password", _raise_no_keyring)
    with pytest.raises(ConfigError) as exc_info:
        get_secret("jira")
    assert "NoKeyringError" in str(exc_info.value)
    assert "GOV-8" in str(exc_info.value)


def test_delete_without_store_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "delete_password", _raise_no_keyring)
    with pytest.raises(ConfigError) as exc_info:
        delete_secret("jira")
    assert "NoKeyringError" in str(exc_info.value)
    assert "GOV-8" in str(exc_info.value)


def test_connector_add_without_store_fails_cleanly_and_leaves_no_connector(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = str(tmp_path / "h")
    assert main(["--home", home, "init", "--name", "A", "--email", "a@example.com"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(keyring, "set_password", _raise_no_keyring)
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    assert (
        main(
            [
                "--home",
                home,
                "connector",
                "add",
                "jira",
                "J",
                "--setting",
                "base_url=https://acme.atlassian.net",
                "--setting",
                "email=a@example.com",
                "--token-stdin",
            ]
        )
        == 1
    )
    assert capsys.readouterr().err.startswith("error: no secure credential store is available")
    assert main(["--home", home, "connector", "list"]) == 0
    assert "  J  " not in capsys.readouterr().out
