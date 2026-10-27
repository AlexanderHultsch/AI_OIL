"""Configuration and data folder. Paths and settings only, never secrets (GOV-8)."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ai_oil.errors import ConfigError

CONFIG_TEMPLATE = """\
# AI_OIL configuration. Paths and settings only — never secrets (GOV-8).

[storage]
max_blob_mb = 25
max_text_chars = 2000000

[backup]
# Folder for daily backups, e.g. a synced OneDrive library. Empty = no automatic backup.
folder = ""
keep = 14

[sync]
done_grace_days = 14
"""


def default_home() -> Path:
    """Data folder: `AI_OIL_HOME`, else `%LOCALAPPDATA%\\AI_OIL`, else `~/.local/share/ai_oil`."""
    explicit = os.environ.get("AI_OIL_HOME")
    if explicit:
        return Path(explicit)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "AI_OIL"
    return Path.home() / ".local" / "share" / "ai_oil"


@dataclass(frozen=True)
class StorageConfig:
    """Limits for stored attachments and extracted text."""

    max_blob_mb: int = 25
    max_text_chars: int = 2_000_000


@dataclass(frozen=True)
class BackupConfig:
    """Automatic backup target and retention."""

    folder: Path | None = None
    keep: int = 14


@dataclass(frozen=True)
class SyncConfig:
    """Sync behaviour."""

    done_grace_days: int = 14


@dataclass(frozen=True)
class Config:
    """Resolved configuration and the paths derived from the data folder."""

    home: Path
    storage: StorageConfig
    backup: BackupConfig
    sync: SyncConfig

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.home / "ai_oil.db"

    @property
    def blob_dir(self) -> Path:
        return self.home / "blobs"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"


def _section(data: dict, name: str) -> dict:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"config key [{name}] must be a table")
    return section


def _int_value(section: dict, section_name: str, key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config key {section_name}.{key} must be an integer")
    return value


def load_config(home: Path | None = None) -> Config:
    """Load `config.toml` from `home` (default: `default_home()`); defaults fill missing keys."""
    home = home if home is not None else default_home()
    config_path = home / "config.toml"
    if not config_path.is_file():
        raise ConfigError(f"AI_OIL is not initialised at {home} — run `oil init`")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {config_path}: {exc}") from exc

    storage = _section(data, "storage")
    backup = _section(data, "backup")
    sync = _section(data, "sync")

    folder_value = backup.get("folder", "")
    if not isinstance(folder_value, str):
        raise ConfigError("config key backup.folder must be a string")

    return Config(
        home=home,
        storage=StorageConfig(
            max_blob_mb=_int_value(storage, "storage", "max_blob_mb", StorageConfig.max_blob_mb),
            max_text_chars=_int_value(storage, "storage", "max_text_chars", StorageConfig.max_text_chars),
        ),
        backup=BackupConfig(
            folder=Path(folder_value) if folder_value else None,
            keep=_int_value(backup, "backup", "keep", BackupConfig.keep),
        ),
        sync=SyncConfig(
            done_grace_days=_int_value(sync, "sync", "done_grace_days", SyncConfig.done_grace_days),
        ),
    )


def write_default_config(home: Path) -> Path:
    """Create the data folder with `blobs/` and `logs/`; write the template only if no config exists."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "blobs").mkdir(exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)
    config_path = home / "config.toml"
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return config_path
