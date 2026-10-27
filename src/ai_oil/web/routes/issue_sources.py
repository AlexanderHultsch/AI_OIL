"""Sources panel of the issue page: refresh, unlink and the link dialog (UI-21, UI-22, SYNC-1, SYNC-9)."""

from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from ai_oil.config import Config
from ai_oil.domain import issues, people
from ai_oil.errors import NotFoundError, ValidationError
from ai_oil.governance import audit
from ai_oil.sources import link_service, linking, store
from ai_oil.sources.linking import LinkedSource
from ai_oil.sources.sync import SyncReport, SyncScope, format_report, run_sync
from ai_oil.web.deps import db_write, get_config, get_engine
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _local_time
from ai_oil.web.routes.sync import BACKUP_FAILED, BACKUP_WRITTEN, auto_backup

router = APIRouter()

VIA = "web"
TYPE_HEADINGS = (
    ("email_thread", "Email"),
    ("jira_issue", "Jira"),
    ("confluence_page", "Confluence"),
    ("file", "SharePoint / OneDrive"),
    ("note", "Notes"),
    ("web_link", "Web links"),
)
OTHER_HEADING = "Other"
STATUS_ICONS = {
    "ok": ("✓", None),
    "error": ("⚠", None),
    "gone": ("✕", "gone in the source system"),
    "pending": ("…", None),
    "skipped": ("–", "connector disabled"),
}  # UI-21: the icon of a fetch status and its fixed tooltip (an error shows its own message)
LINK_MODES = ("reference", "outlook-selected", "outlook-subject")
DEFAULT_ROLE = "reference"
NEVER = "never"


@dataclass(frozen=True)
class SourceRow:
    """One active link of the issue as the panel shows it."""

    id: int
    label: str
    role: str
    connector: str
    status: str
    icon: str
    icon_title: str | None
    checked: str
    changed: str
    versions: int
    entries: int
    open_url: str | None
    path: str | None


@dataclass(frozen=True)
class SourceGroup:
    heading: str
    rows: tuple[SourceRow, ...]


@dataclass(frozen=True)
class Preview:
    """What a connector reports for a reference, with the form fields that link it (UI-22)."""

    connector: str
    connector_type: str
    object_type: str
    title: str | None
    display_ref: str | None
    locator: str | None
    fields: dict[str, str]


def _row(link: LinkedSource) -> SourceRow:
    icon, icon_title = STATUS_ICONS.get(link.fetch_status, ("?", link.fetch_status))
    if link.fetch_status == "error":
        icon_title = link.fetch_error
    locator = link.locator or ""
    is_url = locator.startswith(("http://", "https://"))
    return SourceRow(
        id=link.source_object_id,
        label=link.display_ref or link.title or link.external_id,
        role=link.role,
        connector=link.connector_name,
        status=link.fetch_status,
        icon=icon,
        icon_title=icon_title,
        checked=_local_time(link.last_checked_at) if link.last_checked_at else NEVER,
        changed=_local_time(link.last_changed_at) if link.last_changed_at else NEVER,
        versions=link.version_count,
        entries=link.entry_count,
        open_url=locator if is_url else None,
        path=locator if locator and not is_url and link.object_type == "file" else None,
    )


def _groups(links: list[LinkedSource]) -> list[SourceGroup]:
    """The links grouped by object type in the fixed order; empty groups are left out (UI-21)."""
    known = {object_type for object_type, _ in TYPE_HEADINGS}
    groups = []
    for object_type, heading in TYPE_HEADINGS:
        rows = tuple(_row(link) for link in links if link.object_type == object_type)
        if rows:
            groups.append(SourceGroup(heading, rows))
    others = tuple(_row(link) for link in links if link.object_type not in known)
    if others:
        groups.append(SourceGroup(OTHER_HEADING, others))
    return groups


def _summary(report: SyncReport | None, backup_line: str | None = None) -> dict[str, object] | None:
    if report is None:
        return None
    first, *rest = format_report(report).splitlines()
    return {
        "status": report.status,
        "head": first,
        "lines": [line.strip() for line in rest],
        "backup": backup_line,
    }


