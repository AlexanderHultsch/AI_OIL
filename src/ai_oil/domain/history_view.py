"""An issue's history as change sets with display values: names, local times, short text (DM-30, DM-33)."""

from dataclasses import dataclass

import sqlalchemy

from ai_oil import timeutil
from ai_oil.domain import history, people

PERSON_FIELDS = ("assignee", "reporter")  # history fields that hold person ids (DM-33)
MAX_TEXT = 200
NO_VALUE = "—"


@dataclass(frozen=True)
class HistoryLine:
    field: str
    op: str
    old: str
    new: str


@dataclass(frozen=True)
class HistoryGroup:
    """The events of one change set (DM-30) with who caused them and when."""

    change_set_id: int
    at: str
    time: str
    actor: str
    proposal: str | None
    events: tuple[HistoryLine, ...]


def display_value(field: str, value: str | None, names: dict[int, str]) -> str:
    """A raw stored value as shown: person names, local times, shortened text (DM-33)."""
    if value is None:
        return NO_VALUE
    if field in PERSON_FIELDS and value.isdigit():
        return names.get(int(value), value)
    if field.endswith("_at"):
        try:
            return timeutil.local_display(value)
        except ValueError:
            return value
    if len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def actor_label(entry: history.HistoryEntry, me: people.Person, names: dict[int, str]) -> str:
    if entry.actor_type != "user":
        return entry.actor_type
    if entry.actor_id is None:
        return "user"
    return "You" if entry.actor_id == me.id else names.get(entry.actor_id, "user")


def change_sets(
    conn: sqlalchemy.Connection, issue_id: int, me: people.Person, names: dict[int, str]
) -> list[HistoryGroup]:
    """The history of the issue as change sets, newest first (DM-30, DM-32, DM-33)."""
    grouped: dict[int, list[history.HistoryEntry]] = {}
    for entry in history.issue_history(conn, issue_id):
        grouped.setdefault(entry.change_set_id, []).append(entry)
    groups = []
    newest_first = sorted(grouped.items(), key=lambda item: (item[1][0].at, item[0]), reverse=True)
    for change_set_id, entries in newest_first:
        first = entries[0]
        groups.append(
            HistoryGroup(
                change_set_id=change_set_id,
                at=first.at,
                time=timeutil.local_display(first.at),
                actor=actor_label(first, me, names),
                proposal=(
                    f"(accepted AI proposal #{first.proposal_id})" if first.proposal_id is not None else None
                ),
                events=tuple(
                    HistoryLine(
                        field=entry.field,
                        op=entry.op,
                        old=display_value(entry.field, entry.old_value, names),
                        new=display_value(entry.field, entry.new_value, names),
                    )
                    for entry in entries
                ),
            )
        )
    return groups
