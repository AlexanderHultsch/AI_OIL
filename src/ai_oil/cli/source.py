"""`oil link`, `oil unlink`, `oil sync`, `oil note edit`: sources of an issue (DM-54 .. DM-59, SYNC-1)."""

import argparse

from ai_oil.cli.common import transaction
from ai_oil.cli.context import open_context
from ai_oil.connectors import manual
from ai_oil.domain import people
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
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
    matches: list[tuple[registry.ConnectorRow, object]], reference: str
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
    raise ValidationError(f"no connector recognises '{reference}'")


def _link_reference(args: argparse.Namespace) -> int:
    ctx = open_context(args)
    try:
        with ctx.engine.begin() as conn:
            me = people.get_me(conn)
            manual.ensure_manual_connector(conn)
            only = registry.get_connector(conn, args.connector).id if args.connector is not None else None
            instances = [
                (row, registry.instantiate(row))
                for row in registry.list_connectors(conn)
                if row.enabled and (only is None or row.id == only)
            ]
            matches = registry.match_reference(instances, args.reference)
            row, ref = _choose(matches, args.reference)
            connector = next(instance for candidate, instance in instances if candidate.id == row.id)
            meta = connector.resolve(ref)  # type: ignore[attr-defined]
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
    if (args.reference is None) == (not note_args) or (
        note_args and (args.title is None or args.text is None)
    ):
        raise ValidationError("give a reference, or --title and --text for a note")
    return _link_note(args) if note_args else _link_reference(args)


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
    finally:
        ctx.engine.dispose()
    print(format_report(report))
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