def _backup_line(engine: sqlalchemy.Engine, config: Config) -> str | None:
    """The automatic backup after a sync (NFR-8) as one line for the summary; None if none was due."""
    written, failed = auto_backup(engine, config)
    if failed is not None:
        return f"{BACKUP_FAILED}{failed}"
    return f"{BACKUP_WRITTEN}{written}" if written is not None else None


def sources_context(
    conn: sqlalchemy.Connection,
    issue: issues.Issue,
    report: SyncReport | None = None,
    backup_line: str | None = None,
) -> dict[str, object]:
    """The context of `issue/_sources.html`; a sync report is shown as a summary above the list."""
    return {
        "issue": issue,
        "source_groups": _groups(linking.list_issue_sources(conn, issue.key)),
        "roles": linking.ROLES,
        "default_role": DEFAULT_ROLE,
        "sync_summary": _summary(report, backup_line),
    }


def _panel(
    request: Request,
    engine: sqlalchemy.Engine,
    key: str,
    report: SyncReport | None = None,
    backup_line: str | None = None,
) -> Response:
    with engine.begin() as conn:
        return render(
            request,
            "issue/_sources.html",
            sources_context(conn, issues.get_issue(conn, key), report, backup_line),
        )


def _audit(conn: sqlalchemy.Connection, action: str, key: str, detail: dict[str, object]) -> None:
    audit.log(conn, action, "B", actor_type="user", target=key, detail={**detail, "via": VIA})


def _issue(engine: sqlalchemy.Engine, key: str, source_id: int | None = None) -> issues.Issue:
    """The issue, which must exist and, with `source_id`, have an active link to that source."""
    with engine.begin() as conn:
        issue = issues.get_issue(conn, key)
        if source_id is not None and source_id not in linking.linked_object_ids(conn, issue.key):
            raise NotFoundError(f"{issue.key} has no active link to source object {source_id}")
        return issue


