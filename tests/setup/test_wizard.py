import argparse
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_oil.errors import ConfigError, ValidationError
from ai_oil.setup import wizard
from ai_oil.setup.console import SkipStage
from ai_oil.setup.model import SetupContext, StageResult


class FakeStage:
    def __init__(
        self, name: str, title: str, action: Callable[[SetupContext], StageResult], log: list
    ) -> None:
        self.name = name
        self.title = title
        self._action = action
        self._log = log

    def run(self, ctx: SetupContext) -> StageResult:
        self._log.append(self.name)
        return self._action(ctx)


def _done(lines: list[str] | None = None, open_items: list[str] | None = None):
    return lambda ctx: StageResult("done", lines or [], open_items or [])


def _raise(exc: BaseException):
    def action(ctx: SetupContext) -> StageResult:
        raise exc

    return action


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch):
    """Replace the wizard's stages; returns (log of stage names run, installer)."""
    log: list[str] = []

    def install(*specs: tuple[str, str, Callable[[SetupContext], StageResult]]) -> None:
        monkeypatch.setattr(wizard, "STAGES", [FakeStage(n, t, a, log) for n, t, a in specs])

    return log, install


@pytest.fixture
def ctx(make_ctx, fresh_home: Path) -> SetupContext:
    return make_ctx(fresh_home)


def test_the_real_stages_in_their_order() -> None:
    assert [(s.name, s.title) for s in wizard.STAGES] == [
        ("data", "Data folder"),
        ("me", "About you"),
        ("backup", "Backups"),
        ("outlook", "Outlook"),
        ("network", "Network"),
        ("atlassian", "Jira and Confluence"),
        ("sharepoint", "SharePoint and OneDrive files"),
        ("finish", "Finish"),
    ]


def _set_up_home(ctx: SetupContext) -> None:
    ctx.home.mkdir(parents=True)
    (ctx.home / "config.toml").write_text("", encoding="utf-8")


def _menu_stages(install) -> None:
    install(
        ("data", "Data folder", _done()),
        ("me", "About you", _done()),
        ("backup", "Backups", _done()),
        ("finish", "Finish", _done()),
    )


def test_runs_every_stage_in_order_and_prints_welcome_stage_lines_and_summary(stages, ctx) -> None:
    log, install = stages
    install(
        ("data", "Data folder", _done(["Data folder: X"])),
        ("me", "About you", _done(["Me: Alex"])),
    )

    assert wizard.run_wizard(ctx) == 0

    assert log == ["data", "me"]
    output = ctx.console.output
    assert "Type s to skip a stage" in output
    assert "Ctrl+C" in output
    assert (
        output.index("1/2  Data folder") < output.index("  Data folder: X") < output.index("2/2  About you")
    )
    assert "  Data folder  done" in output
    assert "  About you    done" in output
    assert "Nothing is open." in output


def test_only_runs_just_that_stage(stages, ctx) -> None:
    log, install = stages
    install(("data", "Data folder", _done()), ("me", "About you", _done()))

    assert wizard.run_wizard(ctx, only="me") == 0

    assert log == ["me"]
    assert "1/1  About you" in ctx.console.output


def test_unknown_stage_lists_the_names_and_runs_nothing(stages, ctx) -> None:
    log, install = stages
    install(("data", "Data folder", _done()), ("me", "About you", _done()))

    with pytest.raises(ValidationError, match="unknown stage 'nope'; stages: data, me"):
        wizard.run_wizard(ctx, only="nope")
    assert log == []
    assert ctx.console.output == ""


def test_a_skipped_stage_becomes_an_open_item_and_the_run_goes_on(stages, ctx) -> None:
    log, install = stages
    install(
        ("data", "Data folder", _done()),
        ("me", "About you", _raise(SkipStage())),
        ("backup", "Backups", _done()),
    )

    assert wizard.run_wizard(ctx) == 0

    assert log == ["data", "me", "backup"]
    output = ctx.console.output
    assert "  About you    skipped" in output
    assert "Still open:" in output
    assert "  - About you: skipped — run `oil setup --only me` later" in output


def test_a_failed_stage_is_reported_and_the_exit_code_is_1(stages, ctx) -> None:
    log, install = stages
    install(
        ("data", "Data folder", _done()),
        ("me", "About you", _raise(ConfigError("cannot do it"))),
        ("backup", "Backups", _done()),
    )

    assert wizard.run_wizard(ctx) == 1

    assert log == ["data", "me", "backup"]
    output = ctx.console.output
    assert "  Failed: cannot do it" in output
    assert "  About you    failed" in output
    assert "  - About you: failed" in output
    assert "oil setup --only me" in output


