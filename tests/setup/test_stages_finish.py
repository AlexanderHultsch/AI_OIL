from collections.abc import Callable
from pathlib import Path

import pytest

from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import projects
from ai_oil.governance import audit
from ai_oil.setup.model import SetupContext
from ai_oil.setup.stages_finish import FinishStage
from ai_oil.sources import registry
from ai_oil.sources.health import CheckLine

# --- helpers ---------------------------------------------------------------------------------------


def _engine(home: Path):
    return create_db_engine(load_config(home).db_path)


def _add_connector(
    home: Path, type_name: str, name: str, *, credential_ref: str | None = None, enabled: bool = True
) -> None:
    engine = _engine(home)
    try:
        with engine.begin() as conn:
            registry.create_connector(conn, type_name, name, credential_ref=credential_ref)
            if not enabled:
                registry.set_enabled(conn, name, False)
    finally:
        engine.dispose()


def _add_project(home: Path, key: str, name: str) -> None:
    engine = _engine(home)
    try:
        with engine.begin() as conn:
            projects.create_project(conn, key, name)
    finally:
        engine.dispose()


def _projects(home: Path) -> list[tuple[str, str]]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            return [(p.key, p.name) for p in projects.list_projects(conn)]
    finally:
        engine.dispose()


def _project_audit(home: Path) -> list[audit.AuditEntry]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            return audit.list_entries(conn, action_prefix="project.")
    finally:
        engine.dispose()


class LiveChecks:
    """A stand-in for `health.check_live` that records the connectors it was asked about."""

    def __init__(self, lines: list[CheckLine] | None = None) -> None:
        self.lines = lines if lines is not None else [CheckLine(True, "mailbox: reachable")]
        self.calls: list[object] = []

    def __call__(self, connector: object) -> list[CheckLine]:
        self.calls.append(connector)
        return list(self.lines)


def _run(ctx: SetupContext, live: LiveChecks | None = None):
    return FinishStage(check_live=live or LiveChecks()).run(ctx)


@pytest.fixture
def home_with_project(initialised_home: Path) -> Path:
    _add_project(initialised_home, "OIL", "Open issues")
    return initialised_home


# --- live check ------------------------------------------------------------------------------------


def test_all_checks_ok(make_ctx: Callable[..., SetupContext], home_with_project: Path) -> None:
    _add_connector(home_with_project, "outlook", "Mail")
    live = LiveChecks()
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx, live)

    assert result.status == "done"
    assert result.open_items == []
    assert len(live.calls) == 1
    output = ctx.console.output
    assert "Mail (outlook)" in output
    assert "  [ok] plugin loaded" in output
    assert "  [ok] mailbox: reachable" in output
    assert "[FAIL]" not in output


def test_a_failing_connector_becomes_an_open_item_and_the_stage_stays_done(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    _add_connector(home_with_project, "outlook", "Mail", credential_ref="ai_oil/Mail")  # no secret stored
    live = LiveChecks([CheckLine(False, "inbox: not reachable")])
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx, live)

    assert result.status == "done"
    assert result.open_items == [
        "Connector Mail: credential missing for ai_oil/Mail",
        "Connector Mail: inbox: not reachable",
    ]
    assert "  [FAIL] credential missing for ai_oil/Mail" in ctx.console.output
    assert "  [FAIL] inbox: not reachable" in ctx.console.output


def test_a_disabled_connector_is_not_checked(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    _add_connector(home_with_project, "outlook", "Mail", enabled=False)
    live = LiveChecks()
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx, live)

    assert result.open_items == []
    assert live.calls == []
    assert "Mail (outlook)\n  [--] disabled" in ctx.console.output


def test_no_connectors_gives_a_line_and_an_open_item(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx)

    assert result.status == "done"
    assert "No connectors configured" in result.lines
    assert result.open_items == [
        "Connectors: run `oil setup` again to add Outlook, Jira, Confluence, SharePoint or Teams"
    ]


# --- first project ---------------------------------------------------------------------------------


def test_the_first_project_is_created_and_audited(
    make_ctx: Callable[..., SetupContext], initialised_home: Path
) -> None:
    ctx = make_ctx(initialised_home, ["", "", "n"])

    result = _run(ctx)

    assert "Project: OIL — Open issues" in result.lines
    assert "Issues live in projects" in ctx.console.output
    assert _projects(initialised_home) == [("OIL", "Open issues")]
    [entry] = _project_audit(initialised_home)
    assert (entry.action, entry.target) == ("project.create", "OIL")
    assert entry.detail == {"name": "Open issues", "description": None}


def test_a_key_is_upper_cased_and_the_name_taken_as_typed(
    make_ctx: Callable[..., SetupContext], initialised_home: Path
) -> None:
    ctx = make_ctx(initialised_home, ["mb32", "Dual DCDC", "n"])

    result = _run(ctx)

    assert "Project: MB32 — Dual DCDC" in result.lines
    assert _projects(initialised_home) == [("MB32", "Dual DCDC")]


def test_an_invalid_key_is_asked_again_and_nothing_is_created_meanwhile(
    make_ctx: Callable[..., SetupContext], initialised_home: Path
) -> None:
    ctx = make_ctx(initialised_home, ["1", "x", "my key", "ABC", "Alphabet", "n"])

    result = _run(ctx)

    assert ctx.console.output.count("Invalid project key") == 3
    assert "Project: ABC — Alphabet" in result.lines
    assert _projects(initialised_home) == [("ABC", "Alphabet")]


def test_existing_projects_are_listed_without_a_question(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    _add_project(home_with_project, "ABC", "Alphabet")
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx)

    assert "Projects: ABC, OIL" in result.lines
    assert ctx.console.prompts == ["Open AI_OIL in the browser now? [Y/n]: "]
    assert len(_project_audit(home_with_project)) == 0


def test_an_archived_project_counts_as_existing(
    make_ctx: Callable[..., SetupContext], initialised_home: Path
) -> None:
    _add_project(initialised_home, "OLD", "Retired")
    engine = _engine(initialised_home)
    try:
        with engine.begin() as conn:
            projects.update_project(conn, "OLD", archived=True)
    finally:
        engine.dispose()
    ctx = make_ctx(initialised_home, ["n"])

    result = _run(ctx)

    assert "Projects: OLD" in result.lines
    assert ctx.console.prompts == ["Open AI_OIL in the browser now? [Y/n]: "]
    assert len(_project_audit(initialised_home)) == 0


# --- start now -------------------------------------------------------------------------------------


def test_start_now_is_recorded_when_confirmed(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    ctx = make_ctx(home_with_project, [""])

    result = _run(ctx)

    assert ctx.state["start_now"] is True
    assert result.lines[-1] == ("Next time: double-click start.bat in the AI_OIL folder (or run `oil serve`)")


def test_start_now_is_not_set_when_declined(
    make_ctx: Callable[..., SetupContext], home_with_project: Path
) -> None:
    ctx = make_ctx(home_with_project, ["n"])

    result = _run(ctx)

    assert "start_now" not in ctx.state
    assert result.lines[-1].startswith("Next time: double-click start.bat")
