"""`oil comment`: the user's own notes on an issue (DM-19)."""

import argparse

from ai_oil.cli.common import transaction
from ai_oil.domain import comments, people
from ai_oil.governance import audit


def register(subparsers) -> None:
    comment = subparsers.add_parser("comment", help="add, edit or delete comments")
    sub = comment.add_subparsers(dest="comment_command", required=True)

    add = sub.add_parser("add", help="add a comment to an issue")
    add.add_argument("key", metavar="KEY", help="issue key")
    add.add_argument("text", metavar="TEXT", help="comment text")
    add.set_defaults(func=run_add)

    edit = sub.add_parser("edit", help="replace the text of a comment")
    edit.add_argument("id", metavar="ID", type=int, help="comment id")
    edit.add_argument("text", metavar="TEXT", help="new comment text")
    edit.set_defaults(func=run_edit)

    delete = sub.add_parser("delete", help="delete a comment")
    delete.add_argument("id", metavar="ID", type=int, help="comment id")
    delete.set_defaults(func=run_delete)


def run_add(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        comment = comments.add_comment(conn, args.key, args.text, author_id=me.id)
        audit.log(
            conn,
            "comment.add",
            "B",
            actor_type="user",
            target=comment.issue_key,
            detail={"comment_id": comment.id, "text": comment.body},
        )
    print(f"Added comment #{comment.id} to {comment.issue_key}")
    return 0


def run_edit(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        comment = comments.edit_comment(conn, args.id, args.text, actor_id=me.id)
        audit.log(
            conn,
            "comment.edit",
            "B",
            actor_type="user",
            target=comment.issue_key,
            detail={"comment_id": comment.id, "text": comment.body},
        )
    print(f"Updated comment #{comment.id}")
    return 0


def run_delete(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        comment = comments.get_comment(conn, args.id)
        comments.delete_comment(conn, args.id, actor_id=me.id)
        audit.log(
            conn,
            "comment.delete",
            "B",
            actor_type="user",
            target=comment.issue_key,
            detail={"comment_id": comment.id},
        )
    print(f"Deleted comment #{comment.id}")
    return 0
