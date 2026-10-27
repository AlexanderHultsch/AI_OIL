"""`oil issue`: create, edit and show issues (DM-5 .. DM-18)."""

import argparse

from ai_oil.cli.common import date_arg, date_or_none_arg, format_issue, is_none, transaction
from ai_oil.domain import issues, people
from ai_oil.errors import ValidationError
from ai_oil.governance import audit


def register(subparsers) -> None:
    issue = subparsers.add_parser("issue", help="create, edit and show issues")
    sub = issue.add_subparsers(dest="issue_command", required=True)

    create = sub.add_parser("create", help="create an issue")
    create.add_argument("-p", "--project", required=True, help="project key")
    create.add_argument("-s", "--summary", required=True, help="summary")
    create.add_argument("-d", "--description", required=True, help="description")
    create.add_argument("--type", default="Task", help="issue type (default: Task)")
    create.add_argument("--parent", default=None, help="parent issue key")
    create.add_argument("--priority", default="P4", help="priority (default: P4)")
    create.add_argument("--assignee", default=None, help="assignee: id, e-mail address or name")
    create.add_argument("--reporter", default=None, help="reporter: id, e-mail address or name (default: me)")
    create.add_argument("--due", type=date_arg, default=None, help="due date, YYYY-MM-DD")
    create.add_argument("--acceptance", default=None, help="acceptance criteria")
    create.add_argument("--label", action="append", default=None, help="label (repeat for several)")
    create.add_argument("--component", action="append", default=None, help="component (repeat for several)")
    create.set_defaults(func=run_create)

    edit = sub.add_parser("edit", help="change an issue")
    edit.add_argument("key", metavar="KEY", help="issue key")
    edit.add_argument("--summary", default=None)
    edit.add_argument("--description", default=None)
    edit.add_argument("--acceptance", default=None, help="acceptance criteria")
    edit.add_argument("--type", default=None, help="issue type")
    edit.add_argument("--parent", default=None, help="parent issue key, or 'none'")
    edit.add_argument("--priority", default=None)
    edit.add_argument("--assignee", default=None, help="assignee: id, e-mail address, name, or 'none'")
    edit.add_argument("--reporter", default=None, help="reporter: id, e-mail address or name")
    edit.add_argument("--due", type=date_arg, default=None, help="due date, YYYY-MM-DD")
    edit.add_argument(
        "--snooze", type=date_or_none_arg, default=argparse.SUPPRESS, help="YYYY-MM-DD or 'none'"
    )
    edit.add_argument("--add-label", action="append", default=None, metavar="NAME")
    edit.add_argument("--remove-label", action="append", default=None, metavar="NAME")
    edit.add_argument("--add-component", action="append", default=None, metavar="NAME")
    edit.add_argument("--remove-component", action="append", default=None, metavar="NAME")
    edit.add_argument("--status", default=None, help="move the issue to this status")
    edit.add_argument("--reason", default=None, help="reason for Blocked or Cancelled (needs --status)")
    edit.set_defaults(func=run_edit)

    show = sub.add_parser("show", help="show an issue")
    show.add_argument("key", metavar="KEY", help="issue key")
    show.add_argument("--history", action="store_true", help="include the change history")
    show.set_defaults(func=run_show)


def run_create(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        assignee = people.lookup_person(conn, args.assignee) if args.assignee is not None else None
        reporter = people.lookup_person(conn, args.reporter) if args.reporter is not None else None
        issue = issues.create_issue(
            conn,
            args.project,
            args.summary,
            args.description,
            actor_id=me.id,
            issue_type=args.type,
            parent_key=args.parent,
            priority=args.priority,
            assignee_id=assignee.id if assignee else None,
            reporter_id=reporter.id if reporter else None,
            due_date=args.due,
            acceptance_criteria=args.acceptance,
            labels=args.label or (),
            components=args.component or (),
        )
        audit.log(
            conn,
            "issue.create",
            "B",
            actor_type="user",
            target=issue.key,
            detail={
                "project": args.project,
                "summary": args.summary,
                "description": args.description,
                "type": args.type,
                "parent": args.parent,
                "priority": args.priority,
                "assignee": args.assignee,
                "reporter": args.reporter,
                "due": args.due.isoformat() if args.due else None,
                "acceptance": args.acceptance,
                "labels": args.label or [],
                "components": args.component or [],
            },
        )
    print(issue.key)
    return 0


def run_edit(args: argparse.Namespace) -> int:
    if args.reason is not None and args.status is None:
        raise ValidationError("--reason needs --status")
    changes: dict[str, object] = {}
    detail: dict[str, object] = {}
    simple = (
        ("summary", "summary", args.summary),
        ("description", "description", args.description),
        ("acceptance", "acceptance_criteria", args.acceptance),
        ("type", "issue_type", args.type),
        ("priority", "priority", args.priority),
    )
    for option, field, value in simple:
        if value is not None:
            changes[field] = value
            detail[option] = value
    if args.parent is not None:
        changes["parent"] = None if is_none(args.parent) else args.parent
        detail["parent"] = changes["parent"]
    if args.due is not None:
        changes["due_date"] = args.due
        detail["due"] = args.due.isoformat()
    if hasattr(args, "snooze"):
        changes["snoozed_until"] = args.snooze
        detail["snooze"] = args.snooze.isoformat() if args.snooze else None
    label_component_options = (
        ("add_label", args.add_label),
        ("remove_label", args.remove_label),
        ("add_component", args.add_component),
        ("remove_component", args.remove_component),
    )
    for option, values in label_component_options:
        if values:
            detail[option] = list(values)
    if args.status is not None:
        detail["status"] = args.status
        if args.reason is not None:
            detail["reason"] = args.reason
    if args.assignee is not None:
        detail["assignee"] = None if is_none(args.assignee) else args.assignee
    if args.reporter is not None:
        detail["reporter"] = args.reporter
    if not detail:
        raise ValidationError("nothing to change")

    with transaction(args) as conn:
        me = people.get_me(conn)
        if args.assignee is not None:
            changes["assignee_id"] = (
                None if is_none(args.assignee) else people.lookup_person(conn, args.assignee).id
            )
        if args.reporter is not None:
            changes["reporter_id"] = people.lookup_person(conn, args.reporter).id
        key = args.key.strip().upper()
        if changes:
            issues.update_issue(conn, key, changes, actor_id=me.id)
        for name in args.add_label or ():
            issues.add_label(conn, key, name, actor_id=me.id)
        for name in args.remove_label or ():
            issues.remove_label(conn, key, name, actor_id=me.id)
        for name in args.add_component or ():
            issues.add_component(conn, key, name, actor_id=me.id)
        for name in args.remove_component or ():
            issues.remove_component(conn, key, name, actor_id=me.id)
        if args.status is not None:
            issues.transition_issue(conn, key, args.status, actor_id=me.id, reason=args.reason)
        audit.log(conn, "issue.edit", "B", actor_type="user", target=key, detail=detail)
    print(f"Updated {key}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        issue = issues.get_issue(conn, args.key)
        lines = format_issue(conn, issue, show_history=args.history)
    for line in lines:
        print(line)
    return 0
