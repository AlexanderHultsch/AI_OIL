"""`oil link`, `oil unlink`, `oil sync`, `oil note edit`: sources of an issue (DM-54 .. DM-59, SYNC-1)."""

import argparse
import difflib
import sys
from collections.abc import Callable

import sqlalchemy
from sqlalchemy import text

from ai_oil.backup import maybe_auto_backup
from ai_oil.cli.common import transaction
from ai_oil.cli.context import open_context
from ai_oil.connectors import manual, outlook
from ai_oil.domain import people
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import ObjectMeta
from ai_oil.sources.sync import SyncScope, format_report, run_sync


def register(subparsers) -> None:
    link = subparsers.add_parser("link", help="link a source or a new note to an issue")
    link.add_argument("key", metavar="KEY", help="issue key")
    link.add_argument("reference", metavar="REFERENCE", nargs="?", default=None, help="URL, key or path")
    link.add_argument("--role", choices=linking.ROLES, default="reference", help="role of the source")
    link.add_argument("--note-text", default=None, help="why the source is relevant (link note)")
    link.add_argument("--connector", default=None, metavar="NAME", help="only ask this connector")
    link.add_argument("--title", default=None, help="title of a new note")
    link.add_argument("--text", default=None, help="text of a new note")
    link.add_argument(
        "--outlook-selected", action="store_true", help="link the conversation selected in Outlook"
    )
    link.add_argument(
        "--outlook-subject",
        default=None,
        metavar="TEXT",
        help="link an Outlook conversation found by subject",
    )
    link.add_argument(
        "--pick", type=int, default=None, metavar="N", help="with --outlook-subject: hit number"
    )
    link.set_defaults(func=run_link)

    unlink = subparsers.add_parser("unlink", help="remove a source from an issue")
    unlink.add_argument("key", metavar="KEY", help="issue key")
    unlink.add_argument("source_id", metavar="SOURCE_ID", type=int, help="source object id")
    unlink.set_defaults(func=run_unlink)

    sync = subparsers.add_parser("sync", help="fetch new content of linked sources")
    sync.add_argument("key", metavar="KEY", nargs="?", default=None, help="issue key")
    sync.add_argument("--source", type=int, default=None, metavar="ID", help="one source object")
    sync.add_argument("--connector", default=None, metavar="NAME", help="all sources of one connector")
    sync.add_argument("--all", action="store_true", help="everything linked to an open issue")
    sync.add_argument("--include-done", action="store_true", help="with --all: also finished issues")
    sync.set_defaults(func=run_sync_command)

    source = subparsers.add_parser("source", help="inspect stored sources")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    show = source_sub.add_parser("show", help="show a source object with its stored content")
    show.add_argument("source_id", metavar="ID", type=int, help="source object id")
    show.add_argument("--versions", action="store_true", help="list all stored versions")
    show.add_argument("--entries", action="store_true", help="list the entries (latest revisions)")
    show.add_argument(
        "--diff", nargs=2, type=int, default=None, metavar=("V1", "V2"), help="diff two version numbers"
    )
    show.set_defaults(func=run_source_show)

    note = subparsers.add_parser("note", help="edit notes")
    sub = note.add_subparsers(dest="note_command", required=True)
    edit = sub.add_parser("edit", help="store new text for a note as a new version")
    edit.add_argument("source_id", metavar="SOURCE_ID", type=int, help="note object id")
    edit.add_argument("--text", required=True, help="new note text")
    edit.add_argument("--title", default=None, help="new note title")
    edit.set_defaults(func=run_note_edit)


