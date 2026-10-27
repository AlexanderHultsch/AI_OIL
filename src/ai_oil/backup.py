"""Backups: consistent SQLite copies plus the blob store delta (NFR-8)."""

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy

from ai_oil import timeutil
from ai_oil.config import Config
from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade
from ai_oil.errors import ConfigError, ValidationError
from ai_oil.governance import audit

BACKUP_PATTERN = "ai_oil-*.db"
_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_AUTO_BACKUP_INTERVAL = timedelta(hours=24)


def _stamp() -> str:
    return timeutil._now().astimezone(UTC).strftime(_STAMP_FORMAT)


def _copy_missing_blobs(source: Path, target: Path) -> int:
    """Copy every file under `source` that is not yet present under `target`; returns how many."""
    if not source.is_dir():
        return 0
    copied = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        destination = target / path.relative_to(source)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tmp")
        shutil.copyfile(path, temp)
        temp.replace(destination)
        copied += 1
    return copied


def _prune(folder: Path, keep: int) -> None:
    backups = sorted(folder.glob(BACKUP_PATTERN), key=lambda path: path.name)
    for old in backups[: max(len(backups) - max(keep, 1), 0)]:
        old.unlink()


def create_backup(engine: sqlalchemy.Engine, config: Config, *, folder: Path | None = None) -> Path:
    """Write a consistent copy of the database and the new blobs to the backup folder (NFR-8)."""
    folder = folder if folder is not None else config.backup.folder
    if folder is None:
        raise ConfigError("no backup folder configured — set [backup] folder in config.toml or pass --to")
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"ai_oil-{_stamp()}.db"
    temp = folder / f".{final.name}.tmp"
    temp.unlink(missing_ok=True)

    raw_source = engine.raw_connection()
    try:
        raw_target = sqlite3.connect(temp)
        try:
            raw_source.driver_connection.backup(raw_target)  # type: ignore[union-attr]
            raw_target.execute("PRAGMA journal_mode=DELETE").close()
        finally:
            raw_target.close()
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        raw_source.close()
    temp.replace(final)

    blobs_copied = _copy_missing_blobs(config.blob_dir, folder / "blobs")
    _prune(folder, config.backup.keep)
    with engine.begin() as conn:
        audit.log(
            conn,
            "backup.create",
            "B",
            actor_type="user",
            target=str(final),
            detail={"blobs_copied": blobs_copied},
        )
    return final


def latest_backup(folder: Path) -> Path | None:
    """The newest backup file in `folder` by name, or None."""
    if not folder.is_dir():
        return None
    backups = sorted(folder.glob(BACKUP_PATTERN), key=lambda path: path.name)
    return backups[-1] if backups else None


def maybe_auto_backup(engine: sqlalchemy.Engine, config: Config) -> Path | None:
    """Back up if a folder is configured and the newest backup is older than 24 hours (NFR-8)."""
    folder = config.backup.folder
    if folder is None:
        return None
    newest = latest_backup(folder)
    if newest is not None:
        try:
            taken = datetime.strptime(newest.stem.removeprefix("ai_oil-"), _STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            taken = None
        if taken is not None and timeutil._now() - taken < _AUTO_BACKUP_INTERVAL:
            return None
    return create_backup(engine, config)


def _check_backup_file(backup_file: Path) -> None:
    if not backup_file.is_file():
        raise ValidationError(f"backup file {backup_file} does not exist")
    try:
        probe = sqlite3.connect(f"{backup_file.resolve().as_uri()}?mode=ro", uri=True)
        try:
            probe.execute("SELECT 1 FROM schema_version LIMIT 1").close()
        finally:
            probe.close()
    except sqlite3.Error as exc:
        raise ValidationError(f"{backup_file} is not an AI_OIL database backup: {exc}") from exc


def restore_backup(config: Config, backup_file: Path) -> Path:
    """Replace the live database with a backup; the old one is kept as a safety copy (NFR-8)."""
    _check_backup_file(backup_file)
    db_path = config.db_path
    safety = db_path
    if db_path.exists():
        safety = db_path.with_name(f"{db_path.name}.before-restore-{_stamp()}")
        for suffix in ("", "-wal", "-shm"):
            sibling = db_path.with_name(db_path.name + suffix)
            if sibling.exists():
                sibling.replace(safety.with_name(safety.name + suffix))
    shutil.copyfile(backup_file, db_path)
    _copy_missing_blobs(backup_file.parent / "blobs", config.blob_dir)

    engine = create_db_engine(db_path)
    try:
        upgrade(engine)
        with engine.begin() as conn:
            audit.log(
                conn,
                "backup.restore",
                "B",
                actor_type="user",
                target=str(backup_file),
                detail={"safety_copy": str(safety) if safety != db_path else None},
            )
    finally:
        engine.dispose()
    return safety
