from pathlib import Path

import pytest

from ai_oil.cli.main import main
from ai_oil.domain import history, issue_links, issues
from ai_oil.governance import audit


def seed_two_issues(path: Path) -> None:
    main(["--home", str(path), "project", "add", "OIL", "Open issues"])
    main(["--home", str(path), "issue", "create", "-p", "OIL", "-s", "First", "-d", "Description"])
    main(["--home", str(path), "issue", "create", "-p", "OIL", "-s", "Second", "-d", "Description"])


@pytest.fixture
def home_seed():
    return "issue-link-cmd-two-issues", seed_two_issues


def test_issue_link_add(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("issue-link", "oil-1", "blocks", "OIL-2") == 0

    assert capsys.readouterr().out == "Linked OIL-1 blocks OIL-2\n"
    with db() as conn:
        [link] = issue_links.list_issue_links(conn, "OIL-1")
        [other] = issue_links.list_issue_links(conn, "OIL-2")
        [entry] = audit.list_entries(conn, action_prefix="issue_link.")
        events = history.issue_history(conn, issues.get_issue(conn, "OIL-2").id)
    assert (link.label, link.other_key) == ("blocks", "OIL-2")
    assert (other.label, other.other_key) == ("is blocked by", "OIL-1")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue_link.add",
        "B",
        "user",
        "OIL-1",
    )
    assert entry.detail == {"from": "OIL-1", "type": "blocks", "to": "OIL-2", "link_id": link.id}
    assert [(e.field, e.op, e.new_value) for e in events][-1] == (
        "issue_link",
        "add",
        "is blocked by OIL-1",
    )


def test_issue_link_remove(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue-link", "OIL-1", "relates_to", "OIL-2")
    capsys.readouterr()

    assert oil("issue-link", "OIL-1", "relates_to", "OIL-2", "--remove") == 0

    assert capsys.readouterr().out == "Removed link OIL-1 relates_to OIL-2\n"
    with db() as conn:
        assert issue_links.list_issue_links(conn, "OIL-1") == []
        entries = audit.list_entries(conn, action_prefix="issue_link.")
    assert [e.action for e in entries] == ["issue_link.remove", "issue_link.add"]
    assert entries[0].target == "OIL-1"
    assert entries[0].detail == {"from": "OIL-1", "type": "relates_to", "to": "OIL-2"}


def test_issue_link_errors_write_no_audit_entry(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue-link", "OIL-1", "blocks", "OIL-2")
    capsys.readouterr()

    assert oil("issue-link", "OIL-1", "blocks", "OIL-2") == 1
    assert capsys.readouterr().err == "error: OIL-1 blocks OIL-2 already exists\n"
    assert oil("issue-link", "OIL-1", "blocks", "OIL-99") == 1
    assert capsys.readouterr().err == "error: issue OIL-99 not found\n"
    assert oil("issue-link", "OIL-2", "duplicates", "OIL-1", "--remove") == 1
    assert capsys.readouterr().err == "error: no duplicates link from OIL-2 to OIL-1\n"

    with db() as conn:
        assert [e.action for e in audit.list_entries(conn, action_prefix="issue_link.")] == ["issue_link.add"]


def test_issue_link_type_is_restricted(oil) -> None:
    with pytest.raises(SystemExit) as excinfo:
        oil("issue-link", "OIL-1", "fixes", "OIL-2")
    assert excinfo.value.code == 2
