"""Sync log page: recent runs with results and errors, and "Sync all now" (spec 10.4, SYNC-1, SYNC-10)."""

import json
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote, urlencode

import sqlalchemy
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import bindparam, text
from starlette.responses import RedirectResponse, Response

from ai_oil import backup
from ai_oil.config import Config
from ai_oil.sources.sync import SyncScope, run_sync
from ai_oil.web.deps import db_read, get_config, get_engine
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _local_time

router = APIRouter()

PAGE = "/sync"
MAX_RUNS = 50
BACKUP_WRITTEN = "Backup written: "
BACKUP_FAILED = "Automatic backup failed: "


@dataclass(frozen=True)
class ResultRow:
    """A sync result other than `unchanged`."""

    source_object_id: int
    label: str
    connector: str
    outcome: str
    error: str | None
    duration: str


@dataclass(frozen=True)
class RunRow:
    id: int
    started: str
    trigger: str
    scope: str
    status: str
    duration: str
    checked: str
    changed: str
    new_versions: str
    new_entries: str
    errors: str
    results: tuple[ResultRow, ...]


def _stat(stats: dict, *names: str) -> str:
    """The sum of the named stats, or a dash for a run that has none (still running)."""
    if not any(name in stats for name in names):
        return "—"
    return str(sum(int(stats.get(name, 0)) for name in names))


def _duration(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "—"
    return f"{milliseconds} ms" if milliseconds < 1000 else f"{milliseconds / 1000:.1f} s"


def _results(conn: sqlalchemy.Connection, run_ids: list[int]) -> dict[int, list[ResultRow]]:
    """The non-unchanged results of the runs, by run id, in source object order."""
    statement = text(
        "SELECT s.sync_run_id, s.source_object_id, s.outcome, s.error, s.duration_ms,"
        " COALESCE(o.display_ref, o.title, o.external_id), c.name"
        " FROM sync_result s JOIN source_object o ON o.id = s.source_object_id"
        " JOIN connector c ON c.id = o.connector_id"
        " WHERE s.sync_run_id IN :ids AND s.outcome != 'unchanged'"
        " ORDER BY s.sync_run_id, s.source_object_id"
    ).bindparams(bindparam("ids", expanding=True))
    found: dict[int, list[ResultRow]] = {}
    for run_id, object_id, outcome, error, duration_ms, label, connector in conn.execute(
        statement, {"ids": run_ids}
    ):
        found.setdefault(run_id, []).append(
            ResultRow(object_id, label, connector, outcome, error, _duration(duration_ms))
        )
    return found


def _runs(conn: sqlalchemy.Connection) -> list[RunRow]:
    rows = conn.execute(
        text(
            "SELECT id, trigger, scope, started_at, status, stats_json FROM sync_run"
            " ORDER BY started_at DESC, id DESC LIMIT :limit"
        ),
        {"limit": MAX_RUNS},
    ).all()
    results = _results(conn, [row[0] for row in rows]) if rows else {}
    runs = []
    for run_id, trigger, scope, started_at, status, stats_json in rows:
        stats = json.loads(stats_json)
        runs.append(
            RunRow(
                id=run_id,
                started=_local_time(started_at),
                trigger=trigger,
                scope=scope,
                status=status,
                duration=_duration(stats.get("duration_ms")),
                checked=_stat(stats, "checked"),
                changed=_stat(stats, "changed", "new"),
                new_versions=_stat(stats, "new_versions"),
                new_entries=_stat(stats, "new_entries"),
                errors=_stat(stats, "errors"),
                results=tuple(results.get(run_id, ())),
            )
        )
    return runs


@router.get(PAGE)
def sync_log(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    run: Annotated[int | None, Query()] = None,
    backup_path: Annotated[str | None, Query(alias="backup")] = None,
    backup_error: Annotated[str | None, Query()] = None,
):
    context = {
        "runs": _runs(conn),
        "open_run": run,
        "backup_written": f"{BACKUP_WRITTEN}{backup_path}" if backup_path else None,
        "backup_failed": f"{BACKUP_FAILED}{backup_error}" if backup_error else None,
        "nav_active": "sync",
    }
    return render(request, "sync/list.html", context)


def auto_backup(engine: sqlalchemy.Engine, config: Config) -> tuple[str | None, str | None]:
    """The automatic backup after a web-triggered sync (NFR-8): (written path, failure message).

    A failing backup is no error of the request; the caller shows the message next to the sync result.
    """
    try:
        written = backup.maybe_auto_backup(engine, config)
    except Exception as exc:
        return None, str(exc)
    return (str(written) if written is not None else None), None


def sync_redirect(engine: sqlalchemy.Engine, config: Config, run_id: int | None = None) -> Response:
    """Run the automatic backup, then redirect to the log (opened at the run) showing the backup result."""
    written, failed = auto_backup(engine, config)
    params = {"run": run_id, "backup": written, "backup_error": failed}
    query = urlencode({name: value for name, value in params.items() if value is not None}, quote_via=quote)
    return RedirectResponse(f"{PAGE}?{query}" if query else PAGE, status_code=303)


@router.post(PAGE + "/all")
def sync_all(
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Sync every open issue's sources; the fetch runs outside any request transaction (SYNC-1)."""
    run_sync(engine, SyncScope("all"), config=config, trigger="ui")
    return sync_redirect(engine, config)


@router.post(PAGE + "/backup")
def backup_now(
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Write a backup now, whatever the age of the last one (NFR-8)."""
    written = backup.create_backup(engine, config)
    return RedirectResponse(f"{PAGE}?{urlencode({'backup': written}, quote_via=quote)}", status_code=303)
