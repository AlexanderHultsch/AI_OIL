"""On-demand synchronisation of linked sources (spec chapter 7, SYNC-1 .. SYNC-11)."""

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy
from sqlalchemy import text

from ai_oil.config import Config
from ai_oil.domain.issues import STATUS_CATEGORY
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.blobs import BlobStore
from ai_oil.sources.model import FetchResult, ObjectState
from ai_oil.sources.registry import ConnectorRow
from ai_oil.timeutil import parse_iso, utcnow_iso

SCOPE_KINDS = ("all", "issue", "connector", "source")

ConnectorFactory = Callable[[ConnectorRow], object]


@dataclass(frozen=True)
class SyncScope:
    """What a sync covers (SYNC-1): everything, one issue, one connector or one source object."""

    kind: str
    value: str | int | None = None

    def __post_init__(self) -> None:
        if self.kind not in SCOPE_KINDS:
            raise ValueError(f"unknown sync scope: {self.kind!r}")
        if self.kind != "all" and self.value is None:
            raise ValueError(f"sync scope {self.kind!r} needs a value")

    def describe(self) -> str:
        """The text stored in `sync_run.scope`."""
        return "all" if self.kind == "all" else f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class ObjectResult:
    """The outcome of one source object in a sync run (SYNC-10)."""

    source_object_id: int
    label: str
    connector_name: str
    outcome: str
    new_versions: int
    new_entries: int
    error: str | None
    duration_ms: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SyncReport:
    """A finished sync run: status `ok`, `partial` or `failed`, one result per object (SYNC-7, SYNC-10)."""

    run_id: int
    scope: str
    status: str
    results: tuple[ObjectResult, ...]
    stats: dict


@dataclass(frozen=True)
class _Item:
    state: ObjectState
    label: str
    target: str


def _cutoff_iso(days: int) -> str:
    cutoff = parse_iso(utcnow_iso()) - timedelta(days=days)
    return f"{cutoff:%Y-%m-%dT%H:%M:%S}.{cutoff.microsecond // 1000:03d}Z"


def _resolve_object_ids(
    conn: sqlalchemy.Connection, scope: SyncScope, config: Config, include_done: bool
) -> list[int]:
    """The objects a scope covers, ascending and without duplicates (SYNC-1, SYNC-5, SYNC-6)."""
    ids: list[int]
    if scope.kind == "source":
        obj = store.get_object(conn, int(scope.value))  # type: ignore[arg-type]
        ids = [obj.parent_id if obj.parent_id is not None else obj.id]
    elif scope.kind == "issue":
        ids = linking.linked_object_ids(conn, str(scope.value))
    elif scope.kind == "connector":
        connector = registry.get_connector(conn, scope.value)  # type: ignore[arg-type]
        ids = list(
            conn.execute(
                text(
                    "SELECT DISTINCT o.id FROM source_object o"
                    " JOIN issue_source_link l ON l.source_object_id = o.id"
                    " WHERE o.connector_id = :connector_id AND o.parent_id IS NULL AND l.removed_at IS NULL"
                    " ORDER BY o.id"
                ),
                {"connector_id": connector.id},
            ).scalars()
        )
    elif include_done:
        ids = linking.all_linked_object_ids(conn)
    else:
        open_statuses = [status for status, category in STATUS_CATEGORY.items() if category != "Done"]
        ids = linking.all_linked_object_ids(
            conn,
            statuses=open_statuses,
            resolved_after=_cutoff_iso(config.sync.done_grace_days),
        )
    return sorted(set(ids))


def _plan(engine: sqlalchemy.Engine, object_ids: list[int]) -> list[tuple[ConnectorRow, list[_Item]]]:
    """Read every object's state up front, grouped by connector (SYNC-3)."""
    grouped: dict[int, list[_Item]] = {}
    with engine.begin() as conn:
        for object_id in object_ids:
            obj = store.get_object(conn, object_id)
            item = _Item(
                state=store.object_state(conn, object_id),
                label=linking.source_label(obj),
                target=obj.locator or linking.source_label(obj),
            )
            grouped.setdefault(obj.connector_id, []).append(item)
        return [(registry.get_connector(conn, cid), grouped[cid]) for cid in sorted(grouped)]


