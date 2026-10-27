import json

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil.domain import people
from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.search import compiler, oql, saved_filters
from ai_oil.search.saved_filters import BUILT_INS, COLUMNS, DEFAULT_COLUMNS


@pytest.fixture(autouse=True)
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex Hultsch", is_me=True)


def names(conn: sqlalchemy.Connection) -> list[str]:
    return [f.name for f in saved_filters.list_filters(conn)]


def test_built_ins_are_created_once_and_listed_first_in_order(conn: sqlalchemy.Connection) -> None:
    saved_filters.create_filter(conn, "apples", "status = Blocked")
    saved_filters.create_filter(conn, "Zebra", "status = Blocked")
    saved_filters.create_filter(conn, "Banana", "status = Blocked")

    first = saved_filters.list_filters(conn)
    saved_filters.ensure_built_ins(conn)
    again = saved_filters.list_filters(conn)

    assert first == again
    assert [f.name for f in first] == [*BUILT_INS, "apples", "Banana", "Zebra"]
    assert [f.built_in for f in first] == [True] * 6 + [False] * 3
    assert conn.execute(text("SELECT COUNT(*) FROM saved_filter")).scalar_one() == 9


def test_built_in_definitions(conn: sqlalchemy.Connection) -> None:
    assert list(BUILT_INS) == [
        "All open issues",
        "My open issues",
        "Overdue",
        "Blocked",
        "Updated by sources in the last 24 h",
        "Due this week",
    ]
    for found in saved_filters.list_filters(conn):
        assert found.query == BUILT_INS[found.name]
        assert found.columns == DEFAULT_COLUMNS
        assert found.is_favourite is True


def test_every_built_in_query_compiles(conn: sqlalchemy.Connection) -> None:
    for name, query in BUILT_INS.items():
        compiler.compile_query(conn, query)
        assert name


def test_default_columns_are_known_columns() -> None:
    assert list(COLUMNS) == [
        "key",
        "type",
        "summary",
        "status",
        "priority",
        "assignee",
        "reporter",
        "due",
        "updated",
        "created",
        "labels",
        "sources",
        "source_changed",
    ]
    assert set(DEFAULT_COLUMNS) <= set(COLUMNS)
    assert COLUMNS["source_changed"] == "Source changed"


def test_ensure_built_ins_restores_a_missing_one_and_keeps_edits(conn: sqlalchemy.Connection) -> None:
    saved_filters.ensure_built_ins(conn)
    saved_filters.update_filter(conn, "Blocked", columns=["key", "status"])
    conn.execute(text("DELETE FROM saved_filter WHERE name = 'Overdue'"))

    saved_filters.ensure_built_ins(conn)

    assert "Overdue" in names(conn)
    assert saved_filters.get_filter(conn, "Blocked").columns == ("key", "status")


def test_create_and_get_by_id_and_name(conn: sqlalchemy.Connection) -> None:
    created = saved_filters.create_filter(conn, "  Mine  ", " priority = P1 ", ["key", "summary"])

    assert created.name == "Mine"
    assert created.query == "priority = P1"
    assert created.columns == ("key", "summary")
    assert created.is_favourite is False
    assert created.built_in is False
    assert saved_filters.get_filter(conn, created.id) == created
    assert saved_filters.get_filter(conn, str(created.id)) == created
    assert saved_filters.get_filter(conn, "Mine") == created
    stored = conn.execute(text("SELECT columns_json FROM saved_filter WHERE id = :i"), {"i": created.id})
    assert json.loads(stored.scalar_one()) == ["key", "summary"]


def test_columns_default(conn: sqlalchemy.Connection) -> None:
    assert saved_filters.create_filter(conn, "x", "priority = P1").columns == DEFAULT_COLUMNS


def test_get_unknown_filter(conn: sqlalchemy.Connection) -> None:
    for ref in (999, "999", "nope"):
        with pytest.raises(NotFoundError):
            saved_filters.get_filter(conn, ref)


