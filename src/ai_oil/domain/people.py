"""People and their identities in other systems (DM-20 .. DM-23)."""

from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import history
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.timeutil import utcnow_iso

IDENTITY_SYSTEMS = ("email", "atlassian", "teams")

_PERSON_COLUMNS = "id, display_name, organisation, is_me, merged_into_id, notes"
_IDENTITY_COLUMNS = "id, person_id, system, external_id, display_name"


@dataclass(frozen=True)
class Person:
    id: int
    display_name: str
    organisation: str | None
    is_me: bool
    merged_into_id: int | None
    notes: str | None


@dataclass(frozen=True)
class Identity:
    id: int
    person_id: int
    system: str
    external_id: str
    display_name: str | None


def _person(row: sqlalchemy.RowMapping) -> Person:
    return Person(
        id=row["id"],
        display_name=row["display_name"],
        organisation=row["organisation"],
        is_me=bool(row["is_me"]),
        merged_into_id=row["merged_into_id"],
        notes=row["notes"],
    )


def _identity(row: sqlalchemy.RowMapping) -> Identity:
    return Identity(
        id=row["id"],
        person_id=row["person_id"],
        system=row["system"],
        external_id=row["external_id"],
        display_name=row["display_name"],
    )


def normalize_external_id(system: str, external_id: str) -> str:
    """Canonical form of an external id: e-mail addresses are lower-case (DM-21)."""
    if system not in IDENTITY_SYSTEMS:
        raise ValidationError(
            f"unknown identity system {system!r}; expected one of {', '.join(IDENTITY_SYSTEMS)}"
        )
    value = external_id.strip()
    return value.lower() if system == "email" else value


def create_person(
    conn: sqlalchemy.Connection,
    display_name: str,
    *,
    organisation: str | None = None,
    is_me: bool = False,
    notes: str | None = None,
) -> Person:
    """Create a person; exactly one person may be 'me' (DM-20)."""
    name = display_name.strip()
    if not name:
        raise ValidationError("display name must not be blank")
    if is_me and conn.execute(text("SELECT 1 FROM person WHERE is_me = 1")).first() is not None:
        raise ConflictError("a 'me' person already exists")
    person_id = conn.execute(
        text(
            "INSERT INTO person (display_name, organisation, is_me, notes, created_at)"
            " VALUES (:name, :organisation, :is_me, :notes, :now) RETURNING id"
        ),
        {
            "name": name,
            "organisation": organisation,
            "is_me": int(is_me),
            "notes": notes,
            "now": utcnow_iso(),
        },
    ).scalar_one()
    return get_person(conn, person_id)


def get_person(conn: sqlalchemy.Connection, person_id: int) -> Person:
    row = (
        conn.execute(text(f"SELECT {_PERSON_COLUMNS} FROM person WHERE id = :id"), {"id": person_id})
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"person {person_id} not found")
    return _person(row)


def get_me(conn: sqlalchemy.Connection) -> Person:
    row = conn.execute(text(f"SELECT {_PERSON_COLUMNS} FROM person WHERE is_me = 1")).mappings().first()
    if row is None:
        raise NotFoundError("no 'me' person — run `oil init`")
    return _person(row)


def list_persons(conn: sqlalchemy.Connection, *, include_merged: bool = False) -> list[Person]:
    where = "" if include_merged else " WHERE merged_into_id IS NULL"
    rows = conn.execute(
        text(f"SELECT {_PERSON_COLUMNS} FROM person{where} ORDER BY lower(display_name), id")
    ).mappings()
    return [_person(row) for row in rows]


def list_identities(conn: sqlalchemy.Connection, person_id: int) -> list[Identity]:
    rows = conn.execute(
        text(
            f"SELECT {_IDENTITY_COLUMNS} FROM person_identity"
            " WHERE person_id = :person_id ORDER BY system, external_id"
        ),
        {"person_id": person_id},
    ).mappings()
    return [_identity(row) for row in rows]


def _find_identity(conn: sqlalchemy.Connection, system: str, external_id: str) -> Identity | None:
    row = (
        conn.execute(
            text(
                f"SELECT {_IDENTITY_COLUMNS} FROM person_identity"
                " WHERE system = :system AND external_id = :external_id"
            ),
            {"system": system, "external_id": external_id},
        )
        .mappings()
        .first()
    )
    return _identity(row) if row is not None else None


def _insert_identity(
    conn: sqlalchemy.Connection, person_id: int, system: str, external_id: str, display_name: str | None
) -> Identity:
    identity_id = conn.execute(
        text(
            "INSERT INTO person_identity (person_id, system, external_id, display_name)"
            " VALUES (:person_id, :system, :external_id, :display_name) RETURNING id"
        ),
        {"person_id": person_id, "system": system, "external_id": external_id, "display_name": display_name},
    ).scalar_one()
    return Identity(identity_id, person_id, system, external_id, display_name)


