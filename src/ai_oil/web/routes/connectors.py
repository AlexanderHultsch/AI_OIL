"""Connectors page: status, check, enable and disable, last sync (spec 10.4, CON-4, CON-6)."""

import json
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from starlette.responses import RedirectResponse, Response

from ai_oil.config import Config
from ai_oil.governance import audit
from ai_oil.sources import health, registry
from ai_oil.sources.sync import SyncScope, run_sync
from ai_oil.web.deps import db_read, db_write, get_config, get_engine
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _local_time
from ai_oil.web.routes.sync import sync_redirect

router = APIRouter()

VIA = "web"
PAGE = "/connectors"
NEVER = "never"
NO_CREDENTIAL = "—"


@dataclass(frozen=True)
class ConnectorCard:
    """One connector instance as the page shows it."""

    id: int
    name: str
    type: str
    enabled: bool
    credential: str
    settings: str
    config_lines: tuple[health.CheckLine, ...]
    live_lines: tuple[health.CheckLine, ...]
    last_sync: str
    error_objects: int


def _last_syncs(conn: sqlalchemy.Connection) -> dict[int, str]:
    """Per connector id: the newest start of a sync run that has a result for one of its objects."""
    rows = conn.execute(
        text(
            "SELECT o.connector_id, MAX(r.started_at) FROM sync_result s"
            " JOIN sync_run r ON r.id = s.sync_run_id"
            " JOIN source_object o ON o.id = s.source_object_id GROUP BY o.connector_id"
        )
    )
    return {connector_id: started for connector_id, started in rows}


def _error_objects(conn: sqlalchemy.Connection) -> dict[int, int]:
    """Per connector id: linked source objects whose last fetch failed."""
    rows = conn.execute(
        text(
            "SELECT o.connector_id, COUNT(DISTINCT o.id) FROM source_object o"
            " JOIN issue_source_link l ON l.source_object_id = o.id AND l.removed_at IS NULL"
            " WHERE o.fetch_status = 'error' GROUP BY o.connector_id"
        )
    )
    return {connector_id: count for connector_id, count in rows}


def _card(
    row: registry.ConnectorRow,
    *,
    last_sync: str | None,
    error_objects: int,
    live: bool = False,
) -> ConnectorCard:
    """The card of `row`; the configuration check is the one of `oil connector check`, without --live."""
    config_lines: list[health.CheckLine] = []
    live_lines: list[health.CheckLine] = []
    if row.enabled:
        connector, config_lines = health.check_configuration(row)
        if live and connector is not None:
            live_lines = health.check_live(connector)
    credential = health.credential_state(row)
    return ConnectorCard(
        id=row.id,
        name=row.name,
        type=row.type,
        enabled=row.enabled,
        credential=NO_CREDENTIAL if credential == "-" else credential,
        settings=json.dumps(row.settings, indent=2, sort_keys=True),
        config_lines=tuple(config_lines),
        live_lines=tuple(live_lines),
        last_sync=_local_time(last_sync) if last_sync else NEVER,
        error_objects=error_objects,
    )


@router.get(PAGE)
def connector_list(request: Request, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]):
    rows = registry.list_connectors(conn)
    last_syncs = _last_syncs(conn)
    errors = _error_objects(conn)
    cards = [
        _card(row, last_sync=last_syncs.get(row.id), error_objects=errors.get(row.id, 0)) for row in rows
    ]
    registry.available_connector_types()
    return render(
        request,
        "connectors/list.html",
        {"cards": cards, "load_errors": dict(registry.load_errors), "nav_active": "connectors"},
    )


@router.post(PAGE + "/{connector_id}/check")
def live_check(
    request: Request, connector_id: int, engine: Annotated[sqlalchemy.Engine, Depends(get_engine)]
) -> Response:
    """Run the read-only live check of one connector (class A, no audit entry) outside any transaction."""
    with engine.begin() as conn:
        row = registry.get_connector(conn, connector_id)
        last_sync = _last_syncs(conn).get(row.id)
        errors = _error_objects(conn).get(row.id, 0)
    card = _card(row, last_sync=last_sync, error_objects=errors, live=True)
    return render(request, "connectors/_card.html", {"card": card})


def _set_enabled(conn: sqlalchemy.Connection, connector_id: int, enabled: bool) -> Response:
    row = registry.set_enabled(conn, connector_id, enabled)
    audit.log(
        conn,
        "connector.enable" if enabled else "connector.disable",
        "B",
        actor_type="user",
        target=row.name,
        detail={"type": row.type, "via": VIA},
    )
    return RedirectResponse(PAGE, status_code=303)


@router.post(PAGE + "/{connector_id}/enable")
def enable(connector_id: int, conn: Annotated[sqlalchemy.Connection, Depends(db_write)]) -> Response:
    return _set_enabled(conn, connector_id, True)


@router.post(PAGE + "/{connector_id}/disable")
def disable(connector_id: int, conn: Annotated[sqlalchemy.Connection, Depends(db_write)]) -> Response:
    return _set_enabled(conn, connector_id, False)


@router.post(PAGE + "/{connector_id}/sync")
def sync_now(
    connector_id: int,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Sync every linked source of one connector; the fetch runs outside any request transaction (SYNC-1)."""
    report = run_sync(engine, SyncScope("connector", connector_id), config=config, trigger="ui")
    return sync_redirect(engine, config, report.run_id)
