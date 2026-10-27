"""`oil person`: people and their identities (DM-20 .. DM-23)."""

import argparse

from ai_oil.cli.common import transaction
from ai_oil.domain import people
from ai_oil.governance import audit


def register(subparsers) -> None:
    person = subparsers.add_parser("person", help="list, add or merge people")
    sub = person.add_subparsers(dest="person_command", required=True)

    list_ = sub.add_parser("list", help="list people")
    list_.add_argument("--all", action="store_true", help="include merged people")
    list_.set_defaults(func=run_list)

    add = sub.add_parser("add", help="add a person")
    add.add_argument("name", metavar="NAME", help="display name")
    add.add_argument("--org", default=None, help="organisation")
    add.add_argument("--email", action="append", default=None, help="e-mail address (repeat for several)")
    add.add_argument("--atlassian", default=None, help="Jira/Confluence accountId")
    add.set_defaults(func=run_add)

    merge = sub.add_parser("merge", help="merge one person into another")
    merge.add_argument("source", metavar="SOURCE", help="person to merge away: id, e-mail address or name")
    merge.add_argument("target", metavar="TARGET", help="person to keep: id, e-mail address or name")
    merge.set_defaults(func=run_merge)


def run_list(args: argparse.Namespace) -> int:
    lines: list[str] = []
    with transaction(args) as conn:
        for person in people.list_persons(conn, include_merged=args.all):
            line = f"{person.id}  {person.display_name}"
            if person.organisation:
                line += f" ({person.organisation})"
            if person.is_me:
                line += " *me*"
            if person.merged_into_id is not None:
                line += f" (merged into {person.merged_into_id})"
            lines.append(line)
            for identity in people.list_identities(conn, person.id):
                lines.append(f"    {identity.system}: {identity.external_id}")
    for line in lines:
        print(line)
    return 0


def run_add(args: argparse.Namespace) -> int:
    emails = args.email or []
    with transaction(args) as conn:
        person = people.create_person(conn, args.name, organisation=args.org)
        for address in emails:
            people.add_identity(conn, person.id, "email", address)
        if args.atlassian:
            people.add_identity(conn, person.id, "atlassian", args.atlassian)
        audit.log(
            conn,
            "person.create",
            "B",
            actor_type="user",
            target=str(person.id),
            detail={
                "name": person.display_name,
                "organisation": args.org,
                "emails": emails,
                "atlassian": args.atlassian,
            },
        )
    print(f"Created person #{person.id} {person.display_name}")
    return 0


def run_merge(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        me = people.get_me(conn)
        source = people.lookup_person(conn, args.source)
        target = people.lookup_person(conn, args.target)
        repointed = people.merge_persons(conn, source.id, target.id, actor_id=me.id)
        audit.log(
            conn,
            "person.merge",
            "B",
            actor_type="user",
            target=str(source.id),
            detail={"source_id": source.id, "target_id": target.id, "issue_fields_repointed": repointed},
        )
    print(f"Merged #{source.id} into #{target.id} ({repointed} issue fields re-pointed)")
    return 0
