"""System detection for the setup wizard, behind an interface so tests can supply fixtures."""

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

_USER_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
_ONEDRIVE_ACCOUNTS = r"Software\Microsoft\OneDrive\Accounts"
_ONEDRIVE_ENV = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
_NAME_DISPLAY = 3


class SystemProbe(Protocol):
    """What the wizard may ask the machine; everything detected is a proposal, never a decision."""

    def env(self, name: str) -> str | None: ...

    def documents_folder(self) -> Path | None:
        """The (possibly redirected) Documents folder, or None if it cannot be determined."""
        ...

    def onedrive_roots(self) -> list[Path]:
        """Every OneDrive sync root of the user, existing or not, without duplicates."""
        ...

    def user_principal_name(self) -> str | None: ...

    def display_name(self) -> str | None: ...

    def is_windows(self) -> bool: ...


class LocalProbe:
    """The real machine. Windows-only APIs are imported inside the methods that need them."""

    def env(self, name: str) -> str | None:
        return os.environ.get(name) or None

    def is_windows(self) -> bool:
        return sys.platform == "win32"

    def documents_folder(self) -> Path | None:
        if self.is_windows():
            import winreg

            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_SHELL_FOLDERS) as key:
                    value, _ = winreg.QueryValueEx(key, "Personal")
            except OSError:
                pass
            else:
                if isinstance(value, str) and value.strip():
                    return Path(os.path.expandvars(value))
        fallback = Path.home() / "Documents"
        return fallback if fallback.is_dir() else None

    def onedrive_roots(self) -> list[Path]:
        values = [self.env(name) for name in _ONEDRIVE_ENV]
        if self.is_windows():
            values += self._registry_user_folders()
        roots: dict[str, Path] = {}
        for value in values:
            if value and value.strip():
                roots.setdefault(os.path.normcase(value.strip()), Path(value.strip()))
        return list(roots.values())

    def _registry_user_folders(self) -> list[str]:
        import winreg

        found: list[str] = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ONEDRIVE_ACCOUNTS) as accounts:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(accounts, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(accounts, name) as account:
                            value, _ = winreg.QueryValueEx(account, "UserFolder")
                    except OSError:
                        continue
                    if isinstance(value, str):
                        found.append(value)
        except OSError:
            pass
        return found

    def user_principal_name(self) -> str | None:
        try:
            result = subprocess.run(
                ["whoami", "/upn"], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        upn = result.stdout.strip()
        if result.returncode != 0 or "@" not in upn:
            return None
        return upn

    def display_name(self) -> str | None:
        if not self.is_windows():
            return None
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            ok = ctypes.windll.secur32.GetUserNameExW(_NAME_DISPLAY, buffer, ctypes.byref(size))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return None
        return buffer.value.strip() or None if ok else None


def _normalised(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def inside_onedrive(path: Path, roots: Sequence[Path]) -> Path | None:
    """The OneDrive root that contains `path` (resolved; case-insensitive on Windows), else None."""
    target = _normalised(path)
    for root in roots:
        prefix = _normalised(root).rstrip(os.sep)
        if target == prefix or target.startswith(prefix + os.sep):
            return root
    return None