def _write_result(conn: sqlalchemy.Connection, run_id: int, result: ObjectResult, target: str) -> None:
    """Record one object's outcome and its audit entry (SYNC-10, GOV-7)."""
    conn.execute(
        text(
            "INSERT INTO sync_result (sync_run_id, source_object_id, outcome, new_versions, new_entries,"
            " error, duration_ms) VALUES (:run_id, :object_id, :outcome, :new_versions, :new_entries,"
            " :error, :duration_ms)"
        ),
        {
            "run_id": run_id,
            "object_id": result.source_object_id,
            "outcome": result.outcome,
            "new_versions": result.new_versions,
            "new_entries": result.new_entries,
            "error": result.error,
            "duration_ms": result.duration_ms,
        },
    )
    audit.log(
        conn,
        "source.fetch",
        "A",
        actor_type="sync",
        target=target,
        detail={
            "source_object_id": result.source_object_id,
            "outcome": result.outcome,
            "sync_run_id": run_id,
        },
    )


def _result(item: _Item, row: ConnectorRow, outcome: str, **values: object) -> ObjectResult:
    fields: dict[str, object] = {
        "new_versions": 0,
        "new_entries": 0,
        "error": None,
        "duration_ms": 0,
        "warnings": (),
    }
    fields.update(values)
    return ObjectResult(
        source_object_id=item.state.id,
        label=item.label,
        connector_name=row.name,
        outcome=outcome,
        **fields,  # type: ignore[arg-type]
    )


def _skip(engine: sqlalchemy.Engine, run_id: int, row: ConnectorRow, item: _Item) -> ObjectResult:
    """A disabled connector's object is not fetched (CON-4, DM-46)."""
    result = _result(item, row, "skipped")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_object SET fetch_status = 'skipped' WHERE id = :id"), {"id": item.state.id}
        )
        _write_result(conn, run_id, result, item.target)
    return result


def _fail(
    engine: sqlalchemy.Engine,
    run_id: int,
    row: ConnectorRow,
    item: _Item,
    message: str,
    duration_ms: int = 0,
) -> ObjectResult:
    """One failing object never aborts the run (SYNC-4, AP-10)."""
    result = _result(item, row, "error", error=message, duration_ms=duration_ms)
    with engine.begin() as conn:
        store.mark_error(conn, item.state.id, message)
        _write_result(conn, run_id, result, item.target)
    return result


def _timed_fetch(connector: object, state: ObjectState) -> tuple[FetchResult | None, Exception | None, int]:
    started = time.monotonic()
    try:
        fetched = connector.fetch(state)  # type: ignore[attr-defined]
    except Exception as exc:
        return None, exc, int((time.monotonic() - started) * 1000)
    return fetched, None, int((time.monotonic() - started) * 1000)


def _sync_connector(
    engine: sqlalchemy.Engine,
    run_id: int,
    row: ConnectorRow,
    connector: object,
    items: list[_Item],
    config: Config,
) -> list[ObjectResult]:
    """Fetch a connector's objects in parallel; store each in its own transaction (SYNC-2, SYNC-3)."""
    blobs = BlobStore(config.blob_dir, config.storage.max_blob_mb * 1024 * 1024)
    workers = max(1, int(getattr(type(connector), "max_parallel", 1)))
    results: list[ObjectResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_timed_fetch, connector, item.state): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            fetched, fetch_error, fetch_ms = future.result()
            if fetch_error is not None:
                message = f"{type(fetch_error).__name__}: {fetch_error}"
                results.append(_fail(engine, run_id, row, item, message, fetch_ms))
                continue
            applying = time.monotonic()
            try:
                with engine.begin() as conn:
                    applied = store.apply_fetch(
                        conn,
                        item.state,
                        fetched,  # type: ignore[arg-type]
                        sync_run_id=run_id,
                        blobs=blobs,
                        max_text_chars=config.storage.max_text_chars,
                    )
                    if applied.gone:
                        outcome = "gone"
                    elif (
                        applied.changed
                        and item.state.cursor is None
                        and item.state.latest_content_hash is None
                    ):
                        outcome = "new"
                    elif applied.changed:
                        outcome = "changed"
                    else:
                        outcome = "unchanged"
                    result = _result(
                        item,
                        row,
                        outcome,
                        new_versions=len(applied.new_version_ids),
                        new_entries=len(applied.new_entry_ids),
                        duration_ms=fetch_ms + int((time.monotonic() - applying) * 1000),
                        warnings=tuple(fetched.warnings),  # type: ignore[union-attr]
                    )
                    _write_result(conn, run_id, result, item.target)
            except Exception as exc:
                duration_ms = fetch_ms + int((time.monotonic() - applying) * 1000)
                results.append(_fail(engine, run_id, row, item, f"{type(exc).__name__}: {exc}", duration_ms))
                continue
            results.append(result)
    return results