def add_identity(
    conn: sqlalchemy.Connection,
    person_id: int,
    system: str,
    external_id: str,
    display_name: str | None = None,
) -> Identity:
    """Attach an identity to a person; idempotent, refuses an identity owned by someone else (DM-21)."""
    external_id = normalize_external_id(system, external_id)
    if not external_id:
        raise ValidationError("external id must not be blank")
    get_person(conn, person_id)
    existing = _find_identity(conn, system, external_id)
    if existing is not None:
        if existing.person_id == person_id:
            return existing
        owner = get_person(conn, existing.person_id)
        raise ConflictError(
            f"{system} identity {external_id!r} already belongs to person {owner.id}: {owner.display_name}"
        )
    return _insert_identity(conn, person_id, system, external_id, display_name)


def find_person_by_identity(conn: sqlalchemy.Connection, system: str, external_id: str) -> Person | None:
    """The person behind an identity, following merges to the surviving person (DM-23)."""
    identity = _find_identity(conn, system, normalize_external_id(system, external_id))
    if identity is None:
        return None
    person = get_person(conn, identity.person_id)
    while person.merged_into_id is not None:
        person = get_person(conn, person.merged_into_id)
    return person


def resolve_identity(
    conn: sqlalchemy.Connection, system: str, external_id: str, display_name: str | None = None
) -> Identity:
    """Return the identity, creating a person and the identity when it is unknown (DM-22).

    Exchange-internal addresses are resolved to SMTP by the Outlook connector, not here.
    """
    external_id = normalize_external_id(system, external_id)
    if not external_id:
        raise ValidationError("external id must not be blank")
    name = display_name.strip() if display_name else None
    existing = _find_identity(conn, system, external_id)
    if existing is not None:
        if name and name != existing.display_name:
            conn.execute(
                text("UPDATE person_identity SET display_name = :name WHERE id = :id"),
                {"name": name, "id": existing.id},
            )
            return Identity(existing.id, existing.person_id, system, external_id, name)
        return existing
    person = create_person(conn, name or external_id)
    return _insert_identity(conn, person.id, system, external_id, name)


def lookup_person(conn: sqlalchemy.Connection, ref: str) -> Person:
    """Resolve a CLI reference: 'me', a numeric id, an e-mail address or an exact display name."""
    ref = ref.strip()
    if ref.lower() == "me":
        return get_me(conn)
    if ref.isdigit():
        return get_person(conn, int(ref))
    if "@" in ref:
        person = find_person_by_identity(conn, "email", ref)
        if person is None:
            raise NotFoundError(f"no person with e-mail address {ref!r}")
        return person
    rows = conn.execute(
        text(
            f"SELECT {_PERSON_COLUMNS} FROM person"
            " WHERE merged_into_id IS NULL AND lower(display_name) = lower(:name) ORDER BY id"
        ),
        {"name": ref},
    ).mappings()
    matches = [_person(row) for row in rows]
    if not matches:
        raise NotFoundError(f"no person named {ref!r}")
    if len(matches) > 1:
        candidates = ", ".join(f"{p.id}: {p.display_name}" for p in matches)
        raise ValidationError(f"{ref!r} is ambiguous — use the id ({candidates})")
    return matches[0]


def merge_persons(conn: sqlalchemy.Connection, source_id: int, target_id: int, *, actor_id: int) -> int:
    """Merge `source` into `target` and re-point all references (DM-23).

    History is append-only, so `change_set.actor_id` is left alone. Returns the number of issue
    fields (assignee, reporter) that were re-pointed; each one is recorded in the issue history.
    """
    if source_id == target_id:
        raise ValidationError("cannot merge a person into itself")
    source = get_person(conn, source_id)
    target = get_person(conn, target_id)
    if source.merged_into_id is not None:
        raise ValidationError(f"person {source_id} is already merged")
    if target.merged_into_id is not None:
        raise ValidationError(f"person {target_id} is already merged")
    if source.is_me:
        raise ValidationError("the 'me' person cannot be merged into another person")

    now = utcnow_iso()
    params = {"source": source_id, "target": target_id}
    change_set_id = history.start_change_set(
        conn, "user", actor_id=actor_id, note=f"merged person {source_id} into {target_id}"
    )

    conn.execute(text("UPDATE person_identity SET person_id = :target WHERE person_id = :source"), params)
    conn.execute(text("UPDATE comment SET author_id = :target WHERE author_id = :source"), params)
    conn.execute(text("UPDATE project SET lead_id = :target WHERE lead_id = :source"), params)
    conn.execute(text("UPDATE extracted_item SET owner_id = :target WHERE owner_id = :source"), params)

    affected = conn.execute(
        text(
            "SELECT id, assignee_id, reporter_id FROM issue"
            " WHERE assignee_id = :source OR reporter_id = :source ORDER BY id"
        ),
        params,
    ).all()
    repointed = 0
    for issue_id, assignee_id, reporter_id in affected:
        for column, current in (("assignee", assignee_id), ("reporter", reporter_id)):
            if current != source_id:
                continue
            conn.execute(
                text(f"UPDATE issue SET {column}_id = :target, updated_at = :now WHERE id = :id"),
                {"target": target_id, "now": now, "id": issue_id},
            )
            history.record(conn, change_set_id, issue_id, column, "set", source_id, target_id)
            repointed += 1

    conn.execute(text("UPDATE person SET merged_into_id = :target WHERE id = :source"), params)
    return repointed
