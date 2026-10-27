from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from ai_oil.cli import setup as setup_command
from ai_oil.cli.main import main
from ai_oil.config import CONFIG_TEMPLATE, load_config
from ai_oil.setup import wizard


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch, make_console, probe) -> Callable[[Sequence[str]], object]:
    """Feed `oil setup` a scripted console and the fake probe; returns the console."""

    def install(answers: Sequence[str]):
        console = make_console(answers)
        monkeypatch.setattr(setup_command, "Console", lambda: console)
        monkeypatch.setattr(setup_command, "LocalProbe", lambda: probe)
        return console

    return install


def test_list_prints_name_and_title_per_stage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["setup", "--list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "data  Data folder",
        "me  About you",
        "backup  Backups",
        "outlook  Outlook",
        "network  Network",
        "atlassian  Jira and Confluence",
        "sharepoint  SharePoint and OneDrive files",
    ]


def test_only_backup_runs_through_main(
    scripted, probe, initialised_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "backups"
    console = scripted([str(target)])

    assert main(["--home", str(initialised_home), "setup", "--only", "backup"]) == 0

    assert "1/1  Backups" in console.output
    assert f"  Backup folder: {target.as_posix()}" in console.output
    assert load_config(initialised_home).backup.folder == target
    assert capsys.readouterr().err == ""


def test_only_with_an_unknown_stage_is_an_error(
    scripted, initialised_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted([])
    assert main(["--home", str(initialised_home), "setup", "--only", "nope"]) == 1
    expected = (
        "error: unknown stage 'nope'; stages: data, me, backup, outlook, network, atlassian, sharepoint"
    )
    assert expected in capsys.readouterr().err


def test_ctrl_c_aborts_cleanly_and_leaves_the_data_alone(
    scripted, initialised_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted([])  # end of input

    assert main(["--home", str(initialised_home), "setup", "--only", "backup"]) == 1

    assert "Setup aborted — nothing half-written; run `oil setup` again any time." in capsys.readouterr().out
    assert (initialised_home / "config.toml").read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_a_failing_stage_gives_exit_code_1(scripted, fresh_home: Path) -> None:
    console = scripted([])
    assert main(["--home", str(fresh_home), "setup", "--only", "me"]) == 1
    assert "Failed: AI_OIL is not initialised" in console.output


def test_the_full_wizard_on_a_fresh_folder(
    scripted, probe, fresh_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wizard, "STAGES", wizard.STAGES[:3])  # the stages that reach out are tested apart
    probe.name = "Alex"
    probe.upn = "alex@corp.com"
    target = tmp_path / "backups"
    console = scripted(["", "", str(target)])

    assert main(["--home", str(fresh_home), "setup"]) == 0

    assert "Nothing is open." in console.output
    assert load_config(fresh_home).backup.folder == target