@router.post("/issues/{key}/sources/refresh")
def refresh_all(
    request: Request,
    key: str,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Sync every active link of the issue; the fetch runs outside any request transaction (SYNC-1)."""
    issue = _issue(engine, key)
    report = run_sync(engine, SyncScope("issue", issue.key), config=config, trigger="ui")
    return _panel(request, engine, issue.key, report, _backup_line(engine, config))


@router.post("/issues/{key}/sources/{source_id}/refresh")
def refresh_one(
    request: Request,
    key: str,
    source_id: int,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    """Sync one linked source (SYNC-1)."""
    issue = _issue(engine, key, source_id)
    report = run_sync(engine, SyncScope("source", source_id), config=config, trigger="ui")
    return _panel(request, engine, issue.key, report, _backup_line(engine, config))


@router.post("/issues/{key}/sources/{source_id}/unlink")
def unlink(
    request: Request,
    key: str,
    source_id: int,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
) -> Response:
    """Remove the source from the issue's dictionary; the object and its content stay (DM-57)."""
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    linking.unlink_source(conn, issue.key, source_id, actor_id=me.id)
    label = linking.source_label(store.get_object(conn, source_id))
    _audit(conn, "source.unlink", issue.key, {"source_object_id": source_id, "label": label})
    return render(request, "issue/_sources.html", sources_context(conn, issue))


def _note(note_text: str) -> str | None:
    return note_text.strip() or None


def _role(role: str) -> str:
    return role.strip() or DEFAULT_ROLE


def _pick(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise ValidationError("pick the number of a conversation from the list") from None


def _selection(
    conn: sqlalchemy.Connection, mode: str, reference: str, subject: str, pick: str
) -> link_service.Selection:
    """The selection a link request stands for; the request is stateless, so it is made again (UI-22)."""
    if mode == "reference":
        if not reference.strip():
            raise ValidationError("enter a URL, key or path")
        return link_service.select_reference(conn, reference)
    if mode == "outlook-selected":
        return link_service.select_outlook_selected(conn)
    if mode == "outlook-subject":
        if not subject.strip():
            raise ValidationError("enter a subject to search for")
        row, connector, candidates = link_service.outlook_candidates(conn, subject)
        number = _pick(pick)
        if not 1 <= number <= len(candidates):
            raise ValidationError(f"pick {number} is out of range: {len(candidates)} conversations match")
        return link_service.Selection(row, connector, candidates[number - 1].ref)
    raise ValidationError(f"mode must be one of {', '.join(LINK_MODES)}")


def _preview_response(
    request: Request,
    engine: sqlalchemy.Engine,
    key: str,
    *,
    mode: str,
    reference: str,
    role: str,
    note_text: str,
) -> Response:
    """Select by `mode` and show what the connector reports, without storing anything (UI-22)."""
    issue = _issue(engine, key)
    with engine.begin() as conn:
        selection = _selection(conn, mode, reference, "", "")
    meta = link_service.preview(selection)
    fields = {"mode": mode, "role": _role(role), "note_text": note_text}
    if mode == "reference":
        fields["reference"] = reference
    shown = Preview(
        connector=selection.row.name,
        connector_type=selection.row.type,
        object_type=meta.object_type,
        title=meta.title,
        display_ref=meta.display_ref,
        locator=meta.locator,
        fields=fields,
    )
    return render(request, "issue/_source_preview.html", {"issue": issue, "preview": shown})


@router.post("/issues/{key}/sources/preview")
def preview_reference(
    request: Request,
    key: str,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    reference: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = DEFAULT_ROLE,
    note_text: Annotated[str, Form()] = "",
) -> Response:
    return _preview_response(
        request, engine, key, mode="reference", reference=reference, role=role, note_text=note_text
    )


@router.post("/issues/{key}/sources/preview-outlook")
def preview_outlook(
    request: Request,
    key: str,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    role: Annotated[str, Form()] = DEFAULT_ROLE,
    note_text: Annotated[str, Form()] = "",
) -> Response:
    """Preview the conversation of the email selected in Outlook (CON-21 a)."""
    return _preview_response(
        request, engine, key, mode="outlook-selected", reference="", role=role, note_text=note_text
    )


@router.post("/issues/{key}/sources/outlook-search")
def outlook_search(
    request: Request,
    key: str,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    subject: Annotated[str, Form()] = "",
) -> Response:
    """List the Outlook conversations whose subject matches; each can be linked (CON-21 b)."""
    issue = _issue(engine, key)
    if not subject.strip():
        raise ValidationError("enter a subject to search for")
    with engine.begin() as conn:
        _, _, candidates = link_service.outlook_candidates(conn, subject)
    lines = [link_service.describe_candidate(number, c).strip() for number, c in enumerate(candidates, 1)]
    return render(
        request,
        "issue/_source_candidates.html",
        {"issue": issue, "subject": subject, "candidates": list(enumerate(lines, start=1))},
    )


@router.post("/issues/{key}/sources/link")
def link(
    request: Request,
    key: str,
    engine: Annotated[sqlalchemy.Engine, Depends(get_engine)],
    config: Annotated[Config, Depends(get_config)],
    mode: Annotated[str, Form()] = "reference",
    reference: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    pick: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = DEFAULT_ROLE,
    note_text: Annotated[str, Form()] = "",
) -> Response:
    """Link the selected source and fetch it at once (UI-22, SYNC-9)."""
    issue = _issue(engine, key)
    with engine.begin() as conn:
        me = people.get_me(conn)
        selection = _selection(conn, mode.strip() or "reference", reference, subject, pick)
    _, _, report = link_service.link_selection(
        engine,
        config,
        issue.key,
        selection,
        role=_role(role),
        note=_note(note_text),
        actor_id=me.id,
        via=VIA,
    )
    return _panel(request, engine, issue.key, report)


@router.post("/issues/{key}/sources/note")
def new_note(
    request: Request,
    key: str,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    title: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
) -> Response:
    """Create a note and link it to the issue (CON-10)."""
    me = people.get_me(conn)
    issue = issues.get_issue(conn, key)
    link_service.link_note(
        conn, issue.key, title, text, role=DEFAULT_ROLE, note=None, actor_id=me.id, via=VIA
    )
    return render(request, "issue/_sources.html", sources_context(conn, issue))
