import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_oil.errors import ValidationError
from ai_oil.governance import audit


def test_log_returns_increasing_ids_and_list_is_newest_first(conn: sqlalchemy.Connection) -> None:
    first = audit.log(
        conn, "source.fetch", "A", actor_type="sync", target="mail:1", detail={"count": 3, "ok": True}
    )
    second = audit.log(conn, "issue.create", "B", actor_type="user", approval_id=None)

    assert second > first

    entries = audit.list_entries(conn)

    assert [e.id for e in entries] == [second, first]
    assert entries[1] == audit.AuditEntry(
        id=first,
        at=entries[1].at,
        actor_type="sync",
        action="source.fetch",
        action_class="A",
        target="mail:1",
        detail={"count": 3, "ok": True},
        approval_id=None,
    )
    assert entries[1].at.endswith("Z")
    assert entries[0].detail == {}


def test_action_prefix_filter(conn: sqlalchemy.Connection) -> None:
    audit.log(conn, "source.fetch", "A", actor_type="sync")
    audit.log(conn, "issue.create", "B", actor_type="user")

    entries = audit.list_entries(conn, action_prefix="source.")

    assert [e.action for e in entries] == ["source.fetch"]


def test_limit_respected(conn: sqlalchemy.Connection) -> None:
    for _ in range(5):
        audit.log(conn, "model.call", "A", actor_type="ai")

    assert len(audit.list_entries(conn, limit=2)) == 2


def test_invalid_actor_type_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        audit.log(conn, "source.fetch", "A", actor_type="robot")


def test_invalid_action_class_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        audit.log(conn, "source.fetch", "Z", actor_type="sync")


def test_blank_action_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        audit.log(conn, "   ", "A", actor_type="sync")


@pytest.mark.parametrize("key", ["api_token", "Password", "client_secret"])
def test_secret_detail_keys_rejected(conn: sqlalchemy.Connection, key: str) -> None:
    with pytest.raises(ValidationError, match="GOV-8"):
        audit.log(conn, "jira.comment", "E", actor_type="user", detail={key: "value"})
    assert audit.list_entries(conn) == []


def test_update_and_delete_rejected_by_trigger(conn: sqlalchemy.Connection) -> None:
    audit.log(conn, "source.fetch", "A", actor_type="sync")

    with pytest.raises(IntegrityError, match="append-only"):
        conn.execute(text("UPDATE audit_log SET action = 'x'"))
    with pytest.raises(IntegrityError, match="append-only"):
        conn.execute(text("DELETE FROM audit_log"))
