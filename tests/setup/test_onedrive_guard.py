from pathlib import Path

import pytest

from ai_oil.cli.main import main
from ai_oil.setup.probe import LocalProbe


def test_init_refuses_a_data_folder_inside_onedrive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "OneDrive - Corp"
    monkeypatch.setattr(LocalProbe, "onedrive_roots", lambda self: [root])
    home = root / "AI_OIL"

    code = main(["--home", str(home), "init", "--name", "Alex", "--email", "alex@example.com"])

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: {home} is inside OneDrive ({root}) — a database under a sync client can be corrupted; "
        "use the default %LOCALAPPDATA%\\AI_OIL or set AI_OIL_HOME elsewhere\n"
    )
    assert not home.exists()


def test_init_outside_onedrive_still_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(LocalProbe, "onedrive_roots", lambda self: [tmp_path / "OneDrive - Corp"])
    home = tmp_path / "OneDrive - Corp2" / "AI_OIL"

    assert main(["--home", str(home), "init", "--name", "Alex", "--email", "alex@example.com"]) == 0
    assert (home / "ai_oil.db").is_file()


def test_commands_warn_but_still_run_when_the_data_folder_is_in_onedrive(
    monkeypatch: pytest.MonkeyPatch, initialised_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = initialised_home.parent
    monkeypatch.setattr(LocalProbe, "onedrive_roots", lambda self: [root])

    assert main(["--home", str(initialised_home), "person", "list"]) == 0

    captured = capsys.readouterr()
    assert "Alex *me*" in captured.out
    assert captured.err == (
        f"warning: the AI_OIL data folder {initialised_home} is inside OneDrive ({root})"
        " — move it out (see docs/install_windows.md)\n"
    )


def test_commands_are_silent_outside_onedrive(
    monkeypatch: pytest.MonkeyPatch,
    initialised_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(LocalProbe, "onedrive_roots", lambda self: [tmp_path / "OneDrive - Corp"])

    assert main(["--home", str(initialised_home), "person", "list"]) == 0

    assert capsys.readouterr().err == ""