def test_a_failed_data_stage_stops_the_later_stages(stages, ctx) -> None:
    log, install = stages
    install(
        ("data", "Data folder", _raise(ConfigError("read-only"))),
        ("me", "About you", _done()),
        ("backup", "Backups", _done()),
    )

    assert wizard.run_wizard(ctx) == 1

    assert log == ["data"]
    output = ctx.console.output
    assert "  About you    not run" in output
    assert "  Backups      not run" in output
    assert "  - About you: not run because the data folder failed" in output
    assert "  - Backups: not run because the data folder failed" in output


def test_a_skipped_data_stage_does_not_stop_the_run(stages, ctx) -> None:
    log, install = stages
    install(("data", "Data folder", _raise(SkipStage())), ("me", "About you", _done()))
    assert wizard.run_wizard(ctx) == 0
    assert log == ["data", "me"]


def test_open_items_of_done_stages_are_listed(stages, ctx) -> None:
    _, install = stages
    install(("data", "Data folder", _done(open_items=["Set AI_OIL_HOME"])))

    assert wizard.run_wizard(ctx) == 0

    assert "  - Set AI_OIL_HOME" in ctx.console.output


def test_keyboard_interrupt_propagates(stages, ctx) -> None:
    _, install = stages
    install(("data", "Data folder", _raise(KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        wizard.run_wizard(ctx)


def test_the_context_creates_config_and_engine_lazily_and_closes_them(
    make_ctx, initialised_home: Path
) -> None:
    ctx = make_ctx(initialised_home)
    assert ctx._config is None and ctx._engine is None

    assert ctx.config.home == initialised_home
    assert ctx.config is ctx.config
    engine = ctx.engine
    assert engine is ctx.engine

    ctx.close()
    assert ctx._engine is None
    ctx.close()


def test_the_context_follows_a_switched_home(make_ctx, initialised_home: Path, tmp_path: Path) -> None:
    ctx = make_ctx(initialised_home)
    assert ctx.engine is not None
    assert ctx.config.home == initialised_home

    ctx.use_home(tmp_path / "other")

    assert ctx.home == tmp_path / "other"
    assert ctx._engine is None
    with pytest.raises(ConfigError, match="oil init"):
        _ = ctx.config


def test_args_are_kept(make_ctx, fresh_home: Path) -> None:
    ctx = make_ctx(fresh_home)
    assert isinstance(ctx.args, argparse.Namespace)


# --- re-run menu -----------------------------------------------------------------------------------


def test_a_fresh_folder_shows_no_menu(stages, ctx) -> None:
    log, install = stages
    _menu_stages(install)

    assert wizard.run_wizard(ctx) == 0

    assert log == ["data", "me", "backup", "finish"]
    assert "already set up" not in ctx.console.output
    assert ctx.console.prompts == []


def test_an_existing_setup_offers_the_menu_and_option_1_runs_all_stages(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home, ["1"])
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx) == 0

    assert log == ["data", "me", "backup", "finish"]
    output = ctx.console.output
    assert f"AI_OIL is already set up in {fresh_home}. What do you want to do?" in output
    assert "  1) Go through all stages again (your current values are offered)" in output
    assert "  2) Change one area" in output
    assert "  3) Only run the final check" in output
    assert output.index("Type s to skip a stage") < output.index("already set up")
    assert "1/4  Data folder" in output


def test_the_menu_default_is_to_go_through_all_stages(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home, [""])
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx) == 0

    assert log == ["data", "me", "backup", "finish"]


def test_option_2_runs_the_chosen_area_and_the_final_check(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home, ["2", "2"])
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx) == 0

    assert log == ["me", "finish"]
    output = ctx.console.output
    assert "Which area?" in output
    assert "  1) Data folder" in output
    assert "  3) Backups" in output
    assert "  Finish" not in output.split("Which area?")[1].split("Number")[0]
    assert "1/2  About you" in output
    assert "2/2  Finish" in output


def test_option_3_runs_only_the_final_check(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home, ["3"])
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx) == 0

    assert log == ["finish"]
    assert "1/1  Finish" in ctx.console.output


def test_only_never_shows_the_menu(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home)
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx, only="me") == 0

    assert log == ["me"]
    assert "already set up" not in ctx.console.output
    assert ctx.console.prompts == []


def test_skipping_at_the_menu_changes_nothing_and_exits_with_0(stages, make_ctx, fresh_home) -> None:
    log, install = stages
    _menu_stages(install)
    ctx = make_ctx(fresh_home, ["s"])
    _set_up_home(ctx)

    assert wizard.run_wizard(ctx) == 0

    assert log == []
    output = ctx.console.output
    assert "Summary" in output
    assert "Nothing changed" in output
