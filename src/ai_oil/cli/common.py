"""Helpers shared by the CLI command modules."""

import argparse
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import sqlalchemy

from ai_oil.cli.context import open_context
from ai_oil.domain import comments, history, issue_links, issues, people

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def date_arg(value: str) -> date:
    """argparse `type=` for a YYYY-MM-DD date."""
    if not _DATE_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid date {value!r}: use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}: use YYYY-MM-DD") from None


def date_or_none_arg(value: str) -> date | None:
    """argparse `type=` for a YYYY-MM-DD date or the literal 'none' (case-insensitive), which gives None."""
    if value.strip().lower() == "none":
        return None
    return date_arg(value)


def is_none(value: str) -> bool:
    """True for the literal 'none' (case-insensitive) that clears an optional field."""
    return value.strip().lower() == "none"


@contextmanager
def transaction(args: argparse.Namespace) -> Iterator[sqlalchemy.Connection]:
    """Open the database and yield a connection in one transaction; the engine is always disposed."""
    ctx = open_context(args)
    try:
        with ctx.engine.begin() as conn:
            yield conn
    finally:
        ctx.engine.dispose()


def _single_line(value: str | None) -> str:
    return "-" if value is None else " ".join(value.splitlines())


def _indented(text: str) -> list[str]:
    return [f"  {line}".rstrip() for line in text.splitlines()]


def format_issue(
    conn: sqlalchemy.Connection, issue: issues.Issue, *, show_history: bool = False
) -> list[str]:
    """The lines printed by `oil issue show`."""
    lines = [
        f"{issue.key}  {issue.summary}",
        f"Type: {issue.issue_type} | Status: {issue.status} | Priority: {issue.priority}",
    ]
    if issue.blocked_reason:
        lines.append(f"Blocked reason: {issue.blocked_reason}")
    if issue.cancel_reason:
        lines.append(f"Cancel reason: {issue.cancel_reason}")
    assignee = people.get_person(conn, issue.assignee_id).display_name if issue.assignee_id else "-"
    reporter = people.get_person(conn, issue.reporter_id).display_name
    lines.append(f"Assignee: {assignee} | Reporter: {reporter}")
    if issue.revised_due_date is not None:
        original = issue.original_due_date.isoformat() if issue.original_due_date else "-"
        lines.append(f"Due: {issue.revised_due_date.isoformat()} (original {original})")
    if issue.parent_key:
        lines.append(f"Parent: {issue.parent_key}")
    if issue.labels:
        lines.append(f"Labels: {', '.join(issue.labels)}")
    if issue.components:
        lines.append(f"Components: {', '.join(issue.components)}")
    lines.append(f"Origin: {issue.origin_type} | Created: {issue.created_at} | Updated: {issue.updated_at}")

    lines += ["", "Description:", *_indented(issue.description)]
    if issue.acceptance_criteria:
        lines += ["", "Acceptance criteria:", *_indented(issue.acceptance_criteria)]

    links = issue_links.list_issue_links(conn, issue.key)
    if links:
        lines += ["", "Links:"]
        lines += [
            f"  {link.label} {link.other_key}  {link.other_summary} [{link.other_status}]" for link in links
        ]

    live_comments = comments.list_comments(conn, issue.key)
    if live_comments:
        lines += ["", "Comments:"]
        for comment in live_comments:
            author = people.get_person(conn, comment.author_id).display_name
            first_line = comment.body.splitlines()[0] if comment.body else ""
            lines.append(f"  #{comment.id} {author} {comment.created_at}: {first_line}")

    if show_history:
        entries = history.issue_history(conn, issue.id)
        lines += ["", "History:"]
        for e in entries:
            old, new = _single_line(e.old_value), _single_line(e.new_value)
            lines.append(f"  {e.at} {e.actor_type} {e.field} {e.op} {old} -> {new}")
    return lines
