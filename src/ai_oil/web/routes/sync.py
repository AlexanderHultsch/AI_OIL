"""Sync log page: recent runs with results and errors, and "Sync all now" (spec 10.4, SYNC-1, SYNC-10)."""

import json
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import bindparam, text
from starlette.responses import RedirectResponse, Response

from ai_oil.config import Config
from ai_oil.sources.sync import SyncScope, run_sync
from ai_oil.web.deps import db_read, get_config, get_engine
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _local_time

router = APIRouter()

PAGE = "/sync"
MAX_RUNS = 50


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
):
    return render(request, "sync/list.html", {"runs": _runs(conn), "open_run": run, "nav_active": "sync"})


@router.post(PAGE + "/all")
def sync_all(
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Sync every open issue's sources; the fetch runs outside any request transaction (SYNC-1)."""
    run_sync(engine, SyncScope("all"), config=config, trigger="ui")
    return RedirectResponse(PAGE, status_code=303)
