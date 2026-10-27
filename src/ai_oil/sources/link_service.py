"""Choosing a connector for a reference and linking the source to an issue; shared by the CLI and the web UI (UI-22, CON-3, SYNC-9)."""  # noqa: E501

from dataclasses import dataclass

import sqlalchemy

from ai_oil.config import Config
from ai_oil.connectors import manual, outlook
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.linking import LinkedSource
from ai_oil.sources.model import ObjectMeta, SourceRef
from ai_oil.sources.sync import SyncReport, SyncScope, run_sync

Instances = list[tuple[registry.ConnectorRow, object]]
Skipped = list[tuple[str, str]]


@dataclass(frozen=True)
class Selection:
    """The connector that recognised a reference, with the reference as it parsed it."""

    row: registry.ConnectorRow
    connector: object
    ref: SourceRef


def _instances(conn: sqlalchemy.Connection, connector: str | None) -> tuple[Instances, Skipped]:
    """Enabled connectors that start; one that cannot start is skipped and reported (CON-3)."""
    manual.ensure_manual_connector(conn)
    only = registry.get_connector(conn, connector).id if connector is not None else None
    instances: Instances = []
    skipped: Skipped = []
    for candidate in registry.list_connectors(conn):
        if not candidate.enabled or (only is not None and candidate.id != only):
            continue
        try:
            instances.append((candidate, registry.instantiate(candidate)))
        except Exception as exc:
            skipped.append((candidate.name, f"{type(exc).__name__}: {exc}"))
    if only is not None and skipped:
        raise ValidationError(f"connector {skipped[0][0]} is not usable: {skipped[0][1]}")
    return instances, skipped


def _choose(
    matches: list[tuple[registry.ConnectorRow, SourceRef]],
    reference: str,
    skipped: Skipped | None = None,
) -> tuple[registry.ConnectorRow, SourceRef]:
    """Matches of real connectors win over the manual connector's; several of them are ambiguous."""
    specific = [match for match in matches if match[0].type != manual.ManualConnector.type]
    if len(specific) > 1:
        names = ", ".join(row.name for row, _ in specific)
        raise ValidationError(
            f"several connectors recognise '{reference}' ({names}); choose one with --connector"
        )
    if specific:
        return specific[0]
    if matches:
        return matches[0]
    note = f" (skipped: {'; '.join(f'{name}: {error}' for name, error in skipped)})" if skipped else ""
    raise ValidationError(f"no connector recognises '{reference}'{note}")


def _select_among(instances: Instances, skipped: Skipped, reference: str) -> Selection:
    matches = registry.match_reference(instances, reference)
    row, ref = _choose(matches, reference, skipped)
    connector = next(instance for candidate, instance in instances if candidate.id == row.id)
    return Selection(row, connector, ref)


def select_reference(
    conn: sqlalchemy.Connection, reference: str, *, connector: str | None = None
) -> Selection:
    """The connector that recognises `reference` (URL, key or path); every enabled one is asked (UI-22)."""
    instances, skipped = _instances(conn, connector)
    return _select_among(instances, skipped, reference)


def _outlook_among(instances: Instances, connector: str | None) -> tuple[registry.ConnectorRow, object]:
    found = [(row, instance) for row, instance in instances if row.type == outlook.OutlookConnector.type]
    if connector is not None and instances and not found:
        raise ValidationError(f"connector {connector} is not an Outlook connector")
    if not found:
        raise ValidationError("no enabled Outlook connector — add one with `oil connector add outlook`")
    if len(found) > 1:
        names = ", ".join(row.name for row, _ in found)
        raise ValidationError(
            f"several Outlook connectors are enabled ({names}); choose one with --connector"
        )
    return found[0]


def outlook_instance(
    conn: sqlalchemy.Connection, connector: str | None = None
) -> tuple[registry.ConnectorRow, object]:
    """The one enabled Outlook connector (CON-21)."""
    instances, _ = _instances(conn, connector)
    return _outlook_among(instances, connector)


def select_outlook_selected(conn: sqlalchemy.Connection, *, connector: str | None = None) -> Selection:
    """The conversation of the email currently selected in Outlook (CON-21 a)."""
    instances, skipped = _instances(conn, connector)
    chosen = _outlook_among(instances, connector)
    return _select_among([chosen], skipped, outlook.SELECTED_REFERENCE)


def outlook_candidates(
    conn: sqlalchemy.Connection, subject: str, *, connector: str | None = None
) -> tuple[registry.ConnectorRow, object, list[outlook.ConversationCandidate]]:
    """The conversations whose subject matches, newest first; none is an error (CON-21 b)."""
    row, instance = outlook_instance(conn, connector)
    candidates = instance.find_conversations(subject)  # type: ignore[attr-defined]
    if not candidates:
        raise ValidationError(f"no email conversation matches '{subject}'")
    return row, instance, candidates


def describe_candidate(number: int, candidate: outlook.ConversationCandidate) -> str:
    latest = candidate.latest.astimezone().strftime("%Y-%m-%d %H:%M") if candidate.latest else "no date"
    return (
        f"  {number}. {candidate.subject} — {latest} — {candidate.sender}"
        f" ({candidate.message_count} messages)"
    )


def preview(selection: Selection) -> ObjectMeta:
    """What the connector knows about the referenced object, without storing anything (UI-22)."""
    return selection.connector.resolve(selection.ref)  # type: ignore[attr-defined]


def link_selection(
    engine: sqlalchemy.Engine,
    config: Config,
    key: str,
    selection: Selection,
    *,
    role: str,
    note: str | None,
    actor_id: int,
    via: str | None = None,
) -> tuple[LinkedSource, str, SyncReport]:
    """Link the selected object to an issue, then fetch it at once (SYNC-9)."""
    with engine.begin() as conn:
        meta = preview(selection)
        object_id = store.upsert_object(conn, selection.row.id, meta)
        linked = linking.link_source(conn, key, object_id, role=role, note=note, actor_id=actor_id)
        label = linking.source_label(store.get_object(conn, object_id))
        detail: dict[str, object] = {"source_object_id": object_id, "label": label, "role": role}
        if via is not None:
            detail["via"] = via
        audit.log(conn, "source.link", "B", actor_type="user", target=linked.issue_key, detail=detail)
    report = run_sync(
        engine,
        SyncScope("source", object_id),
        config=config,
        trigger="cli" if via is None else "ui",
    )
    return linked, label, report


def link_note(
    conn: sqlalchemy.Connection,
    key: str,
    title: str,
    text: str,
    *,
    role: str,
    note: str | None,
    actor_id: int,
    via: str | None = None,
) -> tuple[LinkedSource, str]:
    """Create a note and link it to an issue; a note has nothing to fetch (CON-10)."""
    object_id = manual.create_note(conn, title, text)
    linked = linking.link_source(conn, key, object_id, role=role, note=note, actor_id=actor_id)
    label = f"note: {title.strip()}"
    detail: dict[str, object] = {"source_object_id": object_id, "label": label, "role": role}
    if via is not None:
        detail["via"] = via
    audit.log(conn, "source.link", "B", actor_type="user", target=linked.issue_key, detail=detail)
    return linked, label
