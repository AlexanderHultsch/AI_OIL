import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
import sqlalchemy

from ai_oil import backup, timeutil
from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.governance import audit
from ai_oil.sources import registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    IdentityRef,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)

_T0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)


class ScriptedConnector:
    """Recognises SCR-<n> and returns one version and three scripted comments on every fetch."""

    type: ClassVar[str] = "scripted"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 2

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("reachable", True, "ok"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        if not text.startswith("SCR-"):
            return None
        return SourceRef("jira_issue", text[4:], display_ref=text, locator=f"https://scripted/{text}")

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(
            "jira_issue",
            ref.external_id,
            title=f"Scripted {ref.external_id}",
            display_ref=ref.display_ref,
            locator=ref.locator,
        )

    def fetch(self, state: ObjectState) -> FetchResult:
        return FetchResult(
            version=VersionData("Summary line", source_version_ref="r1"),
            entries=(
                EntryData(
                    "jira_comment",
                    "c1",
                    "2026-10-01T10:00:00.000Z",
                    "> quoted tail",
                    new_text="Fresh text\nsecond line",
                    author=IdentityRef("email", "bob@example.com", "Bob"),
                    direction="in",
                ),
                EntryData("jira_comment", "c2", "2026-10-02T10:00:00.000Z", "Plain remark"),
                EntryData("jira_comment", "c3", "2026-10-03T10:00:00.000Z", "x" * 150),
            ),
            cursor="u1",
        )

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


@pytest.fixture
def scripted_registered():
    registry.register_connector_type(ScriptedConnector)
    yield
    registry.unregister_connector_type("scripted")


@pytest.fixture
def add_scripted(home, scripted_registered):
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.begin() as conn:
            return registry.create_connector(conn, "scripted", "Scripted").id
    finally:
        engine.dispose()


def seed_issue(path: Path) -> None:
    assert main(["--home", str(path), "project", "add", "OIL", "Open issues"]) == 0
    assert (
        main(
            [
                "--home",
                str(path),
                "issue",
                "create",
                "-p",
                "OIL",
                "-s",
                "Fit the filter",
                "-d",
                "Needs a part",
            ]
        )
        == 0
    )


@pytest.fixture
def home_seed(request: pytest.FixtureRequest):
    return ("source-backup-issue", seed_issue) if "issue" in request.fixturenames else None


@pytest.fixture
def issue(home) -> str:
    return "OIL-1"


def run(oil, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = oil(*argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def rows(db, sql: str, **params: object):
    with db() as conn:
        return conn.execute(sqlalchemy.text(sql), params).all()


def freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: moment)


def configure_backup(home: Path, folder: Path, keep: int = 14) -> None:
    config_path = home / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('folder = ""', f'folder = "{folder.as_posix()}"').replace(
        "keep = 14", f"keep = {keep}"
    )
    config_path.write_text(text, encoding="utf-8")


def put_blob(home: Path, relative: str, payload: bytes = b"blob") -> Path:
    path = home / "blobs" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# --- oil source show -------------------------------------------------------------------------------------


@pytest.fixture
def note(oil, capsys, issue) -> int:
    assert oil("link", issue, "--title", "Call", "--text", "line one\nline two") == 0
    assert oil("note", "edit", "1", "--text", "line one\nline 2 changed\nline three") == 0
    capsys.readouterr()
    return 1


def test_source_show_header_and_latest_version(oil, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "source", "show", str(note))

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "#1 note Call"
    assert lines[1] == "Title: Call"
    assert lines[2] == "Connector: Manual (manual)"
    assert re.fullmatch(
        r"Status: ok, checked 20\d\d-\d\d-\d\dT[\d:.]+Z, changed 20\d\d-\d\d-\d\dT[\d:.]+Z", lines[3]
    )
    assert lines[4:7] == ["Linked to: OIL-1", "Versions: 2 | Entries: 0 | Children: 0", ""]
    assert re.fullmatch(r"Latest version \(v2, fetched 20\d\d-\d\d-\d\dT[\d:.]+Z\):", lines[7])
    assert lines[8:] == ["  line one", "  line 2 changed", "  line three"]
    assert "Versions:\n" not in out.replace("Versions: 2", "")
    assert "Locator" not in out and "Error" not in out and "Part of" not in out


def test_source_show_cuts_long_text_after_forty_lines(oil, capsys, issue) -> None:
    body = "\n".join(f"row {n}" for n in range(1, 46))
    assert oil("link", issue, "--title", "Long", "--text", body) == 0
    capsys.readouterr()

    _, out, _ = run(oil, capsys, "source", "show", "1")

    shown = out.splitlines()
    assert shown[-1] == "  … (5 more lines)"
    assert shown[-2] == "  row 40"
    assert "row 41" not in out


def test_source_show_versions(oil, db, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "source", "show", str(note), "--versions")

    assert code == 0
    with db() as conn:
        v1, v2 = store.list_versions(conn, note)
    section = out.split("\nVersions:\n")[1].splitlines()
    assert section[0] == f"  v1  {v1.fetched_at}  -  {v1.content_hash[:12]}"
    assert section[1] == f"  v2  {v2.fetched_at}  -  {v2.content_hash[:12]}"
    assert len(section) == 2


def test_source_show_diff(oil, capsys, note) -> None:
    code, out, _ = run(oil, capsys, "source", "show", str(note), "--diff", "1", "2")

    assert code == 0
    assert out.splitlines()[-8:] == [
        "Diff v1 → v2:",
        "--- v1",
        "+++ v2",
        "@@ -1,2 +1,3 @@",
        " line one",
        "-line two",
        "+line 2 changed",
        "+line three",
    ]


def test_source_show_diff_unknown_version(oil, capsys, note) -> None:
    code, out, err = run(oil, capsys, "source", "show", str(note), "--diff", "1", "9")

    assert code == 1
    assert "no version 9" in err
    assert out == ""


def test_source_show_unknown_object(oil, capsys, issue) -> None:
    code, out, err = run(oil, capsys, "source", "show", "42")

    assert code == 1
    assert "42" in err
    assert out == ""


def test_source_show_entries_of_a_connector_object(oil, capsys, issue, add_scripted) -> None:
    assert oil("link", issue, "SCR-7") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "source", "show", "1", "--entries", "--versions")

    assert code == 0
    lines = out.splitlines()
    assert lines[:3] == ["#1 jira_issue SCR-7", "Title: Scripted 7", "Connector: Scripted (scripted)"]
    assert lines[3] == "Locator: https://scripted/SCR-7"
    assert "Versions: 1 | Entries: 3 | Children: 0" in lines
    assert re.fullmatch(
        r"  v1  20\d\d-\d\d-\d\dT[\d:.]+Z  r1  [0-9a-f]{12}", lines[lines.index("Versions:") + 1]
    )
    entries = lines[lines.index("Entries:") + 1 :]
    assert entries == [
        "  2026-10-01T10:00:00.000Z  jira_comment  in  Bob  Fresh text",
        "  2026-10-02T10:00:00.000Z  jira_comment  -  -  Plain remark",
        f"  2026-10-03T10:00:00.000Z  jira_comment  -  -  {'x' * 100}",
    ]


def test_source_show_without_a_version_has_no_latest_block(oil, db, capsys, issue) -> None:
    assert oil("link", issue, "https://example.com/a") == 0
    with db() as conn:
        conn.execute(sqlalchemy.text("DELETE FROM source_version"))
        conn.commit()
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "source", "show", "1")

    assert code == 0
    assert "Latest version" not in out
    assert "Versions: 0 | Entries: 0 | Children: 0" in out


