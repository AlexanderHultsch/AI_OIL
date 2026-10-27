"""The wizard: runs the stages in order, collects open items and prints the summary."""

from ai_oil.errors import AiOilError, ValidationError
from ai_oil.setup.console import SkipStage
from ai_oil.setup.model import SetupContext, Stage, StageResult
from ai_oil.setup.stages_basic import BackupStage, DataStage, MeStage
from ai_oil.setup.stages_connectors import (
    AtlassianStage,
    NetworkStage,
    OutlookStage,
    SharePointStage,
)
from ai_oil.setup.stages_finish import FinishStage

STAGES: list[Stage] = [
    DataStage(),
    MeStage(),
    BackupStage(),
    OutlookStage(),
    NetworkStage(),
    AtlassianStage(),
    SharePointStage(),
    FinishStage(),
]

_WELCOME = (
    "Welcome to the AI_OIL setup.",
    "",
    "I look at this computer, propose a value for every setting and ask you to confirm it.",
    "Press Enter to accept the value in [brackets]. Type s to skip a stage; the summary at the end",
    "lists what is still open. Press Ctrl+C at any time to stop — nothing is left half-written.",
    "You can run the setup again whenever you like; your current values are offered as defaults.",
)


def _skipped(stage: Stage) -> StageResult:
    return StageResult("skipped", [], [f"{stage.title}: skipped — run `oil setup --only {stage.name}` later"])


def _failed(stage: Stage, exc: AiOilError) -> StageResult:
    return StageResult(
        "failed",
        [f"Failed: {exc}"],
        [f"{stage.title}: failed — fix the problem above, then run `oil setup --only {stage.name}`"],
    )


def _rerun_stages(ctx: SetupContext) -> list[Stage]:
    """Ask what to do on a data folder that is already set up; the answer selects the stages to run."""
    console = ctx.console
    finish = [stage for stage in STAGES if stage.name == "finish"]
    areas = [stage for stage in STAGES if stage.name != "finish"]
    mode = console.choose(
        f"AI_OIL is already set up in {ctx.home}. What do you want to do?",
        [
            "Go through all stages again (your current values are offered)",
            "Change one area",
            "Only run the final check",
        ],
        default=0,
    )
    if mode == 0:
        return list(STAGES)
    if mode == 1:
        area = console.choose("Which area?", [stage.title for stage in areas], default=0)
        return [areas[area], *finish]
    return finish


def run_wizard(ctx: SetupContext, *, only: str | None = None) -> int:
    """Run all stages (or just `only`); returns 0 if no stage failed, else 1."""
    names = [stage.name for stage in STAGES]
    if only is not None and only not in names:
        raise ValidationError(f"unknown stage {only!r}; stages: {', '.join(names)}")
    selected = [stage for stage in STAGES if only in (None, stage.name)]

    console = ctx.console
    for line in _WELCOME:
        console.say(line)

    if only is None and (ctx.home / "config.toml").exists():
        try:
            selected = _rerun_stages(ctx)
        except SkipStage:
            console.heading("Summary")
            console.say("Nothing changed")
            return 0

    results: list[tuple[Stage, StageResult]] = []
    not_run: list[Stage] = []
    for index, stage in enumerate(selected):
        console.heading(f"{index + 1}/{len(selected)}  {stage.title}")
        try:
            result = stage.run(ctx)
        except SkipStage:
            result = _skipped(stage)
        except AiOilError as exc:
            result = _failed(stage, exc)
        for line in result.lines:
            console.say(f"  {line}")
        results.append((stage, result))
        if stage.name == "data" and result.status == "failed":
            not_run = selected[index + 1 :]
            break

    open_items = [item for _, result in results for item in result.open_items]
    open_items += [f"{stage.title}: not run because the data folder failed" for stage in not_run]

    console.heading("Summary")
    width = max(len(stage.title) for stage in selected)
    for stage, result in results:
        console.say(f"  {stage.title.ljust(width)}  {result.status}")
    for stage in not_run:
        console.say(f"  {stage.title.ljust(width)}  not run")
    console.say()
    if open_items:
        console.say("Still open:")
        for item in open_items:
            console.say(f"  - {item}")
    else:
        console.say("Nothing is open.")

    return 1 if any(result.status == "failed" for _, result in results) else 0
