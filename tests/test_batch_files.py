"""Static checks of the Windows batch files (docs/installer_concept.md, section 4).

Batch cannot be run here. These tests only guard structure, encoding, line endings, quoting and the
deletion rules; the real test is the first run on the laptop (OP-8, OP-54).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATCH_FILES = ["install.bat", "start.bat", "update.bat", "uninstall.bat", "installer/pip_install.cmd"]
DATA_RMDIR = re.compile(r'rmdir\s+/s\s+/q\s+"%LOCALAPPDATA%\\AI_OIL"', re.IGNORECASE)
VENV_RMDIR = re.compile(r'rmdir\s+/s\s+/q\s+"%LOCALAPPDATA%\\AI_OIL\\venv"', re.IGNORECASE)


def _lines(name: str) -> list[str]:
    return (ROOT / name).read_bytes().decode("ascii").split("\r\n")


def _is_message(line: str) -> bool:
    """Echo and comment lines only talk about paths; they run nothing."""
    return line.strip().lower().startswith(("rem ", "::")) or re.search(r"\becho\b", line, re.I) is not None


def _command_lines(name: str) -> list[str]:
    return [line for line in _lines(name) if line.strip() and not _is_message(line)]


@pytest.mark.parametrize("name", BATCH_FILES)
def test_the_file_exists(name: str) -> None:
    assert (ROOT / name).is_file()


@pytest.mark.parametrize("name", BATCH_FILES)
def test_the_file_is_pure_ascii(name: str) -> None:
    assert (ROOT / name).read_bytes().isascii()


@pytest.mark.parametrize("name", BATCH_FILES)
def test_every_line_ends_with_crlf(name: str) -> None:
    data = (ROOT / name).read_bytes()
    assert data.endswith(b"\r\n")
    assert b"\r" not in data.replace(b"\r\n", b"")
    assert b"\n" not in data.replace(b"\r\n", b"")


@pytest.mark.parametrize("name", BATCH_FILES)
def test_the_file_opens_with_the_standard_preamble(name: str) -> None:
    lines = [line for line in _lines(name) if line.strip()]
    assert lines[0] == "@echo off"
    assert lines[1].lower().startswith("setlocal")
    assert "chcp 65001" in lines[2].lower()
    assert lines[3] == 'cd /d "%~dp0"'


@pytest.mark.parametrize("name", BATCH_FILES)
def test_no_powershell_or_environment_activation(name: str) -> None:
    text = "\n".join(_lines(name)).lower()
    for forbidden in ("activate", "powershell"):
        assert forbidden not in text


@pytest.mark.parametrize("name", BATCH_FILES)
def test_exit_always_uses_the_batch_form(name: str) -> None:
    for line in _command_lines(name):
        assert not re.search(r"\bexit\b(?!\s+/b)", line, re.I), line


@pytest.mark.parametrize("name", BATCH_FILES)
def test_every_goto_and_call_target_has_a_label(name: str) -> None:
    lines = [line for line in _lines(name) if line.strip() and not _is_message(line)]
    text = "\n".join(lines)
    labels = {m.group(1).lower() for m in re.finditer(r"^\s*:([A-Za-z_]\w*)\s*$", text, re.M)}
    targets = {m.group(1).lower() for m in re.finditer(r"\bgoto\s+:?([A-Za-z_]\w*)", text, re.I)}
    targets |= {m.group(1).lower() for m in re.finditer(r"\bcall\s+:([A-Za-z_]\w*)", text, re.I)}
    targets.discard("eof")
    assert targets <= labels, f"missing labels: {sorted(targets - labels)}"


@pytest.mark.parametrize("name", BATCH_FILES)
def test_paths_with_venv_or_localappdata_are_quoted_on_command_lines(name: str) -> None:
    """On every line that runs something (no echo, no rem), each %VENV% and %LOCALAPPDATA% must
    sit inside double quotes: an odd number of quotes lies before the occurrence."""
    for line in _command_lines(name):
        for match in re.finditer(r"%VENV%|%LOCALAPPDATA%", line, re.I):
            assert line[: match.start()].count('"') % 2 == 1, f"unquoted path in: {line.strip()}"


def test_install_uses_python_312_and_points_to_the_download_page() -> None:
    text = "\n".join(_lines("install.bat"))
    assert "py -3.12" in text
    assert "https://www.python.org/downloads/windows/" in text


def test_install_and_update_share_the_pip_helper() -> None:
    for name in ("install.bat", "update.bat"):
        assert 'call "%~dp0installer\\pip_install.cmd"' in "\n".join(_lines(name))


def test_update_checks_for_local_changes_and_only_fast_forwards() -> None:
    text = "\n".join(_lines("update.bat"))
    assert "status --porcelain" in text
    assert "pull --ff-only" in text


def test_the_helper_has_the_truststore_and_offline_fallbacks() -> None:
    text = "\n".join(_lines("installer/pip_install.cmd"))
    assert "--use-feature=truststore" in text
    assert "--no-index --find-links" in text


def test_uninstall_deletes_the_data_folder_only_on_the_typed_word() -> None:
    """The one rmdir of the data folder (not \\venv) is a single-line `if "%ANSWER%"=="DELETE" ...`
    that comes after the prompt which reads ANSWER. The venv rmdir is the only other one."""
    lines = _command_lines("uninstall.bat")
    prompt_at = next(i for i, line in enumerate(_lines("uninstall.bat")) if "set /p" in line)
    data = [line for line in lines if DATA_RMDIR.search(line)]
    assert len(data) == 1
    assert re.match(r'\s*if\s+"%ANSWER%"=="DELETE"\s', data[0])
    assert _lines("uninstall.bat").index(data[0]) > prompt_at
    others = [line for line in lines if re.search(r"\brmdir\b", line, re.I) and line not in data]
    assert others
    assert all(VENV_RMDIR.search(line) for line in others)


@pytest.mark.parametrize("name", BATCH_FILES)
def test_nothing_deletes_the_program_folder(name: str) -> None:
    for line in _command_lines(name):
        assert not re.search(r"\bdel\s+/s\b", line, re.I), line
        assert not (re.search(r"\b(rmdir|rd|del)\b", line, re.I) and "%~dp0" in line), line


def test_gitignore_lists_the_offline_wheels_folder() -> None:
    assert "wheels/" in (ROOT / ".gitignore").read_text(encoding="ascii").split()


def test_gitattributes_forces_crlf_for_batch_files() -> None:
    rules = (ROOT / ".gitattributes").read_text(encoding="ascii").split()
    assert "*.bat" in rules
    assert "*.cmd" in rules
    assert rules.count("text") == 2
    assert rules.count("eol=crlf") == 2
