"""The last wizard stage: live check of every connector, the first project, start now
(installer concept, section 5, stage 8).

The live check is read-only towards Outlook, Jira, Confluence and SharePoint. A failed check never fails
the stage; it becomes an open item.
"""

from collections.abc import Callable

from ai_oil.domain import projects
from ai_oil.domain.projects import _KEY_PATTERN
from ai_oil.governance import audit
from ai_oil.setup.model import SetupContext, StageResult
from ai_oil.sources import health, registry
from ai_oil.sources.health import CheckLine

_DEFAULT_PROJECT_KEY = "OIL"
_DEFAULT_PROJECT_NAME = "Open issues"
_NEXT_TIME = "Next time: double-click start.bat in the AI_OIL folder (or run `oil serve`)"


def _validate_key(text: str) -> str | None:
    if _KEY_PATTERN.fullmatch(text.strip().upper()):
        return None
    return (
        f"Invalid project key {text.strip()!r}: use 2-10 characters, letters and digits,"
        " starting with a letter."
    )


class FinishStage:
    name = "finish"
    title = "Finish"

    def __init__(self, *, check_live: Callable[[object], list[CheckLine]] = health.check_live) -> None:
        self._check_live = check_live

    def run(self, ctx: SetupContext) -> StageResult:
        open_items: list[str] = []
        lines = self._check_connectors(ctx, open_items)
        lines.append(self._first_project(ctx))
        if ctx.console.confirm("Open AI_OIL in the browser now?", default=True):
            ctx.state["start_now"] = True
        lines.append(_NEXT_TIME)
        return StageResult("done", lines, open_items)

    def _check_connectors(self, ctx: SetupContext, open_items: list[str]) -> list[str]:
        console = ctx.console
        with ctx.engine.connect() as conn:
            rows = registry.list_connectors(conn)
        if not rows:
            open_items.append(
                "Connectors: run `oil setup` again to add Outlook, Jira, Confluence or SharePoint"
            )
            return ["No connectors configured"]
        for row in rows:
            console.say(f"{row.name} ({row.type})")
            if not row.enabled:
                console.say("  [--] disabled")
                continue
            connector, checks = health.check_configuration(row)
            if connector is not None:
                checks += self._check_live(connector)
            for check in checks:
                console.say(check.format())
                if not check.ok:
                    open_items.append(f"Connector {row.name}: {check.text}")
        return [f"Connectors checked: {len(rows)}"]

    def _first_project(self, ctx: SetupContext) -> str:
        console = ctx.console
        with ctx.engine.connect() as conn:
            existing = projects.list_projects(conn, include_archived=True)
        if existing:
            return f"Projects: {', '.join(project.key for project in existing)}"
        console.say("Issues live in projects. Create the first one now; you can add more later.")
        key = console.ask("Project key", default=_DEFAULT_PROJECT_KEY, validate=_validate_key).strip().upper()
        name = console.ask("Project name", default=_DEFAULT_PROJECT_NAME).strip()
        with ctx.engine.begin() as conn:
            project = projects.create_project(conn, key, name)
            audit.log(
                conn,
                "project.create",
                "B",
                actor_type="user",
                target=project.key,
                detail={"name": project.name, "description": None},
            )
        return f"Project: {project.key} — {project.name}"
