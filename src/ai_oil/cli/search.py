"""`oil search`: run an OQL query over the issues (SRCH-4 .. SRCH-7)."""

import argparse
import json
import sys

import sqlalchemy

from ai_oil.cli.common import transaction
from ai_oil.domain import issues, people
from ai_oil.search import compiler, oql

_HEADER = ("KEY", "TYPE", "STATUS", "PRIORITY", "ASSIGNEE", "DUE", "SUMMARY")


def _limit_arg(value: str) -> int:
    """argparse `type=` for an integer of at least 1."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"invalid limit {value!r}: use an integer of at least 1")
    return number


def register(subparsers) -> None:
    search = subparsers.add_parser("search", help="search issues with an OQL query")
    search.add_argument("query", metavar="QUERY", help="OQL query, e.g. 'status = Blocked ORDER BY due'")
    search.add_argument("--json", action="store_true", help="print a JSON array instead of a table")
    search.add_argument("--limit", type=_limit_arg, default=None, metavar="N", help="return at most N issues")
    search.set_defaults(func=run_search)


def _name(conn: sqlalchemy.Connection, person_id: int | None) -> str | None:
    return None if person_id is None else people.get_person(conn, person_id).display_name


def _rows(conn: sqlalchemy.Connection, found: list[issues.Issue]) -> list[dict[str, object]]:
    return [
        {
            "key": issue.key,
            "type": issue.issue_type,
            "status": issue.status,
            "priority": issue.priority,
            "assignee": _name(conn, issue.assignee_id),
            "reporter": _name(conn, issue.reporter_id),
            "due": issue.revised_due_date.isoformat() if issue.revised_due_date else None,
            "summary": issue.summary,
            "updated": issue.updated_at,
        }
        for issue in found
    ]


def _table(rows: list[dict[str, object]]) -> list[str]:
    cells = [
        (
            str(row["key"]),
            str(row["type"]),
            str(row["status"]),
            str(row["priority"]),
            str(row["assignee"] or "-"),
            str(row["due"] or "-"),
            str(row["summary"]),
        )
        for row in rows
    ]
    widths = [max(len(line[column]) for line in [_HEADER, *cells]) for column in range(len(_HEADER))]
    return [
        "  ".join(value.ljust(width) for value, width in zip(line, widths, strict=True)).rstrip()
        for line in [_HEADER, *cells]
    ]


def run_search(args: argparse.Namespace) -> int:
    try:
        with transaction(args) as conn:
            rows = _rows(conn, compiler.search_issues(conn, args.query, limit=args.limit))
    except oql.OqlSyntaxError as exc:
        print(oql.format_error(args.query, exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if rows:
        for line in _table(rows):
            print(line)
    print(f"{len(rows)} {'issue' if len(rows) == 1 else 'issues'}")
    return 0
