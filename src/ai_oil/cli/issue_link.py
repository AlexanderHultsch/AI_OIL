"""`oil issue-link`: typed links between issues (DM-14)."""

import argparse

from ai_oil.cli.common import transaction
from ai_oil.domain import issue_links, people
from ai_oil.governance import audit


def register(subparsers) -> None:
    parser = subparsers.add_parser("issue-link", help="link two issues, or remove a link")
    parser.add_argument("from_key", metavar="FROM", help="issue key the link starts at")
    parser.add_argument("link_type", metavar="TYPE", choices=issue_links.LINK_TYPES, help="link type")
    parser.add_argument("to_key", metavar="TO", help="issue key the link points to")
    parser.add_argument("--remove", action="store_true", help="remove the link instead of adding it")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from_key = args.from_key.strip().upper()
    to_key = args.to_key.strip().upper()
    detail: dict[str, object] = {"from": from_key, "type": args.link_type, "to": to_key}
    with transaction(args) as conn:
        me = people.get_me(conn)
        if args.remove:
            issue_links.unlink_issues(conn, from_key, args.link_type, to_key, actor_id=me.id)
            action = "issue_link.remove"
        else:
            detail["link_id"] = issue_links.link_issues(
                conn, from_key, args.link_type, to_key, actor_id=me.id
            )
            action = "issue_link.add"
        audit.log(conn, action, "B", actor_type="user", target=from_key, detail=detail)
    if args.remove:
        print(f"Removed link {from_key} {args.link_type} {to_key}")
    else:
        print(f"Linked {from_key} {args.link_type} {to_key}")
    return 0
