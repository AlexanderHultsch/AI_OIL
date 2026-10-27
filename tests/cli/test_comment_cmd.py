import pytest

from ai_oil.domain import comments, history, issues
from ai_oil.governance import audit


@pytest.fixture(autouse=True)
def issue(oil, capsys: pytest.CaptureFixture[str]) -> None:
    oil("project", "add", "OIL", "Open issues")
    oil("issue", "create", "-p", "OIL", "-s", "Summary", "-d", "Description")
    capsys.readouterr()


def test_comment_add(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("comment", "add", "oil-1", "Called the supplier") == 0

    assert capsys.readouterr().out == "Added comment #1 to OIL-1\n"
    with db() as conn:
        [comment] = comments.list_comments(conn, "OIL-1")
        [entry] = audit.list_entries(conn, action_prefix="comment.")
    assert comment.body == "Called the supplier"
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "comment.add",
        "B",
        "user",
        "OIL-1",
    )
    assert entry.detail == {"comment_id": 1, "text": "Called the supplier"}


def test_comment_edit(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("comment", "add", "OIL-1", "First")
    capsys.readouterr()

    assert oil("comment", "edit", "1", "Second") == 0

    assert capsys.readouterr().out == "Updated comment #1\n"
    with db() as conn:
        comment = comments.get_comment(conn, 1)
        entries = audit.list_entries(conn, action_prefix="comment.")
    assert comment.body == "Second"
    assert comment.edited_at is not None
    assert [e.action for e in entries] == ["comment.edit", "comment.add"]
    assert entries[0].target == "OIL-1"


def test_comment_delete(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("comment", "add", "OIL-1", "First")
    capsys.readouterr()

    assert oil("comment", "delete", "1") == 0

    assert capsys.readouterr().out == "Deleted comment #1\n"
    with db() as conn:
        assert comments.list_comments(conn, "OIL-1") == []
        assert comments.get_comment(conn, 1).deleted_at is not None
        entries = audit.list_entries(conn, action_prefix="comment.")
        events = history.issue_history(conn, issues.get_issue(conn, "OIL-1").id)
    assert [e.action for e in entries] == ["comment.delete", "comment.add"]
    assert entries[0].detail == {"comment_id": 1}
    assert [(e.field, e.op) for e in events] == [
        ("issue", "create"),
        ("comment", "add"),
        ("comment", "delete"),
    ]


def test_comment_errors(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("comment", "add", "OIL-99", "Text") == 1
    assert capsys.readouterr().err == "error: issue OIL-99 not found\n"
    assert oil("comment", "edit", "7", "Text") == 1
    assert capsys.readouterr().err == "error: comment 7 not found\n"
    assert oil("comment", "delete", "7") == 1
    assert capsys.readouterr().err == "error: comment 7 not found\n"
    assert oil("comment", "add", "OIL-1", "   ") == 1
    assert capsys.readouterr().err == "error: comment body must not be blank (DM-19)\n"
    with db() as conn:
        assert audit.list_entries(conn, action_prefix="comment.") == []


def test_comment_id_must_be_a_number(oil) -> None:
    with pytest.raises(SystemExit) as excinfo:
        oil("comment", "delete", "abc")
    assert excinfo.value.code == 2