def test_create_validation(conn: sqlalchemy.Connection) -> None:
    saved_filters.create_filter(conn, "Mine", "priority = P1")

    with pytest.raises(ValidationError):
        saved_filters.create_filter(conn, "   ", "priority = P1")
    with pytest.raises(ConflictError):
        saved_filters.create_filter(conn, "Mine", "priority = P2")
    with pytest.raises(ConflictError):
        saved_filters.create_filter(conn, "Blocked", "priority = P2")
    with pytest.raises(oql.OqlSyntaxError):
        saved_filters.create_filter(conn, "Broken", "priority = P9")
    with pytest.raises(oql.OqlSyntaxError):
        saved_filters.create_filter(conn, "Broken", "nonsense field")
    with pytest.raises(ValidationError, match="unknown column 'colour'"):
        saved_filters.create_filter(conn, "Broken", "priority = P1", ["key", "colour"])
    assert "Broken" not in names(conn)


def test_update(conn: sqlalchemy.Connection) -> None:
    created = saved_filters.create_filter(conn, "Mine", "priority = P1")

    updated = saved_filters.update_filter(
        conn, created.id, name="Renamed", query="priority = P2", columns=["key"], is_favourite=True
    )

    assert (updated.id, updated.name, updated.query, updated.columns, updated.is_favourite) == (
        created.id,
        "Renamed",
        "priority = P2",
        ("key",),
        True,
    )
    assert saved_filters.update_filter(conn, "Renamed", is_favourite=False).is_favourite is False
    assert saved_filters.update_filter(conn, "Renamed") == saved_filters.get_filter(conn, "Renamed")
    assert saved_filters.update_filter(conn, "Renamed", name="Renamed").name == "Renamed"


def test_update_validation(conn: sqlalchemy.Connection) -> None:
    saved_filters.create_filter(conn, "Other", "priority = P1")
    created = saved_filters.create_filter(conn, "Mine", "priority = P1")

    with pytest.raises(ValidationError):
        saved_filters.update_filter(conn, created.id, name=" ")
    with pytest.raises(ConflictError):
        saved_filters.update_filter(conn, created.id, name="Other")
    with pytest.raises(oql.OqlSyntaxError):
        saved_filters.update_filter(conn, created.id, query="priority = P9")
    with pytest.raises(ValidationError):
        saved_filters.update_filter(conn, created.id, columns=["nope"])
    with pytest.raises(NotFoundError):
        saved_filters.update_filter(conn, 999, name="x")
    assert saved_filters.get_filter(conn, created.id) == created


def test_delete(conn: sqlalchemy.Connection) -> None:
    created = saved_filters.create_filter(conn, "Mine", "priority = P1")

    saved_filters.delete_filter(conn, created.id)

    with pytest.raises(NotFoundError):
        saved_filters.get_filter(conn, created.id)
    with pytest.raises(NotFoundError):
        saved_filters.delete_filter(conn, created.id)


def test_built_ins_are_protected(conn: sqlalchemy.Connection) -> None:
    saved_filters.ensure_built_ins(conn)
    blocked = saved_filters.get_filter(conn, "Blocked")

    with pytest.raises(ValidationError):
        saved_filters.update_filter(conn, "Blocked", name="Stuck")
    with pytest.raises(ValidationError):
        saved_filters.update_filter(conn, "Blocked", query="status = Done")
    with pytest.raises(ValidationError):
        saved_filters.delete_filter(conn, "Blocked")
    assert saved_filters.get_filter(conn, blocked.id) == blocked


def test_built_in_columns_and_favourite_flag_can_change(conn: sqlalchemy.Connection) -> None:
    saved_filters.ensure_built_ins(conn)

    updated = saved_filters.update_filter(conn, "Blocked", columns=["key", "summary"], is_favourite=False)

    assert updated.columns == ("key", "summary")
    assert updated.is_favourite is False
    assert updated.built_in is True
    assert updated.query == BUILT_INS["Blocked"]