def run_sync(
    engine: sqlalchemy.Engine,
    scope: SyncScope,
    *,
    config: Config,
    trigger: str = "cli",
    include_done: bool = False,
    connector_factory: ConnectorFactory | None = None,
) -> SyncReport:
    """Synchronise the objects of `scope`, one transaction per object (SYNC-1 .. SYNC-6, SYNC-10, SYNC-11).

    Fetching is the only parallel part and does no database work. A failing object is recorded as an error
    and never aborts the run (SYNC-4, AP-10); the run ends `ok`, `partial` or `failed`. A single-source
    scope serves the initial fetch of a newly linked object (SYNC-9).
    """
    started = time.monotonic()
    with engine.begin() as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO sync_run (trigger, scope, started_at, status)"
                " VALUES (:trigger, :scope, :now, 'running') RETURNING id"
            ),
            {"trigger": trigger, "scope": scope.describe(), "now": utcnow_iso()},
        ).scalar_one()
        object_ids = _resolve_object_ids(conn, scope, config, include_done)

    results: list[ObjectResult] = []
    for row, items in _plan(engine, object_ids):
        if not row.enabled:
            results.extend(_skip(engine, run_id, row, item) for item in items)
            continue
        try:
            connector = connector_factory(row) if connector_factory is not None else registry.instantiate(row)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            results.extend(_fail(engine, run_id, row, item, message) for item in items)
            continue
        results.extend(_sync_connector(engine, run_id, row, connector, items, config))
    results.sort(key=lambda result: result.source_object_id)

    counts = {
        outcome: sum(1 for result in results if result.outcome == outcome)
        for outcome in ("new", "changed", "unchanged", "gone", "skipped", "error")
    }
    if counts["error"] == 0:
        status = "ok"
    elif counts["new"] + counts["changed"] + counts["unchanged"] + counts["gone"] == 0:
        status = "failed"
    else:
        status = "partial"
    stats = {
        "checked": len(results),
        "new": counts["new"],
        "changed": counts["changed"],
        "unchanged": counts["unchanged"],
        "gone": counts["gone"],
        "skipped": counts["skipped"],
        "errors": counts["error"],
        "new_versions": sum(result.new_versions for result in results),
        "new_entries": sum(result.new_entries for result in results),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sync_run SET finished_at = :now, status = :status, stats_json = :stats_json"
                " WHERE id = :id"
            ),
            {"now": utcnow_iso(), "status": status, "stats_json": json.dumps(stats), "id": run_id},
        )
    return SyncReport(
        run_id=run_id, scope=scope.describe(), status=status, results=tuple(results), stats=stats
    )


def format_report(report: SyncReport) -> str:
    """The summary shown after every run: checked, changed, new versions and entries, errors, duration (SYNC-7)."""  # noqa: E501
    stats = report.stats
    lines = [
        f"Sync {report.scope}: {report.status} — {stats['checked']} checked,"
        f" {stats['changed'] + stats['new']} changed, {stats['new_versions']} new versions,"
        f" {stats['new_entries']} new entries, {stats['errors']} errors ({stats['duration_ms']} ms)"
    ]
    for result in report.results:
        if result.outcome == "unchanged" and not result.warnings:
            continue
        line = f"  {result.outcome:9} {result.label} [{result.connector_name}]"
        if result.error:
            line += f": {result.error}"
        lines.append(line)
        lines.extend(f"  ! {warning}" for warning in result.warnings)
    return "\n".join(lines)
