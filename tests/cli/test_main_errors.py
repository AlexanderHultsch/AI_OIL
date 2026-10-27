import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_oil.cli import main as cli_main
from ai_oil.errors import ValidationError


def _boom(args: argparse.Namespace) -> int:
    raise ValidationError("bad input")


def _register(subparsers) -> None:
    subparsers.add_parser("boom").set_defaults(func=_boom)


def test_domain_error_prints_message_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_main, "COMMAND_MODULES", [SimpleNamespace(register=_register)])
    assert cli_main.main(["boom"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: bad input\n"
    assert captured.out == ""


def test_home_option_is_accepted(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main(["--home", str(tmp_path), "--version"])
    assert excinfo.value.code == 0
