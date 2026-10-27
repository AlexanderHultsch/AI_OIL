"""System detection for the setup wizard, behind an interface so tests can supply fixtures."""

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

from ai_oil.errors import ConfigError

_USER_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
_ONEDRIVE_ACCOUNTS = r"Software\Microsoft\OneDrive\Accounts"
_ONEDRIVE_ENV = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
_SYNC_PROVIDERS = r"Software\SyncEngines\Providers\OneDrive"
_NAME_DISPLAY = 3
_OUTLOOK_PROGID = "Outlook.Application"
_OUTLOOK_PREFERENCES = r"Software\Microsoft\Office\16.0\Outlook\Preferences"
_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_USER_ENVIRONMENT = "Environment"
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002
_BROADCAST_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class InternetSettings:
    """How this computer reaches the internet: the Windows proxy settings and the environment."""

    proxy_server: str | None = None
    auto_config_url: str | None = None
    env_proxy: str | None = None


@dataclass(frozen=True)
class SyncedLibrary:
    """A library the OneDrive client syncs: its web address, its local folder and its `LibraryType`."""

    name: str
    url: str
    path: str
    kind: str


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

    def outlook_installed(self) -> bool:
        """Whether a classic Outlook is registered for COM (False off Windows)."""
        ...

    def new_outlook_enabled(self) -> bool:
        """Whether the "New Outlook" toggle is switched on (False off Windows or if unknown)."""
        ...

    def outlook_account_addresses(self) -> list[str]:
        """The SMTP address of every Outlook account, lower-cased; empty if Outlook cannot be asked."""
        ...

    def internet_settings(self) -> InternetSettings: ...

    def sharepoint_libraries(self) -> list[SyncedLibrary]:
        """Every library OneDrive syncs for the user, as the sync client registered it; empty off Windows."""
        ...

    def set_user_env(self, name: str, value: str) -> None:
        """Store an environment variable for the Windows user (and this process); ConfigError elsewhere."""
        ...


def _registry_value(root: str, path: str, name: str) -> object | None:
    import winreg

    try:
        with winreg.OpenKey(getattr(winreg, root), path) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value


def _text(value: object | None) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


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

    def outlook_installed(self) -> bool:
        if not self.is_windows():
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, _OUTLOOK_PROGID):
                return True
        except OSError:
            return False

    def new_outlook_enabled(self) -> bool:
        if not self.is_windows():
            return False
        return _registry_value("HKEY_CURRENT_USER", _OUTLOOK_PREFERENCES, "UseNewOutlook") == 1

    def outlook_account_addresses(self) -> list[str]:
        if not self.is_windows():
            return []
        try:
            from win32com.client import Dispatch

            accounts = Dispatch(_OUTLOOK_PROGID).GetNamespace("MAPI").Accounts
            found: dict[str, None] = {}
            for index in range(1, accounts.Count + 1):
                address = _text(accounts.Item(index).SmtpAddress)
                if address:
                    found.setdefault(address.lower(), None)
            return list(found)
        except Exception:
            return []

    def internet_settings(self) -> InternetSettings:
        proxy_server: str | None = None
        auto_config_url: str | None = None
        if self.is_windows():
            if _registry_value("HKEY_CURRENT_USER", _INTERNET_SETTINGS, "ProxyEnable") == 1:
                proxy_server = _text(_registry_value("HKEY_CURRENT_USER", _INTERNET_SETTINGS, "ProxyServer"))
            auto_config_url = _text(_registry_value("HKEY_CURRENT_USER", _INTERNET_SETTINGS, "AutoConfigURL"))
        return InternetSettings(
            proxy_server=proxy_server,
            auto_config_url=auto_config_url,
            env_proxy=self.env("HTTPS_PROXY") or self.env("https_proxy"),
        )

    def sharepoint_libraries(self) -> list[SyncedLibrary]:
        if not self.is_windows():
            return []
        import winreg

        found: list[SyncedLibrary] = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SYNC_PROVIDERS) as providers:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(providers, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(providers, name) as provider:
                            url, _ = winreg.QueryValueEx(provider, "UrlNamespace")
                            mount, _ = winreg.QueryValueEx(provider, "MountPoint")
                            try:
                                kind, _ = winreg.QueryValueEx(provider, "LibraryType")
                            except OSError:
                                kind = ""
                    except OSError:
                        continue
                    url, mount = _text(url), _text(mount)
                    if url is None or mount is None:
                        continue
                    found.append(
                        SyncedLibrary(
                            name=PureWindowsPath(mount).name or mount,
                            url=url.rstrip("/"),
                            path=mount,
                            kind=kind.strip() if isinstance(kind, str) else "",
                        )
                    )
        except OSError:
            return []
        return found

    def set_user_env(self, name: str, value: str) -> None:
        if not self.is_windows():
            raise ConfigError("only possible on Windows")
        import winreg

        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
        except OSError as exc:
            raise ConfigError(f"cannot store {name} for your Windows user: {exc}") from exc
        os.environ[name] = value
        self._announce_environment_change()

    @staticmethod
    def _announce_environment_change() -> None:
        """Tell running programs (Explorer) to re-read the user environment, as `setx` does."""
        try:
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
                _HWND_BROADCAST,
                _WM_SETTINGCHANGE,
                0,
                _USER_ENVIRONMENT,
                _SMTO_ABORTIFHUNG,
                _BROADCAST_TIMEOUT_MS,
                None,
            )
        except (AttributeError, OSError):
            pass


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