def _link_note(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        object_id = manual.create_note(conn, args.title, args.text)
        linked = linking.link_source(
            conn, args.key, object_id, role=args.role, note=args.note_text, actor_id=me.id
        )
        audit.log(
            conn,
            "source.link",
            "B",
            actor_type="user",
            target=linked.issue_key,
            detail={"source_object_id": object_id, "label": f"note: {args.title.strip()}", "role": args.role},
        )
    print(f"Linked note: {args.title.strip()} to {linked.issue_key}")
    return 0


def _choose(
    matches: list[tuple[registry.ConnectorRow, object]],
    reference: str,
    skipped: list[tuple[str, str]] | None = None,
) -> tuple[registry.ConnectorRow, object]:
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


Selection = tuple[registry.ConnectorRow, ObjectMeta] | None
Selector = Callable[
    [argparse.Namespace, list[tuple[registry.ConnectorRow, object]], list[tuple[str, str]]], Selection
]


def _select_reference(
    args: argparse.Namespace,
    instances: list[tuple[registry.ConnectorRow, object]],
    skipped: list[tuple[str, str]],
    reference: str | None = None,
) -> Selection:
    reference = args.reference if reference is None else reference
    matches = registry.match_reference(instances, reference)
    row, ref = _choose(matches, reference, skipped)
    connector = next(instance for candidate, instance in instances if candidate.id == row.id)
    return row, connector.resolve(ref)  # type: ignore[attr-defined]


def _outlook_instance(
    args: argparse.Namespace, instances: list[tuple[registry.ConnectorRow, object]]
) -> tuple[registry.ConnectorRow, object]:
    found = [(row, instance) for row, instance in instances if row.type == outlook.OutlookConnector.type]
    if args.connector is not None and instances and not found:
        raise ValidationError(f"connector {args.connector} is not an Outlook connector")
    if not found:
        raise ValidationError("no enabled Outlook connector — add one with `oil connector add outlook`")
    if len(found) > 1:
        names = ", ".join(row.name for row, _ in found)
        raise ValidationError(
            f"several Outlook connectors are enabled ({names}); choose one with --connector"
        )
    return found[0]


def _select_outlook_selected(
    args: argparse.Namespace,
    instances: list[tuple[registry.ConnectorRow, object]],
    skipped: list[tuple[str, str]],
) -> Selection:
    return _select_reference(args, [_outlook_instance(args, instances)], skipped, outlook.SELECTED_REFERENCE)


def _describe_candidate(number: int, candidate: outlook.ConversationCandidate) -> str:
    latest = candidate.latest.astimezone().strftime("%Y-%m-%d %H:%M") if candidate.latest else "no date"
    return (
        f"  {number}. {candidate.subject} — {latest} — {candidate.sender}"
        f" ({candidate.message_count} messages)"
    )


def _select_outlook_subject(
    args: argparse.Namespace,
    instances: list[tuple[registry.ConnectorRow, object]],
    skipped: list[tuple[str, str]],
) -> Selection:
    row, connector = _outlook_instance(args, instances)
    candidates = connector.find_conversations(args.outlook_subject)  # type: ignore[attr-defined]
    if not candidates:
        raise ValidationError(f"no email conversation matches '{args.outlook_subject}'")
    if args.pick is not None:
        if args.pick > len(candidates):
            raise ValidationError(
                f"--pick {args.pick} is out of range: {len(candidates)} conversations match"
            )
        chosen = candidates[args.pick - 1]
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        print("Several conversations match; re-run with --pick N:")
        for number, candidate in enumerate(candidates, start=1):
            print(_describe_candidate(number, candidate))
        return None
    return row, connector.resolve(chosen.ref)  # type: ignore[attr-defined]


def _link_source(args: argparse.Namespace, select: Selector) -> int:
    ctx = open_context(args)
    try:
        with ctx.engine.begin() as conn:
            me = people.get_me(conn)
            manual.ensure_manual_connector(conn)
            only = registry.get_connector(conn, args.connector).id if args.connector is not None else None
            instances = []
            skipped: list[tuple[str, str]] = []
            for candidate in registry.list_connectors(conn):
                if not candidate.enabled or (only is not None and candidate.id != only):
                    continue
                try:
                    instances.append((candidate, registry.instantiate(candidate)))
                except Exception as exc:
                    skipped.append((candidate.name, f"{type(exc).__name__}: {exc}"))
            if only is not None and skipped:
                raise ValidationError(f"connector {skipped[0][0]} is not usable: {skipped[0][1]}")
            selection = select(args, instances, skipped)
            if selection is None:
                return 1
            row, meta = selection
            object_id = store.upsert_object(conn, row.id, meta)
            linked = linking.link_source(
                conn, args.key, object_id, role=args.role, note=args.note_text, actor_id=me.id
            )
            label = linking.source_label(store.get_object(conn, object_id))
            audit.log(
                conn,
                "source.link",
                "B",
                actor_type="user",
                target=linked.issue_key,
                detail={"source_object_id": object_id, "label": label, "role": args.role},
            )
        report = run_sync(ctx.engine, SyncScope("source", object_id), config=ctx.config, trigger="cli")
    finally:
        ctx.engine.dispose()
    print(f"Linked {label} to {linked.issue_key}")
    print(format_report(report))
    return 0


def run_link(args: argparse.Namespace) -> int:
    note_args = args.title is not None or args.text is not None
    modes = [args.reference is not None, note_args, args.outlook_selected, args.outlook_subject is not None]
    if sum(modes) != 1 or (note_args and (args.title is None or args.text is None)):
        raise ValidationError(
            "give a reference, or --title and --text for a note, or --outlook-selected, or --outlook-subject"
        )
    if args.pick is not None and (args.outlook_subject is None or args.pick < 1):
        raise ValidationError("--pick needs --outlook-subject and a number of 1 or more")
    if note_args:
        return _link_note(args)
    if args.outlook_selected:
        return _link_source(args, _select_outlook_selected)
    if args.outlook_subject is not None:
        return _link_source(args, _select_outlook_subject)
    return _link_source(args, _select_reference)


def run_unlink(args: argparse.Namespace) -> int:
    key = args.key.strip().upper()
    with transaction(args) as conn:
        me = people.get_me(conn)
        linking.unlink_source(conn, key, args.source_id, actor_id=me.id)
        label = linking.source_label(store.get_object(conn, args.source_id))
        audit.log(
            conn,
            "source.unlink",
            "B",
            actor_type="user",
            target=key,
            detail={"source_object_id": args.source_id, "label": label},
        )
    print(f"Unlinked #{args.source_id} from {key}")
    return 0


def run_sync_command(args: argparse.Namespace) -> int:
    chosen = [args.key is not None, args.source is not None, args.connector is not None, args.all]
    if sum(chosen) != 1:
        raise ValidationError("choose KEY, --source, --connector or --all")
    if args.include_done and not args.all:
        raise ValidationError("--include-done only works with --all")
    if args.key is not None:
        scope = SyncScope("issue", args.key.strip().upper())
    elif args.source is not None:
        scope = SyncScope("source", args.source)
    elif args.connector is not None:
        scope = SyncScope("connector", args.connector)
    else:
        scope = SyncScope("all")
    ctx = open_context(args)
    try:
        report = run_sync(ctx.engine, scope, config=ctx.config, trigger="cli", include_done=args.include_done)
        print(format_report(report))
        try:
            backup_path = maybe_auto_backup(ctx.engine, ctx.config)
        except Exception as exc:
            print(f"warning: automatic backup failed: {exc}", file=sys.stderr)
        else:
            if backup_path is not None:
                print(f"Backup written: {backup_path}")
    finally:
        ctx.engine.dispose()
    return 1 if report.status == "failed" else 0


def run_note_edit(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        before = store.get_object(conn, args.source_id).title
        version_id = manual.edit_note(conn, args.source_id, args.text, title=args.title)
        obj = store.get_object(conn, args.source_id)
        changed = version_id is not None or obj.title != before
        audit.log(
            conn,
            "note.edit",
            "B",
            actor_type="user",
            target=linking.source_label(obj),
            detail={"source_object_id": obj.id, "version_id": version_id, "changed": changed},
        )
    print(f"Updated note #{args.source_id}" if changed else f"Note #{args.source_id} unchanged")
    return 0


_TEXT_LINE_LIMIT = 40
_ENTRY_PREVIEW_CHARS = 100


def _version_by_number(
    versions: list[store.SourceVersion], source_id: int, number: int
) -> store.SourceVersion:
    for version in versions:
        if version.version_no == number:
            return version
    raise ValidationError(f"source object {source_id} has no version {number}")


def _author_name(conn: sqlalchemy.Connection, identity_id: int | None) -> str:
    if identity_id is None:
        return "-"
    name = conn.execute(
        text(
            "SELECT p.display_name FROM person_identity i JOIN person p ON p.id = i.person_id"
            " WHERE i.id = :id"
        ),
        {"id": identity_id},
    ).scalar_one_or_none()
    return name or "-"


def run_source_show(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        obj = store.get_object(conn, args.source_id)
        connector = registry.get_connector(conn, obj.connector_id)
        versions = store.list_versions(conn, obj.id)
        entries = store.list_entries(conn, obj.id)
        issue_keys = linking.issues_for_source(conn, obj.id)
        lines = [f"#{obj.id} {obj.object_type} {obj.display_ref or obj.title or obj.external_id}"]
        if obj.title is not None:
            lines.append(f"Title: {obj.title}")
        lines.append(f"Connector: {connector.name} ({connector.type})")
        if obj.locator is not None:
            lines.append(f"Locator: {obj.locator}")
        lines.append(
            f"Status: {obj.fetch_status}, checked {obj.last_checked_at or 'never'},"
            f" changed {obj.last_changed_at or 'never'}"
        )
        if obj.fetch_error:
            lines.append(f"Error: {obj.fetch_error}")
        if obj.parent_id is not None:
            parent = store.get_object(conn, obj.parent_id)
            lines.append(f"Part of: #{parent.id} {linking.source_label(parent)}")
        lines.append(f"Linked to: {', '.join(issue_keys) if issue_keys else '-'}")
        children = store.list_children(conn, obj.id)
        lines.append(f"Versions: {len(versions)} | Entries: {len(entries)} | Children: {len(children)}")

        if versions:
            latest = versions[-1]
            body = latest.text.splitlines()
            lines += ["", f"Latest version (v{latest.version_no}, fetched {latest.fetched_at}):"]
            lines += [f"  {line}".rstrip() for line in body[:_TEXT_LINE_LIMIT]]
            if len(body) > _TEXT_LINE_LIMIT:
                lines.append(f"  … ({len(body) - _TEXT_LINE_LIMIT} more lines)")

        if args.versions:
            lines += ["", "Versions:"]
            lines += [
                f"  v{v.version_no}  {v.fetched_at}  {v.source_version_ref or '-'}  {v.content_hash[:12]}"
                for v in versions
            ]

        if args.entries:
            lines += ["", "Entries:"]
            for entry in entries:
                preview = (entry.new_text or entry.text or "").splitlines()
                first = preview[0][:_ENTRY_PREVIEW_CHARS] if preview else ""
                lines.append(
                    f"  {entry.occurred_at}  {entry.entry_type}  {entry.direction or '-'}"
                    f"  {_author_name(conn, entry.author_identity_id)}  {first}".rstrip()
                )

        if args.diff is not None:
            first_no, second_no = args.diff
            first_version = _version_by_number(versions, obj.id, first_no)
            second_version = _version_by_number(versions, obj.id, second_no)
            lines += ["", f"Diff v{first_no} → v{second_no}:"]
            lines += difflib.unified_diff(
                first_version.text.splitlines(),
                second_version.text.splitlines(),
                fromfile=f"v{first_no}",
                tofile=f"v{second_no}",
                lineterm="",
            )
    print("\n".join(lines))
    return 0