# --- oil backup ------------------------------------------------------------------------------------------


def backups_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.glob(backup.BACKUP_PATTERN))


def test_backup_writes_a_valid_copy_with_the_blobs(
    oil, db, home, capsys, monkeypatch, tmp_path, issue
) -> None:
    folder = tmp_path / "bk"
    configure_backup(home, folder)
    blob = put_blob(home, "ab/abcdef.gz", b"payload")
    freeze(monkeypatch, _T0)

    code, out, _ = run(oil, capsys, "backup")

    target = folder / "ai_oil-20261026-091500.db"
    assert (code, out) == (0, f"Backup written: {target}\n")
    assert backups_in(folder) == [target.name]
    assert not list(folder.glob("*.tmp")) and not list(folder.glob(".*"))
    assert not list(folder.glob("*-wal")) and not list(folder.glob("*-shm"))
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("SELECT count(*) FROM issue").fetchone() == (1,)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        copy.close()
    assert (folder / "blobs" / "ab" / "abcdef.gz").read_bytes() == blob.read_bytes()
    with db() as conn:
        [entry] = audit.list_entries(conn, action_prefix="backup.create")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", str(target))
    assert entry.detail == {"blobs_copied": 1}


def test_backup_copies_only_new_blobs(oil, db, home, capsys, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "bk"
    configure_backup(home, folder)
    put_blob(home, "ab/one.gz")
    freeze(monkeypatch, _T0)
    assert oil("backup") == 0
    put_blob(home, "cd/two.gz")
    freeze(monkeypatch, _T0 + timedelta(minutes=1))
    assert oil("backup") == 0

    with db() as conn:
        first, second = reversed(audit.list_entries(conn, action_prefix="backup.create"))
    assert (first.detail["blobs_copied"], second.detail["blobs_copied"]) == (1, 1)
    assert sorted(p.name for p in (folder / "blobs").rglob("*.gz")) == ["one.gz", "two.gz"]


def test_backup_keeps_the_newest_copies_and_every_blob(oil, home, capsys, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "bk"
    configure_backup(home, folder, keep=2)
    put_blob(home, "ab/one.gz")
    for day in (24, 25, 26):
        freeze(monkeypatch, _T0.replace(day=day))
        assert oil("backup") == 0

    assert backups_in(folder) == ["ai_oil-20261025-091500.db", "ai_oil-20261026-091500.db"]
    assert (folder / "blobs" / "ab" / "one.gz").exists()


def test_backup_to_overrides_the_configured_folder(oil, home, capsys, monkeypatch, tmp_path) -> None:
    configured, other = tmp_path / "configured", tmp_path / "other"
    configure_backup(home, configured)
    freeze(monkeypatch, _T0)

    code, out, _ = run(oil, capsys, "backup", "--to", str(other))

    assert (code, out) == (0, f"Backup written: {other / 'ai_oil-20261026-091500.db'}\n")
    assert not configured.exists()


def test_backup_without_a_folder_is_an_error(oil, capsys) -> None:
    code, out, err = run(oil, capsys, "backup")

    assert code == 1
    assert "no backup folder configured" in err
    assert out == ""


# --- automatic backup ------------------------------------------------------------------------------------


@pytest.fixture
def live(home):
    config = load_config(home)
    engine = create_db_engine(config.db_path)
    yield config, engine
    engine.dispose()


def test_auto_backup_does_nothing_without_a_folder(live) -> None:
    config, engine = live

    assert backup.maybe_auto_backup(engine, config) is None


def test_auto_backup_waits_for_24_hours(live, home, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "bk"
    configure_backup(home, folder)
    config = load_config(home)
    _, engine = live
    freeze(monkeypatch, _T0)

    first = backup.maybe_auto_backup(engine, config)
    assert first == folder / "ai_oil-20261026-091500.db"
    freeze(monkeypatch, _T0 + timedelta(hours=23, minutes=59))
    assert backup.maybe_auto_backup(engine, config) is None
    freeze(monkeypatch, _T0 + timedelta(hours=24, minutes=1))
    second = backup.maybe_auto_backup(engine, config)

    assert second == folder / "ai_oil-20261027-091600.db"
    assert backup.latest_backup(folder) == second


def test_latest_backup_of_a_missing_or_empty_folder(tmp_path) -> None:
    assert backup.latest_backup(tmp_path / "nope") is None
    (tmp_path / "empty").mkdir()
    assert backup.latest_backup(tmp_path / "empty") is None


@pytest.fixture
def linked(oil, capsys, issue) -> str:
    assert oil("link", issue, "https://example.com/a") == 0
    capsys.readouterr()
    return issue


def test_sync_writes_a_backup_when_due_and_not_when_recent(
    oil, home, capsys, monkeypatch, tmp_path, linked
) -> None:
    folder = tmp_path / "bk"
    configure_backup(home, folder)
    freeze(monkeypatch, _T0)

    code, out, _ = run(oil, capsys, "sync", "--all")
    assert code == 0
    assert out.splitlines()[-1] == f"Backup written: {folder / 'ai_oil-20261026-091500.db'}"

    freeze(monkeypatch, _T0 + timedelta(hours=1))
    code, out, _ = run(oil, capsys, "sync", "--all")
    assert code == 0
    assert "Backup written" not in out

    freeze(monkeypatch, _T0 + timedelta(hours=25))
    code, out, _ = run(oil, capsys, "sync", "--all")
    assert code == 0
    assert out.splitlines()[-1] == f"Backup written: {folder / 'ai_oil-20261027-101500.db'}"


def test_sync_without_a_backup_folder_writes_no_backup(oil, capsys, linked) -> None:
    code, out, err = run(oil, capsys, "sync", "--all")

    assert code == 0
    assert "Backup" not in out and err == ""


def test_failing_automatic_backup_only_warns(oil, home, capsys, monkeypatch, tmp_path, linked) -> None:
    configure_backup(home, tmp_path / "bk")

    def explode(*args: object, **kwargs: object) -> Path:
        raise RuntimeError("disk full")

    monkeypatch.setattr(backup, "create_backup", explode)

    code, out, err = run(oil, capsys, "sync", "--all")

    assert code == 0
    assert out.startswith("Sync all: ok")
    assert "Backup written" not in out
    assert err == "warning: automatic backup failed: disk full\n"


# --- oil restore -----------------------------------------------------------------------------------------


def issue_count(db) -> int:
    return len(rows(db, "SELECT id FROM issue"))


def test_restore_without_yes_changes_nothing(oil, db, home, capsys, tmp_path, issue) -> None:
    folder = tmp_path / "bk"
    assert oil("backup", "--to", str(folder)) == 0
    [file] = folder.glob(backup.BACKUP_PATTERN)
    assert oil("issue", "create", "-p", "OIL", "-s", "Second", "-d", "D") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "restore", str(file))

    assert code == 1
    assert out == (
        f"This replaces {home / 'ai_oil.db'} with {file}. The current database is kept as a safety copy."
        " Re-run with --yes to proceed.\n"
    )
    assert issue_count(db) == 2
    assert list(home.glob("ai_oil.db.before-restore-*")) == []


def test_restore_replaces_the_database_and_blobs(oil, db, home, capsys, monkeypatch, tmp_path, issue) -> None:
    folder = tmp_path / "bk"
    blob = put_blob(home, "ab/abcdef.gz", b"payload")
    freeze(monkeypatch, _T0)
    assert oil("backup", "--to", str(folder)) == 0
    [file] = folder.glob(backup.BACKUP_PATTERN)
    assert oil("issue", "create", "-p", "OIL", "-s", "Second", "-d", "D") == 0
    blob.unlink()
    freeze(monkeypatch, _T0 + timedelta(hours=1))
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "restore", str(file), "--yes")

    safety = home / "ai_oil.db.before-restore-20261026-101500"
    assert (code, out) == (0, f"Restored {file}; previous database kept at {safety}\n")
    assert issue_count(db) == 1
    assert safety.exists()
    old = sqlite3.connect(safety)
    try:
        assert old.execute("SELECT count(*) FROM issue").fetchone() == (2,)
    finally:
        old.close()
    assert blob.read_bytes() == b"payload"
    with db() as conn:
        [entry] = audit.list_entries(conn, action_prefix="backup.restore")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", str(file))
    assert entry.detail == {"safety_copy": str(safety)}
    assert oil("issue", "show", "OIL-1") == 0


def test_restore_into_a_folder_without_a_database(oil, home, capsys, tmp_path, issue) -> None:
    folder = tmp_path / "bk"
    assert oil("backup", "--to", str(folder)) == 0
    [file] = folder.glob(backup.BACKUP_PATTERN)
    for path in home.glob("ai_oil.db*"):
        path.unlink()
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "restore", str(file), "--yes")

    assert (code, out) == (0, f"Restored {file}; previous database kept at {home / 'ai_oil.db'}\n")
    assert oil("issue", "show", "OIL-1") == 0


def test_restore_of_a_file_that_is_not_a_database(oil, db, home, capsys, tmp_path, issue) -> None:
    bogus = tmp_path / "bogus.db"
    bogus.write_text("this is not sqlite", encoding="utf-8")

    code, out, err = run(oil, capsys, "restore", str(bogus), "--yes")

    assert code == 1
    assert "not an AI_OIL database backup" in err
    assert issue_count(db) == 1
    assert list(home.glob("ai_oil.db.before-restore-*")) == []


def test_restore_of_a_database_without_schema_version(oil, db, home, capsys, tmp_path, issue) -> None:
    other = tmp_path / "other.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.commit()
    connection.close()

    code, _, err = run(oil, capsys, "restore", str(other), "--yes")

    assert code == 1
    assert "not an AI_OIL database backup" in err
    assert issue_count(db) == 1


def test_restore_of_a_missing_file(oil, capsys, tmp_path, issue) -> None:
    code, _, err = run(oil, capsys, "restore", str(tmp_path / "gone.db"), "--yes")

    assert code == 1
    assert "does not exist" in err
