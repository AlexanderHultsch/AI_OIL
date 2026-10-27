"""People page: list with identities, add a person, merge two persons with a confirmation step (spec 10.4)."""

import re
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import text
from starlette.responses import RedirectResponse, Response

from ai_oil.domain import people
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.web.deps import db_read, db_write
from ai_oil.web.rendering import render

router = APIRouter()

VIA = "web"
PAGE = "/people"
_EMAIL_SEPARATORS = re.compile(r"[,\r\n]+")


@router.get(PAGE)
def person_list(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_read)],
    show_all: Annotated[str | None, Query(alias="all")] = None,
):
    include_merged = show_all == "1"
    persons = people.list_persons(conn, include_merged=include_merged)
    names = {person.id: person.display_name for person in people.list_persons(conn, include_merged=True)}
    return render(
        request,
        "people/list.html",
        {
            "rows": [(person, people.list_identities(conn, person.id)) for person in persons],
            "names": names,
            "include_merged": include_merged,
            "active": [person for person in persons if person.merged_into_id is None],
            "nav_active": "people",
        },
    )


@router.post(PAGE)
def person_create(
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    name: Annotated[str, Form()] = "",
    organisation: Annotated[str, Form()] = "",
    emails: Annotated[str, Form()] = "",
    atlassian: Annotated[str, Form()] = "",
) -> Response:
    org = organisation.strip() or None
    addresses = [part.strip() for part in _EMAIL_SEPARATORS.split(emails) if part.strip()]
    account = atlassian.strip() or None
    person = people.create_person(conn, name, organisation=org)
    for address in addresses:
        people.add_identity(conn, person.id, "email", address)
    if account:
        people.add_identity(conn, person.id, "atlassian", account)
    audit.log(
        conn,
        "person.create",
        "B",
        actor_type="user",
        target=str(person.id),
        detail={
            "name": person.display_name,
            "organisation": org,
            "emails": addresses,
            "atlassian": account,
            "via": VIA,
        },
    )
    return RedirectResponse(PAGE, status_code=303)


def _person_id(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise ValidationError("choose a person from the list") from None


def _check_mergeable(source: people.Person, target: people.Person) -> None:
    """The rules `merge_persons` enforces, checked before the confirmation page (DM-23)."""
    if source.id == target.id:
        raise ValidationError("cannot merge a person into itself")
    for person in (source, target):
        if person.merged_into_id is not None:
            raise ValidationError(f"person {person.id} is already merged")
    if source.is_me:
        raise ValidationError("the 'me' person cannot be merged into another person")


def _issue_fields(conn: sqlalchemy.Connection, person_id: int) -> int:
    """Assignee and reporter fields of issues that point to the person."""
    return conn.execute(
        text(
            "SELECT (SELECT COUNT(*) FROM issue WHERE assignee_id = :id)"
            " + (SELECT COUNT(*) FROM issue WHERE reporter_id = :id)"
        ),
        {"id": person_id},
    ).scalar_one()


@router.post("/people/merge")
def person_merge(
    request: Request,
    conn: Annotated[sqlalchemy.Connection, Depends(db_write)],
    source: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Without `confirm=1` show what a merge would do; with it, merge (DM-23)."""
    source_person = people.get_person(conn, _person_id(source))
    target_person = people.get_person(conn, _person_id(target))
    _check_mergeable(source_person, target_person)
    if confirm != "1":
        return render(
            request,
            "people/merge_confirm.html",
            {
                "source": source_person,
                "target": target_person,
                "fields": _issue_fields(conn, source_person.id),
                "nav_active": "people",
            },
        )
    me = people.get_me(conn)
    repointed = people.merge_persons(conn, source_person.id, target_person.id, actor_id=me.id)
    audit.log(
        conn,
        "person.merge",
        "B",
        actor_type="user",
        target=str(source_person.id),
        detail={
            "source_id": source_person.id,
            "target_id": target_person.id,
            "issue_fields_repointed": repointed,
            "via": VIA,
        },
    )
    return RedirectResponse(PAGE, status_code=303)
