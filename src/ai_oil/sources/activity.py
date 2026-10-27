"""One chronological view of an issue: comments, history, source entries and new versions (UI-23)."""

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy
from sqlalchemy import bindparam, text

from ai_oil import timeutil
from ai_oil.domain import comments, history_view, issues, people
from ai_oil.errors import ValidationError
from ai_oil.sources import linking

KINDS = ("comment", "history", "entry", "version")
DEFAULT_LIMIT = 200
UNKNOWN_AUTHOR = "unknown"
COMMENT_FIELD = "comment"  # history events of comments are left out: the comment itself is an item

_OBJECTS = text(
    "SELECT id, parent_id, object_type, external_id, title, display_ref FROM source_object"
    " WHERE id IN :ids OR parent_id IN :ids"
).bindparams(bindparam("ids", expanding=True))
_ENTRIES = text(
    "SELECT id, source_object_id, entry_type, direction, recipients_json, author_identity_id, occurred_at,"
    " text, new_text, metadata_json FROM source_entry"
    " WHERE source_object_id IN :ids AND revision = (SELECT MAX(other.revision) FROM source_entry other"
    " WHERE other.source_object_id = source_entry.source_object_id"
    " AND other.entry_type = source_entry.entry_type AND other.external_id = source_entry.external_id)"
    " ORDER BY occurred_at DESC, id DESC LIMIT :limit"
).bindparams(bindparam("ids", expanding=True))
_VERSIONS = text(
    "SELECT id, source_object_id, version_no, fetched_at FROM source_version"
    " WHERE source_object_id IN :ids AND version_no > 1 ORDER BY fetched_at DESC, id DESC LIMIT :limit"
).bindparams(bindparam("ids", expanding=True))
_IDENTITIES = text(
    "SELECT id, person_id, display_name, external_id FROM person_identity WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))


@dataclass(frozen=True)
class ActivityItem:
    """One event on an issue's timeline; `at` is the stored UTC timestamp."""

    kind: str  # "comment", "history", "entry" or "version"
    at: str
    title: str
    actor: str | None
    body: str | None
    source_object_id: int | None
    link: str | None
    meta: dict


@dataclass(frozen=True)
class _Object:
    id: int
    parent_id: int | None
    object_type: str
    label: str  # display_ref, else title, else external id (as `linking.source_label` without the type)
    title: str


def _instant(value: str) -> datetime:
    """The moment a stored timestamp stands for; unreadable ones sort as the oldest."""
    try:
        return timeutil.parse_iso(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _identifier(item: ActivityItem) -> int:
    """The id of the stored row behind an item, which makes the order of simultaneous items stable."""
    for key in ("comment_id", "change_set_id", "entry_id", "version_id"):
        if key in item.meta:
            return item.meta[key]
    return 0


def _objects(conn: sqlalchemy.Connection, linked: list[int]) -> dict[int, _Object]:
    """The actively linked objects together with their children (DM-44)."""
    found = {}
    for row in conn.execute(_OBJECTS, {"ids": linked}).mappings():
        label = row["display_ref"] or row["title"] or row["external_id"]
        found[row["id"]] = _Object(
            row["id"], row["parent_id"], row["object_type"], label, row["title"] or label
        )
    return found


def resolve_identities(
    conn: sqlalchemy.Connection, ids: Iterable[int], persons: dict[int, people.Person] | None = None
) -> dict[int, tuple[str, str]]:
    """Identity id to (display name of the surviving person, external id) (DM-23)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    if persons is None:
        persons = {person.id: person for person in people.list_persons(conn, include_merged=True)}
    found = {}
    for row in conn.execute(_IDENTITIES, {"ids": wanted}).mappings():
        person = persons.get(row["person_id"])
        seen = set()
        while person is not None and person.merged_into_id is not None and person.id not in seen:
            seen.add(person.id)
            person = persons.get(person.merged_into_id)
        name = person.display_name if person is not None else row["display_name"]
        found[row["id"]] = (name or UNKNOWN_AUTHOR, row["external_id"])
    return found


def _entry_title(entry_type: str, author: str, subject: str | None, owner: _Object, entry_text: str) -> str:
    if entry_type == "email_message":
        return f"Email from {author}: {subject}" if subject else f"Email from {author}"
    if entry_type == "jira_comment":
        return f"Jira comment by {author} on {owner.label}"
    if entry_type == "jira_change":
        first = entry_text.splitlines()[0] if entry_text else ""
        return f"Jira change on {owner.label}: {first}" if first else f"Jira change on {owner.label}"
    if entry_type == "confluence_comment":
        return f"Confluence comment by {author} on {owner.title}"
    if entry_type == "teams_message":
        return f"Teams message by {author}"
    return "Note"


def _addresses(metadata: dict, key: str) -> set[str]:
    value = metadata.get(key)
    return {str(address).casefold() for address in value} if isinstance(value, list) else set()


def _recipient_meta(
    entry_type: str, metadata: dict, recipients: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """Recipient display names; for email also the To and Cc split taken from the message addresses."""
    meta = {"recipients": [name for name, _ in recipients]}
    if entry_type == "email_message":
        for key in ("to", "cc"):
            addresses = _addresses(metadata, key)
            meta[key] = [name for name, address in recipients if address.casefold() in addresses]
    return meta


def _entry_items(
    conn: sqlalchemy.Connection,
    objects: dict[int, _Object],
    persons: dict[int, people.Person],
    limit: int,
) -> list[ActivityItem]:
    rows = list(conn.execute(_ENTRIES, {"ids": sorted(objects), "limit": limit}).mappings())
    recipient_lists = {
        row["id"]: json.loads(row["recipients_json"]) if row["recipients_json"] else [] for row in rows
    }
    identity_ids = [i for row in rows if (i := row["author_identity_id"]) is not None]
    identity_ids += [i for ids in recipient_lists.values() for i in ids]
    identities = resolve_identities(conn, identity_ids, persons)
    attachments: dict[int, list[str]] = {}
    for obj in sorted(objects.values(), key=lambda o: o.id):
        if obj.parent_id is not None:
            attachments.setdefault(obj.parent_id, []).append(obj.title)
    items = []
    for row in rows:
        owner = objects[row["source_object_id"]]
        metadata = json.loads(row["metadata_json"])
        author = identities.get(row["author_identity_id"], (UNKNOWN_AUTHOR, ""))[0]
        recipients = [identities[i] for i in recipient_lists[row["id"]] if i in identities]
        meta: dict = {
            "entry_id": row["id"],
            "entry_type": row["entry_type"],
            "direction": row["direction"],
            **_recipient_meta(row["entry_type"], metadata, recipients),
        }
        if row["entry_type"] == "email_message":
            meta["attachments"] = attachments.get(owner.id, [])
        items.append(
            ActivityItem(
                kind="entry",
                at=row["occurred_at"],
                title=_entry_title(row["entry_type"], author, metadata.get("subject"), owner, row["text"]),
                actor=author if row["author_identity_id"] is not None else None,
                body=row["new_text"] or row["text"],
                source_object_id=owner.id,
                link=f"/sources/{owner.id}",
                meta=meta,
            )
        )
    return items


def _version_items(
    conn: sqlalchemy.Connection, objects: dict[int, _Object], limit: int
) -> list[ActivityItem]:
    items = []
    for row in conn.execute(_VERSIONS, {"ids": sorted(objects), "limit": limit}).mappings():
        owner = objects[row["source_object_id"]]
        items.append(
            ActivityItem(
                kind="version",
                at=row["fetched_at"],
                title=f"New version v{row['version_no']} of {owner.label}",
                actor=None,
                body=None,
                source_object_id=owner.id,
                link=f"/sources/{owner.id}",
                meta={
                    "version_id": row["id"],
                    "version_no": row["version_no"],
                    "previous_version_no": row["version_no"] - 1,
                },
            )
        )
    return items


def issue_activity(
    conn: sqlalchemy.Connection,
    issue_key: str,
    *,
    kinds: Collection[str] = KINDS,
    limit: int = DEFAULT_LIMIT,
) -> list[ActivityItem]:
    """The newest `limit` events of an issue, newest first (UI-23).

    Covers non-deleted comments, history change sets without their comment events (a comment is already an
    item of its own), the latest revision of every entry and every version after the first of the actively
    linked sources and their children (DM-44, DM-57). Entries and versions are read in one query each for
    all linked objects.
    """
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValidationError(f"activity kinds must be among {', '.join(KINDS)}, got {', '.join(unknown)}")
    issue = issues.get_issue(conn, issue_key)
    persons = {person.id: person for person in people.list_persons(conn, include_merged=True)}
    names = {person_id: person.display_name for person_id, person in persons.items()}
    items: list[ActivityItem] = []

    if "comment" in kinds:
        for comment in comments.list_comments(conn, issue.key):
            author = names.get(comment.author_id, "Unknown")
            items.append(
                ActivityItem(
                    kind="comment",
                    at=comment.created_at,
                    title=f"Comment by {author}",
                    actor=author,
                    body=comment.body,
                    source_object_id=None,
                    link=None,
                    meta={"comment_id": comment.id, "edited": comment.edited_at is not None},
                )
            )

    if "history" in kinds:
        for group in history_view.change_sets(conn, issue.id, people.get_me(conn), names):
            events = [event for event in group.events if event.field != COMMENT_FIELD]
            if not events:
                continue
            items.append(
                ActivityItem(
                    kind="history",
                    at=group.at,
                    title=group.actor,
                    actor=group.actor,
                    body="\n".join(f"{e.field} {e.op}: {e.old} → {e.new}" for e in events),
                    source_object_id=None,
                    link=None,
                    meta={"change_set_id": group.change_set_id, "proposal": group.proposal},
                )
            )

    linked = linking.linked_object_ids(conn, issue.key) if {"entry", "version"} & set(kinds) else []
    objects = _objects(conn, linked) if linked else {}
    if objects and "entry" in kinds:
        items += _entry_items(conn, objects, persons, limit)
    if objects and "version" in kinds:
        items += _version_items(conn, objects, limit)

    rank = {kind: number for number, kind in enumerate(KINDS)}
    items.sort(
        key=lambda item: (
            _instant(item.at),
            rank[item.kind],
            _identifier(item),
        ),
        reverse=True,
    )
    return items[:limit]
