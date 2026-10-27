import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import history, people
from ai_oil.errors import ConflictError, NotFoundError, ValidationError


def test_create_and_get_person(conn: sqlalchemy.Connection) -> None:
    created = people.create_person(conn, " Sam ", organisation="ACME", notes="n")
    assert created == people.Person(created.id, "Sam", "ACME", False, None, "n")
    assert people.get_person(conn, created.id) == created
    with pytest.raises(NotFoundError):
        people.get_person(conn, 999)


def test_blank_name_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        people.create_person(conn, "  ")


def test_me(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(NotFoundError, match="oil init"):
        people.get_me(conn)
    me = people.create_person(conn, "Alex", is_me=True)
    assert me.is_me is True
    assert people.get_me(conn) == me
    with pytest.raises(ConflictError):
        people.create_person(conn, "Other", is_me=True)


def test_normalize_external_id() -> None:
    assert people.normalize_external_id("email", "  Alex@Example.COM ") == "alex@example.com"
    assert people.normalize_external_id("atlassian", " AbC-1 ") == "AbC-1"
    assert people.normalize_external_id("teams", " U1 ") == "U1"
    with pytest.raises(ValidationError):
        people.normalize_external_id("slack", "x")


def test_add_identity_is_idempotent(conn: sqlalchemy.Connection) -> None:
    person = people.create_person(conn, "Sam")
    first = people.add_identity(conn, person.id, "email", "Sam@Example.com", "Sam S.")
    again = people.add_identity(conn, person.id, "email", "sam@example.com")
    assert first == again
    assert first.external_id == "sam@example.com"
    assert first.display_name == "Sam S."
    assert people.list_identities(conn, person.id) == [first]


def test_add_identity_conflict_names_owner(conn: sqlalchemy.Connection) -> None:
    owner = people.create_person(conn, "Sam")
    other = people.create_person(conn, "Pat")
    people.add_identity(conn, owner.id, "email", "sam@example.com")
    with pytest.raises(ConflictError, match=f"{owner.id}: Sam"):
        people.add_identity(conn, other.id, "email", "SAM@example.com")


def test_add_identity_unknown_person(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(NotFoundError):
        people.add_identity(conn, 999, "email", "x@example.com")


def test_list_identities_ordered(conn: sqlalchemy.Connection) -> None:
    person = people.create_person(conn, "Sam")
    people.add_identity(conn, person.id, "teams", "t1")
    people.add_identity(conn, person.id, "email", "b@example.com")
    people.add_identity(conn, person.id, "email", "a@example.com")
    assert [(i.system, i.external_id) for i in people.list_identities(conn, person.id)] == [
        ("email", "a@example.com"),
        ("email", "b@example.com"),
        ("teams", "t1"),
    ]


def test_resolve_identity_creates_once(conn: sqlalchemy.Connection) -> None:
    first = people.resolve_identity(conn, "email", "New@Example.com", "New Person")
    again = people.resolve_identity(conn, "email", "new@example.com")
    assert again == first
    assert first.display_name == "New Person"
    assert people.get_person(conn, first.person_id).display_name == "New Person"
    assert len(people.list_persons(conn)) == 1


def test_resolve_identity_without_name_uses_external_id(conn: sqlalchemy.Connection) -> None:
    identity = people.resolve_identity(conn, "atlassian", "acc-9")
    assert people.get_person(conn, identity.person_id).display_name == "acc-9"
    assert identity.display_name is None


def test_resolve_identity_updates_display_name(conn: sqlalchemy.Connection) -> None:
    first = people.resolve_identity(conn, "email", "a@example.com", "Old")
    updated = people.resolve_identity(conn, "email", "a@example.com", "New")
    assert updated.id == first.id
    assert updated.display_name == "New"
    assert people.list_identities(conn, first.person_id)[0].display_name == "New"
    assert people.resolve_identity(conn, "email", "a@example.com", "  ").display_name == "New"


def test_lookup_person(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    sam = people.create_person(conn, "Sam Smith")
    people.add_identity(conn, sam.id, "email", "sam@example.com")
    first_pat = people.create_person(conn, "Pat")
    second_pat = people.create_person(conn, "pat")

    assert people.lookup_person(conn, "ME") == me
    assert people.lookup_person(conn, str(sam.id)) == sam
    assert people.lookup_person(conn, "Sam@Example.com") == sam
    assert people.lookup_person(conn, "sam smith") == sam
    with pytest.raises(ValidationError, match=f"{first_pat.id}: Pat.*{second_pat.id}: pat"):
        people.lookup_person(conn, "PAT")
    with pytest.raises(NotFoundError):
        people.lookup_person(conn, "nobody")
    with pytest.raises(NotFoundError):
        people.lookup_person(conn, "nobody@example.com")
    with pytest.raises(NotFoundError):
        people.lookup_person(conn, "999")


def _seed_work(conn: sqlalchemy.Connection, source: int, target: int) -> None:
    conn.execute(
        text("INSERT INTO project(id, key, name, lead_id) VALUES (1, 'OIL', 'Open issues', :s)"),
        {"s": source},
    )
    rows = [
        (1, 1, "OIL-1", source, target),  # assignee moves
        (2, 2, "OIL-2", target, source),  # reporter moves
        (3, 3, "OIL-3", source, source),  # both move
        (4, 4, "OIL-4", target, target),  # untouched
    ]
    for issue_id, number, key, assignee, reporter in rows:
        conn.execute(
            text(
                "INSERT INTO issue(id, project_id, number, key, summary, description, assignee_id,"
                " reporter_id, updated_at)"
                " VALUES (:id, 1, :n, :key, 's', 'd', :a, :r, '2000-01-01T00:00:00.000Z')"
            ),
            {"id": issue_id, "n": number, "key": key, "a": assignee, "r": reporter},
        )
    conn.execute(
        text("INSERT INTO comment(issue_id, author_id, body) VALUES (1, :s, 'hello')"), {"s": source}
    )
    conn.execute(text("INSERT INTO connector(id, type, name) VALUES (1, 'manual', 'Manual')"))
    conn.execute(
        text(
            "INSERT INTO source_object(id, connector_id, object_type, external_id)"
            " VALUES (1, 1, 'note', 'n1')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO extracted_item(kind, source_object_id, text, owner_id)"
            " VALUES ('action_item', 1, 'do it', :s)"
        ),
        {"s": source},
    )


def test_merge_repoints_references(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    target = people.create_person(conn, "Samuel Smith")
    source = people.create_person(conn, "Sam")
    people.add_identity(conn, source.id, "email", "sam@example.com")
    people.add_identity(conn, target.id, "email", "samuel@example.com")
    _seed_work(conn, source.id, target.id)

    assert people.merge_persons(conn, source.id, target.id, actor_id=me.id) == 4

    assert {i.external_id for i in people.list_identities(conn, target.id)} == {
        "sam@example.com",
        "samuel@example.com",
    }
    assert people.list_identities(conn, source.id) == []
    assert people.get_person(conn, source.id).merged_into_id == target.id
    assert conn.execute(text("SELECT assignee_id, reporter_id FROM issue ORDER BY id")).all() == [
        (target.id, target.id),
        (target.id, target.id),
        (target.id, target.id),
        (target.id, target.id),
    ]
    assert conn.execute(text("SELECT author_id FROM comment")).scalar_one() == target.id
    assert conn.execute(text("SELECT lead_id FROM project")).scalar_one() == target.id
    assert conn.execute(text("SELECT owner_id FROM extracted_item")).scalar_one() == target.id
    stale = conn.execute(text("SELECT id FROM issue WHERE updated_at = '2000-01-01T00:00:00.000Z'")).all()
    assert [row.id for row in stale] == [4]

    assert [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, 1)] == [
        ("assignee", "set", str(source.id), str(target.id))
    ]
    assert [e.field for e in history.issue_history(conn, 2)] == ["reporter"]
    assert [e.field for e in history.issue_history(conn, 3)] == ["assignee", "reporter"]
    assert history.issue_history(conn, 4) == []
    entry = history.issue_history(conn, 1)[0]
    assert (entry.actor_type, entry.actor_id) == ("user", me.id)
    assert entry.note == f"merged person {source.id} into {target.id}"
    assert conn.execute(text("SELECT count(*) FROM change_set")).scalar_one() == 1


def test_merge_does_not_touch_history_actor(conn: sqlalchemy.Connection) -> None:
    target = people.create_person(conn, "Target")
    source = people.create_person(conn, "Source")
    me = people.create_person(conn, "Alex", is_me=True)
    earlier = history.start_change_set(conn, "user", actor_id=source.id)

    people.merge_persons(conn, source.id, target.id, actor_id=me.id)

    assert (
        conn.execute(text("SELECT actor_id FROM change_set WHERE id = :id"), {"id": earlier}).scalar_one()
        == source.id
    )


def test_find_person_by_identity_follows_merge(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    target = people.create_person(conn, "Target")
    source = people.create_person(conn, "Source")
    people.add_identity(conn, source.id, "email", "source@example.com")
    assert people.find_person_by_identity(conn, "email", "SOURCE@example.com") == source

    people.merge_persons(conn, source.id, target.id, actor_id=me.id)

    found = people.find_person_by_identity(conn, "email", "source@example.com")
    assert found is not None
    assert found.id == target.id
    assert people.find_person_by_identity(conn, "email", "unknown@example.com") is None
    assert people.lookup_person(conn, "source@example.com").id == target.id


def test_merge_into_me_is_allowed(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    duplicate = people.create_person(conn, "Alex H.")
    people.merge_persons(conn, duplicate.id, me.id, actor_id=me.id)
    assert people.get_person(conn, duplicate.id).merged_into_id == me.id


def test_merge_rejections(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    a = people.create_person(conn, "A")
    b = people.create_person(conn, "B")
    c = people.create_person(conn, "C")
    with pytest.raises(ValidationError):
        people.merge_persons(conn, a.id, a.id, actor_id=me.id)
    with pytest.raises(ValidationError, match="me"):
        people.merge_persons(conn, me.id, a.id, actor_id=me.id)
    people.merge_persons(conn, a.id, b.id, actor_id=me.id)
    with pytest.raises(ValidationError, match="already merged"):
        people.merge_persons(conn, a.id, c.id, actor_id=me.id)
    with pytest.raises(ValidationError, match="already merged"):
        people.merge_persons(conn, c.id, a.id, actor_id=me.id)
    with pytest.raises(NotFoundError):
        people.merge_persons(conn, 999, c.id, actor_id=me.id)


def test_list_persons_excludes_merged_by_default(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    people.create_person(conn, "bob")
    carol = people.create_person(conn, "Carol")
    dup = people.create_person(conn, "Alice")
    people.create_person(conn, "Bob")
    people.merge_persons(conn, dup.id, carol.id, actor_id=me.id)

    assert [p.display_name for p in people.list_persons(conn)] == ["Alex", "bob", "Bob", "Carol"]
    assert [p.display_name for p in people.list_persons(conn, include_merged=True)] == [
        "Alex",
        "Alice",
        "bob",
        "Bob",
        "Carol",
    ]


def test_update_person_changes_name_and_organisation(conn: sqlalchemy.Connection) -> None:
    person = people.create_person(conn, "Sam", organisation="ACME", notes="n")

    renamed = people.update_person(conn, person.id, display_name=" Samuel ")
    assert renamed == people.Person(person.id, "Samuel", "ACME", False, None, "n")

    moved = people.update_person(conn, person.id, organisation="Globex")
    assert moved == people.Person(person.id, "Samuel", "Globex", False, None, "n")
    assert people.update_person(conn, person.id) == moved


def test_update_person_rejects_blank_name_and_unknown_person(conn: sqlalchemy.Connection) -> None:
    person = people.create_person(conn, "Sam")
    with pytest.raises(ValidationError):
        people.update_person(conn, person.id, display_name="  ")
    assert people.get_person(conn, person.id).display_name == "Sam"
    with pytest.raises(NotFoundError):
        people.update_person(conn, 999, display_name="X")
