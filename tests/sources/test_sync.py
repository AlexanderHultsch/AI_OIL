import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.config import Config, load_config, write_default_config
from ai_oil.domain import issues, people, projects
from ai_oil.errors import NotFoundError
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)
from ai_oil.sources.sync import ObjectResult, SyncReport, SyncScope, format_report, run_sync

_NOW = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


class Script(dict):
    """Scripted fetch results per external id (a FetchResult or an exception), plus call bookkeeping."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()


class FakeConnector:
    type: ClassVar[str] = "scripted"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue", "attachment"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 3

    def __init__(self, config: ConnectorConfig, *, script: Script, **backends: object) -> None:
        self.config = config
        self.script = script

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("reachable", True, "ok"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        return None

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(ref.object_type, ref.external_id)

    def fetch(self, state: ObjectState) -> FetchResult:
        script = self.script
        with script.lock:
            script.calls.append(state.external_id)
            script.active += 1
            script.max_active = max(script.max_active, script.active)
        try:
            if script.delay:
                time.sleep(script.delay)
            scripted = script[state.external_id]
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        finally:
            with script.lock:
                script.active -= 1

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


def body(text: str = "body", *, cursor: str = "c1", entries: tuple[EntryData, ...] = (), warnings=()):
    return FetchResult(version=VersionData(text), cursor=cursor, entries=entries, warnings=tuple(warnings))


def comment(external_id: str) -> EntryData:
    return EntryData("jira_comment", external_id, "2026-10-01T10:00:00.000Z", f"comment {external_id}")


@dataclass
class Env:
    engine: sqlalchemy.Engine
    config: Config
    script: Script
    actor_id: int

    def factory(self, row: registry.ConnectorRow) -> FakeConnector:
        return FakeConnector(registry.to_config(row), script=self.script)

    def sync(self, scope: SyncScope, **kwargs: object) -> SyncReport:
        return run_sync(self.engine, scope, config=self.config, connector_factory=self.factory, **kwargs)

    def connector(self, name: str = "Fake") -> int:
        with self.engine.begin() as conn:
            return registry.create_connector(conn, "scripted", name).id

    def issue(self, summary: str = "Issue") -> str:
        with self.engine.begin() as conn:
            return issues.create_issue(conn, "OIL", summary, "Description", actor_id=self.actor_id).key

    def obj(
        self, connector_id: int, external_id: str, *, locator: str | None = None, parent_id: int | None = None
    ) -> int:
        object_type = "attachment" if parent_id is not None else "jira_issue"
        meta = ObjectMeta(object_type, external_id, display_ref=f"REF-{external_id}", locator=locator)
        with self.engine.begin() as conn:
            return store.upsert_object(conn, connector_id, meta, parent_id=parent_id)

    def link(self, key: str, object_id: int) -> None:
        with self.engine.begin() as conn:
            linking.link_source(conn, key, object_id, actor_id=self.actor_id)

    def linked(self, key: str, connector_id: int, *external_ids: str) -> list[int]:
        ids = [self.obj(connector_id, external_id) for external_id in external_ids]
        for object_id in ids:
            self.link(key, object_id)
        return ids

    def one(self, sql: str, **params: object):
        with self.engine.connect() as conn:
            return conn.execute(sqlalchemy.text(sql), params).one()

    def all(self, sql: str, **params: object):
        with self.engine.connect() as conn:
            return conn.execute(sqlalchemy.text(sql), params).all()


@pytest.fixture(autouse=True)
def scripted_registered() -> Iterator[None]:
    registry.register_connector_type(FakeConnector)
    yield
    registry.unregister_connector_type("scripted")


@pytest.fixture
def env(engine: sqlalchemy.Engine, tmp_path) -> Env:
    home = tmp_path / "sync_home"
    write_default_config(home)
    with engine.begin() as conn:
        me = people.create_person(conn, "Alex", is_me=True)
        projects.create_project(conn, "OIL", "Open issues")
    return Env(engine, load_config(home), Script(), me.id)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    holder = [_NOW]
    monkeypatch.setattr(timeutil, "_now", lambda: holder[0])
    return holder


def outcomes(report: SyncReport) -> dict[int, str]:
    return {result.source_object_id: result.outcome for result in report.results}


# --- scope -----------------------------------------------------------------------------------------------


def test_scope_describe() -> None:
    assert SyncScope("all").describe() == "all"
    assert SyncScope("issue", "OIL-1").describe() == "issue:OIL-1"
    assert SyncScope("connector", "Jira").describe() == "connector:Jira"
    assert SyncScope("source", 42).describe() == "source:42"


def test_scope_rejects_unknown_kind_and_missing_value() -> None:
    with pytest.raises(ValueError, match="unknown sync scope"):
        SyncScope("everything")
    with pytest.raises(ValueError, match="needs a value"):
        SyncScope("issue")


# --- issue scope: new, unchanged, changed ---------------------------------------------------------------


def test_first_sync_stores_both_objects_as_new(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    a = env.obj(connector_id, "A", locator="https://jira/A")
    b = env.obj(connector_id, "B")
    env.link(key, a)
    env.link(key, b)
    env.script["A"] = body("text a", entries=(comment("c1"),))
    env.script["B"] = body("text b", cursor="c9", entries=(comment("c2"), comment("c3")))

    report = env.sync(SyncScope("issue", key))

    assert report.status == "ok"
    assert report.scope == f"issue:{key}"
    assert outcomes(report) == {a: "new", b: "new"}
    assert [(r.new_versions, r.new_entries) for r in report.results] == [(1, 1), (1, 2)]
    assert [r.label for r in report.results] == ["jira_issue: REF-A", "jira_issue: REF-B"]
    assert {r.connector_name for r in report.results} == {"Fake"}
    assert report.stats["checked"] == 2
    assert (report.stats["new"], report.stats["changed"], report.stats["errors"]) == (2, 0, 0)
    assert (report.stats["new_versions"], report.stats["new_entries"]) == (2, 3)

    with env.engine.connect() as conn:
        assert [v.text for v in store.list_versions(conn, a)] == ["text a"]
        assert [e.external_id for e in store.list_entries(conn, b)] == ["c2", "c3"]
        assert store.get_object(conn, b).cursor == "c9"
        assert store.list_versions(conn, a)[0].sync_run_id == report.run_id

    run = env.one("SELECT trigger, scope, status, finished_at, stats_json FROM sync_run")
    assert (run.trigger, run.scope, run.status) == ("cli", f"issue:{key}", "ok")
    assert run.finished_at is not None
    assert '"new_entries": 3' in run.stats_json
    rows = env.all(
        "SELECT source_object_id, outcome, new_versions, new_entries, error, duration_ms FROM sync_result"
        " ORDER BY source_object_id"
    )
    assert [(r.source_object_id, r.outcome, r.new_versions, r.new_entries, r.error) for r in rows] == [
        (a, "new", 1, 1, None),
        (b, "new", 1, 2, None),
    ]
    assert all(r.duration_ms is not None and r.duration_ms >= 0 for r in rows)

    with env.engine.connect() as conn:
        entries = audit.list_entries(conn, action_prefix="source.fetch")
    assert len(entries) == 2
    assert {e.action_class for e in entries} == {"A"}
    assert {e.actor_type for e in entries} == {"sync"}
    assert {e.target for e in entries} == {"https://jira/A", "jira_issue: REF-B"}
    assert {e.detail["source_object_id"] for e in entries} == {a, b}
    assert {e.detail["outcome"] for e in entries} == {"new"}
    assert {e.detail["sync_run_id"] for e in entries} == {report.run_id}


def test_trigger_is_recorded(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    env.linked(key, connector_id, "A")
    env.script["A"] = body()

    env.sync(SyncScope("issue", key), trigger="ui")

    assert env.one("SELECT trigger FROM sync_run").trigger == "ui"


def test_second_sync_is_unchanged_then_changed(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    (a,) = env.linked(key, connector_id, "A")
    env.script["A"] = body("v1", entries=(comment("c1"),))
    env.sync(SyncScope("issue", key))

    second = env.sync(SyncScope("issue", key))
    assert outcomes(second) == {a: "unchanged"}
    assert (second.results[0].new_versions, second.results[0].new_entries) == (0, 0)
    assert (second.stats["unchanged"], second.stats["new_versions"]) == (1, 0)

    env.script["A"] = body("v2", cursor="c2", entries=(comment("c1"), comment("c4")))
    third = env.sync(SyncScope("issue", key))
    assert outcomes(third) == {a: "changed"}
    assert (third.results[0].new_versions, third.results[0].new_entries) == (1, 1)
    assert third.stats["changed"] == 1

    with env.engine.connect() as conn:
        assert [v.text for v in store.list_versions(conn, a)] == ["v1", "v2"]
    assert env.one("SELECT COUNT(*) AS n FROM sync_run WHERE status = 'ok'").n == 3


def test_removed_links_are_not_synced(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    a, b = env.linked(key, connector_id, "A", "B")
    with env.engine.begin() as conn:
        linking.unlink_source(conn, key, b, actor_id=env.actor_id)
    env.script["A"] = body()

    report = env.sync(SyncScope("issue", key))

    assert outcomes(report) == {a: "new"}
    assert env.script.calls == ["A"]


def test_empty_scope_is_an_ok_run(env: Env) -> None:
    key = env.issue()

    report = env.sync(SyncScope("issue", key))

    assert (report.status, report.results, report.stats["checked"]) == ("ok", (), 0)


# --- failures -------------------------------------------------------------------------------------------


def test_one_failing_fetch_leaves_the_other_stored_and_run_partial(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    a, b = env.linked(key, connector_id, "A", "B")
    env.script["A"] = RuntimeError("boom")
    env.script["B"] = body("text b")

    report = env.sync(SyncScope("issue", key))

    assert report.status == "partial"
    assert outcomes(report) == {a: "error", b: "new"}
    assert report.results[0].error == "RuntimeError: boom"
    assert report.stats["errors"] == 1
    with env.engine.connect() as conn:
        failed = store.get_object(conn, a)
        assert (failed.fetch_status, failed.fetch_error) == ("error", "RuntimeError: boom")
        assert store.get_object(conn, b).fetch_status == "ok"
        assert [v.text for v in store.list_versions(conn, b)] == ["text b"]
        assert store.list_versions(conn, a) == []
    assert env.one("SELECT status FROM sync_run").status == "partial"
    row = env.one("SELECT outcome, error FROM sync_result WHERE source_object_id = :id", id=a)
    assert (row.outcome, row.error) == ("error", "RuntimeError: boom")


def test_all_failing_ends_failed(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    env.linked(key, connector_id, "A", "B")
    env.script["A"] = RuntimeError("one")
    env.script["B"] = ValueError("two")

    report = env.sync(SyncScope("issue", key))

    assert report.status == "failed"
    assert [r.error for r in report.results] == ["RuntimeError: one", "ValueError: two"]
    assert env.one("SELECT status FROM sync_run").status == "failed"


def test_apply_failure_rolls_back_only_that_object(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    connector_id = env.connector()
    key = env.issue()
    a, b = env.linked(key, connector_id, "A", "B")
    env.script["A"] = body("text a")
    env.script["B"] = body("text b", entries=(comment("c1"),))
    original = store.apply_fetch

    def apply_fetch(conn, state, result, **kwargs):
        applied = original(conn, state, result, **kwargs)
        if state.external_id == "B":
            raise RuntimeError("disk full")
        return applied

    monkeypatch.setattr(store, "apply_fetch", apply_fetch)

    report = env.sync(SyncScope("issue", key))

    assert report.status == "partial"
    assert outcomes(report) == {a: "new", b: "error"}
    assert report.results[1].error == "RuntimeError: disk full"
    with env.engine.connect() as conn:
        assert [v.text for v in store.list_versions(conn, a)] == ["text a"]
        assert store.list_versions(conn, b) == []
        assert store.list_entries(conn, b) == []
        broken = store.get_object(conn, b)
        assert (broken.fetch_status, broken.fetch_error) == ("error", "RuntimeError: disk full")
        assert broken.cursor is None
    rows = env.all("SELECT source_object_id, outcome FROM sync_result ORDER BY source_object_id")
    assert [(r.source_object_id, r.outcome) for r in rows] == [(a, "new"), (b, "error")]


def test_gone_keeps_earlier_versions(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    (a,) = env.linked(key, connector_id, "A")
    env.script["A"] = body("kept")
    env.sync(SyncScope("issue", key))

    env.script["A"] = FetchResult(status="gone")
    report = env.sync(SyncScope("issue", key))

    assert outcomes(report) == {a: "gone"}
    assert report.status == "ok"
    assert report.stats["gone"] == 1
    with env.engine.connect() as conn:
        assert store.get_object(conn, a).fetch_status == "gone"
        assert [v.text for v in store.list_versions(conn, a)] == ["kept"]


# --- scopes ---------------------------------------------------------------------------------------------


def test_shared_object_is_fetched_once_in_an_all_run(env: Env) -> None:
    connector_id = env.connector()
    first, second = env.issue("First"), env.issue("Second")
    (shared,) = env.linked(first, connector_id, "S")
    env.link(second, shared)
    (only,) = env.linked(second, connector_id, "O")
    env.script["S"] = body()
    env.script["O"] = body()

    report = env.sync(SyncScope("all"))

    assert sorted(env.script.calls) == ["O", "S"]
    assert outcomes(report) == {shared: "new", only: "new"}


def test_all_skips_old_done_issues_unless_include_done(env: Env, clock: list[datetime]) -> None:
    connector_id = env.connector()
    open_new, in_progress = env.issue("New"), env.issue("Progress")
    old_done, recent_done, old_cancelled = env.issue("Old"), env.issue("Recent"), env.issue("Cancelled")
    ids = {
        key: env.linked(key, connector_id, key)[0]
        for key in (open_new, in_progress, old_done, recent_done, old_cancelled)
    }
    for key in ids:
        env.script[key] = body()

    def move(key: str, status: str, when: datetime, reason: str | None = None) -> None:
        clock[0] = when
        with env.engine.begin() as conn:
            issues.transition_issue(conn, key, status, actor_id=env.actor_id, reason=reason)

    move(in_progress, "In Progress", _NOW - timedelta(days=30))
    move(old_done, "Done", _NOW - timedelta(days=20))
    move(old_cancelled, "Cancelled", _NOW - timedelta(days=15), reason="dropped")
    move(recent_done, "Done", _NOW - timedelta(days=3))
    clock[0] = _NOW

    report = env.sync(SyncScope("all"))
    assert set(outcomes(report)) == {ids[open_new], ids[in_progress], ids[recent_done]}
    assert report.scope == "all"

    everything = env.sync(SyncScope("all"), include_done=True)
    assert set(outcomes(everything)) == set(ids.values())


def test_connector_scope_covers_only_linked_top_level_objects_of_that_connector(env: Env) -> None:
    one, two = env.connector("One"), env.connector("Two")
    key = env.issue()
    a, b = env.linked(key, one, "A", "B")
    (c,) = env.linked(key, two, "C")
    env.obj(one, "unlinked")
    with env.engine.begin() as conn:
        linking.unlink_source(conn, key, b, actor_id=env.actor_id)
    env.script.update({"A": body(), "B": body(), "C": body()})

    by_name = env.sync(SyncScope("connector", "One"))
    assert outcomes(by_name) == {a: "new"}
    assert by_name.scope == "connector:One"

    by_id = env.sync(SyncScope("connector", two))
    assert outcomes(by_id) == {c: "new"}
    assert sorted(env.script.calls) == ["A", "C"]


def test_unknown_connector_issue_or_source_raises_and_leaves_no_run(env: Env) -> None:
    for scope in (SyncScope("connector", "Nope"), SyncScope("issue", "OIL-99"), SyncScope("source", 999)):
        with pytest.raises(NotFoundError):
            env.sync(scope)
    assert env.one("SELECT COUNT(*) AS n FROM sync_run").n == 0


def test_source_scope_with_a_child_fetches_the_parent(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    (parent,) = env.linked(key, connector_id, "P")
    child = env.obj(connector_id, "P#1", parent_id=parent)
    env.script["P"] = body("parent text")

    report = env.sync(SyncScope("source", child))

    assert report.scope == f"source:{child}"
    assert outcomes(report) == {parent: "new"}
    assert env.script.calls == ["P"]


def test_source_scope_serves_the_initial_fetch(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    (a,) = env.linked(key, connector_id, "A")
    env.linked(key, connector_id, "B")
    env.script.update({"A": body("fresh"), "B": body()})

    report = env.sync(SyncScope("source", a))

    assert outcomes(report) == {a: "new"}
    assert env.script.calls == ["A"]


# --- connectors -----------------------------------------------------------------------------------------


def test_disabled_connector_is_skipped_without_fetching(env: Env) -> None:
    off, on = env.connector("Off"), env.connector("On")
    key = env.issue()
    (a,) = env.linked(key, off, "A")
    (b,) = env.linked(key, on, "B")
    with env.engine.begin() as conn:
        registry.set_enabled(conn, "Off", False)
    env.script["B"] = body()

    report = env.sync(SyncScope("issue", key))

    assert report.status == "ok"
    assert outcomes(report) == {a: "skipped", b: "new"}
    assert env.script.calls == ["B"]
    assert report.stats["skipped"] == 1
    with env.engine.connect() as conn:
        assert store.get_object(conn, a).fetch_status == "skipped"
    assert env.one("SELECT outcome FROM sync_result WHERE source_object_id = :id", id=a).outcome == "skipped"
    with env.engine.connect() as conn:
        assert len(audit.list_entries(conn, action_prefix="source.fetch")) == 2


def test_failing_factory_marks_that_connectors_objects_and_others_continue(env: Env) -> None:
    broken, fine = env.connector("Broken"), env.connector("Fine")
    key = env.issue()
    a, b = env.linked(key, broken, "A", "B")
    (c,) = env.linked(key, fine, "C")
    env.script["C"] = body()

    def factory(row: registry.ConnectorRow) -> FakeConnector:
        if row.name == "Broken":
            raise RuntimeError("no credentials")
        return env.factory(row)

    report = run_sync(env.engine, SyncScope("issue", key), config=env.config, connector_factory=factory)

    assert report.status == "partial"
    assert outcomes(report) == {a: "error", b: "error", c: "new"}
    assert {r.error for r in report.results if r.outcome == "error"} == {"RuntimeError: no credentials"}
    with env.engine.connect() as conn:
        assert store.get_object(conn, a).fetch_error == "RuntimeError: no credentials"
        assert store.get_object(conn, c).fetch_status == "ok"
    assert env.one("SELECT COUNT(*) AS n FROM sync_result").n == 3


def test_default_factory_uses_the_registry(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    connector_id = env.connector()
    key = env.issue()
    (a,) = env.linked(key, connector_id, "A")
    env.script["A"] = body()
    monkeypatch.setattr(
        registry,
        "instantiate",
        lambda row, **backends: FakeConnector(registry.to_config(row), script=env.script),
    )

    report = run_sync(env.engine, SyncScope("issue", key), config=env.config)

    assert outcomes(report) == {a: "new"}


# --- warnings and report --------------------------------------------------------------------------------


def test_warnings_surface_in_result_and_report(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    (a,) = env.linked(key, connector_id, "A")
    env.script["A"] = body(warnings=("attachment skipped: too large",))

    report = env.sync(SyncScope("issue", key))

    assert report.results[0].warnings == ("attachment skipped: too large",)
    text = format_report(report)
    assert "  new       jira_issue: REF-A [Fake]" in text
    assert "  ! attachment skipped: too large" in text

    again = env.sync(SyncScope("issue", key))
    assert again.results[0].outcome == "unchanged"
    assert "  ! attachment skipped: too large" in format_report(again)
    assert a == again.results[0].source_object_id


def test_format_report_summary_and_lines() -> None:
    def result(object_id: int, label: str, outcome: str, **values: object) -> ObjectResult:
        fields: dict[str, object] = {
            "new_versions": 0,
            "new_entries": 0,
            "error": None,
            "duration_ms": 1,
            "warnings": (),
        }
        fields.update(values)
        return ObjectResult(object_id, label, "Jira", outcome, **fields)  # type: ignore[arg-type]

    report = SyncReport(
        run_id=7,
        scope="issue:OIL-1",
        status="partial",
        results=(
            result(1, "jira_issue: ABC-1", "new", new_versions=1, new_entries=2),
            result(2, "jira_issue: ABC-2", "changed", new_versions=1),
            result(3, "jira_issue: ABC-3", "unchanged"),
            result(4, "jira_issue: ABC-4", "error", error="RuntimeError: boom", warnings=("slow",)),
        ),
        stats={
            "checked": 4,
            "new": 1,
            "changed": 1,
            "unchanged": 1,
            "gone": 0,
            "skipped": 0,
            "errors": 1,
            "new_versions": 2,
            "new_entries": 2,
            "duration_ms": 42,
        },
    )

    assert format_report(report).splitlines() == [
        "Sync issue:OIL-1: partial — 4 checked, 2 changed, 2 new versions, 2 new entries, 1 errors (42 ms)",
        "  new       jira_issue: ABC-1 [Jira]",
        "  changed   jira_issue: ABC-2 [Jira]",
        "  error     jira_issue: ABC-4 [Jira]: RuntimeError: boom",
        "  ! slow",
    ]


# --- parallelism ----------------------------------------------------------------------------------------


def test_fetches_run_in_parallel_up_to_max_parallel(env: Env) -> None:
    connector_id = env.connector()
    key = env.issue()
    names = [f"O{n}" for n in range(6)]
    env.linked(key, connector_id, *names)
    for name in names:
        env.script[name] = body(name)
    env.script.delay = 0.05

    report = env.sync(SyncScope("issue", key))

    assert report.status == "ok"
    assert len(report.results) == 6
    assert 1 < env.script.max_active <= FakeConnector.max_parallel


def test_missing_max_parallel_means_sequential(env: Env) -> None:
    class Sequential:
        def __init__(self, script: Script) -> None:
            self.script = script

        fetch = FakeConnector.fetch

    connector_id = env.connector()
    key = env.issue()
    names = [f"Q{n}" for n in range(4)]
    env.linked(key, connector_id, *names)
    for name in names:
        env.script[name] = body(name)
    env.script.delay = 0.02

    def factory(row: registry.ConnectorRow) -> Sequential:
        return Sequential(env.script)

    report = run_sync(env.engine, SyncScope("issue", key), config=env.config, connector_factory=factory)

    assert report.status == "ok"
    assert env.script.max_active == 1
