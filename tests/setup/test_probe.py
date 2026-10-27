import ctypes
import ntpath
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_oil.setup import probe as probe_module
from ai_oil.setup.probe import LocalProbe, inside_onedrive

_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
_ACCOUNTS = r"Software\Microsoft\OneDrive\Accounts"


class _Key:
    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeWinreg:
    """The part of `winreg` the probe uses, over a dict {full key path: {value name: data}}."""

    HKEY_CURRENT_USER = object()

    def __init__(self, keys: dict[str, dict[str, str]]) -> None:
        self.keys = keys

    def OpenKey(self, parent, sub: str) -> _Key:  # noqa: N802
        path = sub if parent is self.HKEY_CURRENT_USER else f"{parent.path}\\{sub}"
        if not any(key == path or key.startswith(path + "\\") for key in self.keys):
            raise FileNotFoundError(path)
        return _Key(path)

    def QueryValueEx(self, key: _Key, name: str) -> tuple[str, int]:  # noqa: N802
        try:
            return self.keys[key.path][name], 1
        except KeyError:
            raise FileNotFoundError(name) from None

    def EnumKey(self, key: _Key, index: int) -> str:  # noqa: N802
        prefix = key.path + "\\"
        children = sorted({k[len(prefix) :].split("\\")[0] for k in self.keys if k.startswith(prefix)})
        if index >= len(children):
            raise OSError("no more data")
        return children[index]


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch):
    """Pretend to be Windows with the given registry; returns the installer for a FakeWinreg."""
    monkeypatch.setattr(LocalProbe, "is_windows", lambda self: True)

    def install(keys: dict[str, dict[str, str]]) -> None:
        monkeypatch.setitem(sys.modules, "winreg", FakeWinreg(keys))

    return install


@pytest.fixture
def no_onedrive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(name, raising=False)


def test_inside_onedrive_finds_the_containing_root(tmp_path: Path) -> None:
    root = tmp_path / "OneDrive - Corp"
    other = tmp_path / "OneDrive"
    assert inside_onedrive(root / "Documents" / "x", [other, root]) == root
    assert inside_onedrive(root, [root]) == root


def test_inside_onedrive_requires_a_whole_path_component(tmp_path: Path) -> None:
    assert inside_onedrive(tmp_path / "OneDrive2" / "x", [tmp_path / "OneDrive"]) is None
    assert inside_onedrive(tmp_path / "elsewhere", [tmp_path / "OneDrive"]) is None
    assert inside_onedrive(tmp_path / "x", []) is None


def test_inside_onedrive_resolves_relative_parts_and_links(tmp_path: Path) -> None:
    root = tmp_path / "OneDrive"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    assert inside_onedrive(root / "a" / ".." / "b", [root]) == root
    assert inside_onedrive(root / "link" / "data", [root]) is None
    assert inside_onedrive(outside / ".." / "OneDrive" / "x", [root]) == root


@pytest.mark.skipif(os.name == "nt", reason="documents the case-sensitive comparison on POSIX")
def test_inside_onedrive_is_case_sensitive_where_the_file_system_is(tmp_path: Path) -> None:
    assert inside_onedrive(tmp_path / "onedrive" / "x", [tmp_path / "OneDrive"]) is None


def test_inside_onedrive_ignores_case_with_the_windows_normcase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # os.path.normcase is the one thing that differs on Windows: it lower-cases the path.
    monkeypatch.setattr(os.path, "normcase", lambda path: str(path).lower())
    root = tmp_path / "OneDrive - Corp"
    assert inside_onedrive(tmp_path / "ONEDRIVE - corp" / "Documents", [root]) == root
    assert inside_onedrive(tmp_path / "onedrive - corp2", [root]) is None


def test_env_treats_empty_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_OIL_PROBE_A", "x")
    monkeypatch.setenv("AI_OIL_PROBE_B", "")
    monkeypatch.delenv("AI_OIL_PROBE_C", raising=False)
    local = LocalProbe()
    assert local.env("AI_OIL_PROBE_A") == "x"
    assert local.env("AI_OIL_PROBE_B") is None
    assert local.env("AI_OIL_PROBE_C") is None


def test_is_windows_follows_the_platform() -> None:
    assert LocalProbe().is_windows() == (sys.platform == "win32")


