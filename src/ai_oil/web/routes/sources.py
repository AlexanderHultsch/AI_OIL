"""Source page: stored content, versions, entries and the side-by-side version diff (UI-24, UI-25)."""

import difflib
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from ai_oil.connectors import manual
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import activity, linking, registry, store
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import render
from ai_oil.web.routes.issues import _local_time

router = APIRouter()

VIA = "web"
MAX_TEXT_LINES = 400
DIFF_WRAP_COLUMN = 100
DIFF_CONTEXT_LINES = 3
SHORT_HASH = 12
NEVER = "never"
UNKNOWN_AUTHOR = "-"


@dataclass(frozen=True)
class SourceView:
    id: int
    object_type: str
    label: str
    title: str | None
    locator: str | None
    open_url: str | None
    status: str
    checked: str
    changed: str
    error: str | None


@dataclass(frozen=True)
class VersionRow:
    number: int
    fetched: str
    ref: str
    hash: str


@dataclass(frozen=True)
class LatestVersion:
    number: int
    fetched: str
    text: str


@dataclass(frozen=True)
class EntryRow:
    time: str
    entry_type: str
    direction: str | None
    author: str
    text: str


@dataclass(frozen=True)
class RelatedSource:
    id: int
    object_type: str
    label: str
    status: str


def _label(obj: store.SourceObject) -> str:
    return obj.display_ref or obj.title or obj.external_id


def _related(obj: store.SourceObject) -> RelatedSource:
    return RelatedSource(obj.id, obj.object_type, _label(obj), obj.fetch_status)


def _view(obj: store.SourceObject) -> SourceView:
    locator = obj.locator or None
    return SourceView(
        id=obj.id,
        object_type=obj.object_type,
        label=_label(obj),
        title=obj.title,
        locator=locator,
        open_url=locator if locator and locator.startswith(("http://", "https://")) else None,
        status=obj.fetch_status,
        checked=_local_time(obj.last_checked_at) if obj.last_checked_at else NEVER,
        changed=_local_time(obj.last_changed_at) if obj.last_changed_at else NEVER,
        error=obj.fetch_error,
    )


def _clipped(text: str) -> str:
    """The text cut after `MAX_TEXT_LINES` lines, with a note on how many were left out."""
    lines = text.splitlines()
    if len(lines) <= MAX_TEXT_LINES:
        return text
    return "\n".join([*lines[:MAX_TEXT_LINES], f"… ({len(lines) - MAX_TEXT_LINES} more lines)"])


def _version_by_number(
    versions: list[store.SourceVersion], source_id: int, value: str
) -> store.SourceVersion:
    try:
        number = int(value.strip())
    except ValueError:
        raise ValidationError(f"version numbers must be whole numbers, got {value!r}") from None
    for version in versions:
        if version.version_no == number:
            return version
    raise ValidationError(f"source object {source_id} has no version {number}")


@router.get("/sources/{source_id}")
def source_page(
    request: Request, source_id: int, conn: Annotated[sqlalchemy.Connection, Depends(db_read)]
) -> Response:
    """The same information as `oil source show`, with the entries in full (UI-24)."""
    obj = store.get_object(conn, source_id)
    connector = registry.get_connector(conn, obj.connector_id)
    versions = store.list_versions(conn, obj.id)
    entries = store.list_entries(conn, obj.id)
    children = store.list_children(conn, obj.id)
    identities = activity.resolve_identities(
        conn, [e.author_identity_id for e in entries if e.author_identity_id is not None]
    )
    latest = versions[-1] if versions else None
    context = {
        "source": _view(obj),
        "connector": connector,
        "parent": _related(store.get_object(conn, obj.parent_id)) if obj.parent_id is not None else None,
        "issue_keys": linking.issues_for_source(conn, obj.id),
        "versions": [
            VersionRow(
                v.version_no,
                _local_time(v.fetched_at),
                v.source_version_ref or "-",
                v.content_hash[:SHORT_HASH],
            )
            for v in versions
        ],
        "latest": (
            LatestVersion(latest.version_no, _local_time(latest.fetched_at), _clipped(latest.text))
            if latest is not None
            else None
        ),
        "note_text": latest.text if obj.object_type == "note" and latest is not None else "",
        "compare_a": versions[-2].version_no if len(versions) > 1 else None,
        "compare_b": latest.version_no if latest is not None else None,
        "entries": [
            EntryRow(
                time=_local_time(e.occurred_at) if e.occurred_at else NEVER,
                entry_type=e.entry_type,
                direction=e.direction,
                author=identities.get(e.author_identity_id, (UNKNOWN_AUTHOR, ""))[0],
                text=e.text,
            )
            for e in reversed(entries)
        ],
        "children": [_related(child) for child in children],
    }
    return render(request, "sources/detail.html", context)


@router.get("/sources/{source_id}/diff")
def source_diff(
    request: Request,
    source_id: int,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    a: str = "",
    b: str = "",
) -> Response:
    """Two stored versions side by side with a text diff (UI-25)."""
    obj = store.get_object(conn, source_id)
    versions = store.list_versions(conn, obj.id)
    first = _version_by_number(versions, obj.id, a)
    second = _version_by_number(versions, obj.id, b)
    first_lines, second_lines = first.text.splitlines(), second.text.splitlines()
    table = None
    if first_lines != second_lines:
        table = difflib.HtmlDiff(wrapcolumn=DIFF_WRAP_COLUMN).make_table(
            first_lines,
            second_lines,
            fromdesc=f"v{first.version_no}",
            todesc=f"v{second.version_no}",
            context=True,
            numlines=DIFF_CONTEXT_LINES,
        )
    context = {"source": _view(obj), "a": first.version_no, "b": second.version_no, "table": table}
    return render(request, "sources/diff.html", context)


@router.post("/sources/{source_id}/note")
def edit_note(
    source_id: int,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    title: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
) -> Response:
    """Store an edited note as a new version, like `oil note edit` (CON-10)."""
    before = store.get_object(conn, source_id).title
    version_id = manual.edit_note(conn, source_id, text.replace("\r\n", "\n"), title=title)
    obj = store.get_object(conn, source_id)
    changed = version_id is not None or obj.title != before
    audit.log(
        conn,
        "note.edit",
        "B",
        actor_type="user",
        target=linking.source_label(obj),
        detail={"source_object_id": obj.id, "version_id": version_id, "changed": changed, "via": VIA},
    )
    return RedirectResponse(f"/sources/{source_id}", status_code=303)