def test_onedrive_roots_from_the_environment_without_duplicates(
    monkeypatch: pytest.MonkeyPatch, no_onedrive_env: None
) -> None:
    monkeypatch.setenv("OneDrive", "/x/OneDrive - Corp")
    monkeypatch.setenv("OneDriveCommercial", "/x/OneDrive - Corp")
    monkeypatch.setenv("OneDriveConsumer", "/x/OneDrive")
    assert LocalProbe().onedrive_roots() == [Path("/x/OneDrive - Corp"), Path("/x/OneDrive")]


def test_onedrive_roots_without_any_onedrive(no_onedrive_env: None) -> None:
    assert LocalProbe().onedrive_roots() == []


def test_onedrive_roots_add_every_registry_account(
    monkeypatch: pytest.MonkeyPatch, no_onedrive_env: None, windows
) -> None:
    monkeypatch.setenv("OneDrive", "C:/Users/a/OneDrive - Corp")
    windows(
        {
            f"{_ACCOUNTS}\\Business1": {"UserFolder": "C:/Users/a/OneDrive - Corp"},
            f"{_ACCOUNTS}\\Personal": {"UserFolder": "C:/Users/a/OneDrive"},
            f"{_ACCOUNTS}\\Business2": {"Other": "x"},
        }
    )
    assert LocalProbe().onedrive_roots() == [Path("C:/Users/a/OneDrive - Corp"), Path("C:/Users/a/OneDrive")]


def test_onedrive_roots_survive_a_missing_registry_key(no_onedrive_env: None, windows) -> None:
    windows({})
    assert LocalProbe().onedrive_roots() == []


def test_documents_folder_from_the_registry_with_variables_expanded(
    monkeypatch: pytest.MonkeyPatch, windows
) -> None:
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\a")
    monkeypatch.setattr(os.path, "expandvars", ntpath.expandvars)
    windows({_SHELL_FOLDERS: {"Personal": "%USERPROFILE%\\OneDrive - Corp\\Documents"}})
    assert LocalProbe().documents_folder() == Path("C:\\Users\\a\\OneDrive - Corp\\Documents")


def test_documents_folder_falls_back_to_the_home_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, windows
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    windows({})
    assert LocalProbe().documents_folder() == tmp_path / "Documents"


def test_documents_folder_is_none_when_nothing_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert LocalProbe().documents_folder() is None


def _fake_whoami(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", code: int = 0, error=None) -> list:
    calls: list = []

    def run(command, **kwargs):
        calls.append(command)
        if error is not None:
            raise error
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    monkeypatch.setattr(probe_module.subprocess, "run", run)
    return calls


def test_user_principal_name_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_whoami(monkeypatch, stdout="  alex@corp.com\r\n")
    assert LocalProbe().user_principal_name() == "alex@corp.com"
    assert calls == [["whoami", "/upn"]]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stdout": "ERROR: The user name could not be found.", "code": 1},
        {"stdout": "no-at-sign\n"},
        {"stdout": ""},
        {"error": FileNotFoundError("whoami")},
        {"error": subprocess.TimeoutExpired("whoami", 10)},
    ],
)
def test_user_principal_name_is_none_on_errors(monkeypatch: pytest.MonkeyPatch, kwargs: dict) -> None:
    _fake_whoami(monkeypatch, **kwargs)
    assert LocalProbe().user_principal_name() is None


def test_display_name_is_none_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LocalProbe, "is_windows", lambda self: False)
    assert LocalProbe().display_name() is None


def test_display_name_reads_name_display_from_secur32(monkeypatch: pytest.MonkeyPatch, windows) -> None:
    seen: list[int] = []

    def get_user_name_ex(name_format, buffer, size) -> int:
        seen.append(name_format)
        buffer.value = " Alexander Hultsch "
        return 1

    windows({})
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(secur32=SimpleNamespace(GetUserNameExW=get_user_name_ex)),
        raising=False,
    )
    assert LocalProbe().display_name() == "Alexander Hultsch"
    assert seen == [3]


def test_display_name_is_none_when_the_call_fails(monkeypatch: pytest.MonkeyPatch, windows) -> None:
    windows({})
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(secur32=SimpleNamespace(GetUserNameExW=lambda *args: 0)),
        raising=False,
    )
    assert LocalProbe().display_name() is None
