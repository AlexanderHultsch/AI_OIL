from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ai_oil import timeutil
from ai_oil.cli.main import main
from ai_oil.domain import history, issues
from ai_oil.governance import audit

NOW = "2026-10-27T08:00:00.000Z"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC))


@pytest.fixture
def project(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("project", "add", "OIL", "Open issues") == 0
    assert oil("component", "add", "OIL", "Electronics") == 0
    assert oil("component", "add", "OIL", "Housing") == 0
    assert oil("person", "add", "Bob", "--email", "bob@example.com") == 0
    capsys.readouterr()


@pytest.mark.usefixtures("project")
def test_issue_create_prints_only_the_key(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("issue", "create", "-p", "oil", "-s", "Fit the filter", "-d", "Needs a new part") == 0

    assert capsys.readouterr().out == "OIL-1\n"
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        [entry] = audit.list_entries(conn, action_prefix="issue.")
    assert (issue.summary, issue.description, issue.issue_type, issue.priority, issue.status) == (
        "Fit the filter",
        "Needs a new part",
        "Task",
        "P4",
        "New",
    )
    assert issue.reporter_id == 1
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "issue.create",
        "B",
        "user",
        "OIL-1",
    )
    assert entry.detail["summary"] == "Fit the filter"


@pytest.mark.usefixtures("project")
def test_issue_create_with_all_options(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("issue", "create", "-p", "OIL", "-s", "Epic", "-d", "D", "--type", "Epic") == 0
    assert (
        oil(
            "issue",
            "create",
            "--project",
            "OIL",
            "--summary",
            "Child",
            "--description",
            "D",
            "--parent",
            "OIL-1",
            "--priority",
            "P2",
            "--assignee",
            "bob@example.com",
            "--reporter",
            "Bob",
            "--due",
            "2026-11-15",
            "--acceptance",
            "Part fits",
            "--label",
            "emc",
            "--label",
            "thermal",
            "--component",
            "Electronics",
            "--component",
            "Housing",
        )
        == 0
    )

    assert capsys.readouterr().out == "OIL-1\nOIL-2\n"
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-2")
        [entry] = audit.list_entries(conn, limit=1)
    assert (issue.parent_key, issue.priority, issue.assignee_id, issue.reporter_id) == ("OIL-1", "P2", 2, 2)
    assert (issue.original_due_date, issue.revised_due_date) == (date(2026, 11, 15), date(2026, 11, 15))
    assert issue.acceptance_criteria == "Part fits"
    assert issue.labels == ("emc", "thermal")
    assert issue.components == ("Electronics", "Housing")
    assert entry.detail["labels"] == ["emc", "thermal"]
    assert entry.detail["due"] == "2026-11-15"


@pytest.mark.usefixtures("project")
@pytest.mark.parametrize("value", ["tomorrow", "2026-13-45", "15.11.2026", "20261115"])
def test_bad_date_exits_with_two(oil, value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D", "--due", value)
    assert excinfo.value.code == 2
    assert "invalid date" in capsys.readouterr().err


@pytest.mark.usefixtures("project")
def test_edit_combines_fields_labels_components_and_status(
    oil, db, capsys: pytest.CaptureFixture[str]
) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "Old", "-d", "D")
    capsys.readouterr()

    assert (
        oil(
            "issue",
            "edit",
            "oil-1",
            "--summary",
            "New",
            "--priority",
            "P2",
            "--assignee",
            "Bob",
            "--due",
            "2026-12-01",
            "--add-label",
            "emc",
            "--add-label",
            "thermal",
            "--add-component",
            "Electronics",
            "--status",
            "Blocked",
            "--reason",
            "no samples",
        )
        == 0
    )

    assert capsys.readouterr().out == "Updated OIL-1\n"
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        entries = audit.list_entries(conn, action_prefix="issue.")
        events = history.issue_history(conn, issue.id)
    assert (issue.summary, issue.priority, issue.assignee_id, issue.status) == ("New", "P2", 2, "Blocked")
    assert issue.blocked_reason == "no samples"
    assert issue.revised_due_date == date(2026, 12, 1)
    assert issue.labels == ("emc", "thermal")
    assert issue.components == ("Electronics",)
    assert [e.action for e in entries] == ["issue.edit", "issue.create"]
    assert (entries[0].action_class, entries[0].actor_type, entries[0].target) == ("B", "user", "OIL-1")
    assert entries[0].detail == {
        "summary": "New",
        "priority": "P2",
        "assignee": "Bob",
        "due": "2026-12-01",
        "add_label": ["emc", "thermal"],
        "add_component": ["Electronics"],
        "status": "Blocked",
        "reason": "no samples",
    }
    assert [(e.field, e.op, e.old_value, e.new_value) for e in events] == [
        ("issue", "create", None, "OIL-1"),
        ("summary", "set", "Old", "New"),
        ("priority", "set", "P4", "P2"),
        ("assignee", "set", None, "2"),
        ("due_date", "set", None, "2026-12-01"),
        ("original_due_date", "set", None, "2026-12-01"),
        ("label", "add", None, "emc"),
        ("label", "add", None, "thermal"),
        ("component", "add", None, "Electronics"),
        ("status", "set", "New", "Blocked"),
        ("blocked_reason", "set", None, "no samples"),
    ]


@pytest.mark.usefixtures("project")
def test_edit_removes_labels_components_and_clears_optional_fields(
    oil, db, capsys: pytest.CaptureFixture[str]
) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D", "--assignee", "Bob", "--label", "emc")
    oil("issue", "create", "-p", "OIL", "-s", "Epic", "-d", "D", "--type", "Epic")
    oil("issue", "edit", "OIL-1", "--parent", "OIL-2", "--add-component", "Housing", "--snooze", "2026-11-30")
    capsys.readouterr()
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        assert (issue.parent_key, issue.snoozed_until) == ("OIL-2", date(2026, 11, 30))

    assert (
        oil(
            "issue",
            "edit",
            "OIL-1",
            "--assignee",
            "NONE",
            "--parent",
            "none",
            "--snooze",
            "none",
            "--remove-label",
            "emc",
            "--remove-component",
            "Housing",
        )
        == 0
    )

    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        entry = audit.list_entries(conn, limit=1)[0]
    assert issue.assignee_id is None
    assert issue.parent_id is None
    assert issue.snoozed_until is None
    assert issue.labels == ()
    assert issue.components == ()
    assert entry.detail == {
        "assignee": None,
        "parent": None,
        "snooze": None,
        "remove_label": ["emc"],
        "remove_component": ["Housing"],
    }


@pytest.mark.usefixtures("project")
def test_edit_reason_without_status(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D")
    capsys.readouterr()

    assert oil("issue", "edit", "OIL-1", "--reason", "because") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: --reason needs --status\n"


@pytest.mark.usefixtures("project")
def test_edit_with_nothing_to_change(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D")
    capsys.readouterr()

    assert oil("issue", "edit", "OIL-1") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: nothing to change\n"
    with db() as conn:
        assert [e.action for e in audit.list_entries(conn, action_prefix="issue.")] == ["issue.create"]


@pytest.mark.usefixtures("project")
def test_failed_edit_changes_nothing(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "Old", "-d", "D")
    capsys.readouterr()

    assert oil("issue", "edit", "OIL-1", "--summary", "New", "--add-label", "emc", "--status", "Blocked") == 1

    assert capsys.readouterr().err == "error: moving to Blocked requires a reason\n"
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        assert (issue.summary, issue.labels, issue.status) == ("Old", (), "New")
        assert [e.action for e in audit.list_entries(conn, action_prefix="issue.")] == ["issue.create"]
        assert len(history.issue_history(conn, issue.id)) == 1


@pytest.mark.usefixtures("project")
def test_edit_bad_snooze_date_exits_with_two(oil) -> None:
    with pytest.raises(SystemExit) as excinfo:
        oil("issue", "edit", "OIL-1", "--snooze", "soon")
    assert excinfo.value.code == 2


@pytest.mark.usefixtures("project")
def test_unknown_issue_is_an_error(oil, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["issue", "show", "OIL-99"],
        ["issue", "edit", "OIL-99", "--summary", "X"],
        ["issue", "edit", "OIL-99", "--add-label", "x"],
        ["issue", "edit", "OIL-99", "--status", "Done"],
    ):
        assert oil(*argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "error: issue OIL-99 not found\n"


@pytest.mark.usefixtures("project", "clock")
def test_show_full_layout(oil, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "Product launch", "-d", "D", "--type", "Epic")
    oil(
        "issue",
        "create",
        "-p",
        "OIL",
        "-s",
        "Fit the filter",
        "-d",
        "First line\n\nThird line",
        "--parent",
        "OIL-1",
        "--priority",
        "P2",
        "--assignee",
        "Bob",
        "--due",
        "2026-11-15",
        "--acceptance",
        "Part fits\nTest passed",
        "--label",
        "thermal",
        "--label",
        "emc",
        "--component",
        "Electronics",
    )
    oil("issue", "create", "-p", "OIL", "-s", "Order the part", "-d", "D", "--parent", "OIL-1")
    oil("issue-link", "OIL-2", "blocks", "OIL-3")
    oil("comment", "add", "OIL-2", "Asked the supplier\nstill waiting")
    oil("comment", "add", "OIL-2", "Deleted note")
    oil("comment", "delete", "2")
    oil("issue", "edit", "OIL-2", "--status", "Blocked", "--reason", "waiting for samples")
    capsys.readouterr()

    assert oil("issue", "show", "oil-2") == 0

    assert capsys.readouterr().out.splitlines() == [
        "OIL-2  Fit the filter",
        "Type: Task | Status: Blocked | Priority: P2",
        "Blocked reason: waiting for samples",
        "Assignee: Bob | Reporter: Alex",
        "Due: 2026-11-15 (original 2026-11-15)",
        "Parent: OIL-1",
        "Labels: emc, thermal",
        "Components: Electronics",
        f"Origin: manual | Created: {NOW} | Updated: {NOW}",
        "",
        "Description:",
        "  First line",
        "",
        "  Third line",
        "",
        "Acceptance criteria:",
        "  Part fits",
        "  Test passed",
        "",
        "Links:",
        "  blocks OIL-3  Order the part [New]",
        "",
        "Comments:",
        f"  #1 Alex {NOW}: Asked the supplier",
    ]


@pytest.mark.usefixtures("project", "clock")
def test_show_minimal_issue_omits_empty_lines(oil, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "Plain", "-d", "Just text")
    oil("issue", "create", "-p", "OIL", "-s", "Other", "-d", "D")
    oil("issue-link", "OIL-2", "relates_to", "OIL-1")
    capsys.readouterr()

    assert oil("issue", "show", "OIL-1") == 0

    assert capsys.readouterr().out.splitlines() == [
        "OIL-1  Plain",
        "Type: Task | Status: New | Priority: P4",
        "Assignee: - | Reporter: Alex",
        f"Origin: manual | Created: {NOW} | Updated: {NOW}",
        "",
        "Description:",
        "  Just text",
        "",
        "Links:",
        "  relates to OIL-2  Other [New]",
    ]


@pytest.mark.usefixtures("project", "clock")
def test_show_cancel_reason_and_history(oil, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "Old", "-d", "D")
    oil("issue", "edit", "OIL-1", "--summary", "New", "--assignee", "Bob", "--due", "2026-12-01")
    oil("comment", "add", "OIL-1", "Line one\nline two")
    oil("issue", "edit", "OIL-1", "--status", "Cancelled", "--reason", "not needed")
    capsys.readouterr()

    assert oil("issue", "show", "OIL-1", "--history") == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[:4] == [
        "OIL-1  New",
        "Type: Task | Status: Cancelled | Priority: P4",
        "Cancel reason: not needed",
        "Assignee: Bob | Reporter: Alex",
    ]
    assert lines[lines.index("History:") - 1] == ""
    assert lines[lines.index("History:") :] == [
        "History:",
        f"  {NOW} user issue create - -> OIL-1",
        f"  {NOW} user summary set Old -> New",
        f"  {NOW} user assignee set - -> 2",
        f"  {NOW} user due_date set - -> 2026-12-01",
        f"  {NOW} user original_due_date set - -> 2026-12-01",
        f"  {NOW} user comment add - -> Line one line two",
        f"  {NOW} user status set New -> Cancelled",
        f"  {NOW} user cancel_reason set - -> not needed",
        f"  {NOW} user resolved_at set - -> {NOW}",
    ]


@pytest.mark.usefixtures("project")
def test_show_without_history_flag_has_no_history_section(oil, capsys: pytest.CaptureFixture[str]) -> None:
    oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D")
    capsys.readouterr()
    assert oil("issue", "show", "OIL-1") == 0
    assert "History:" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["project", "list"],
        ["component", "list", "OIL"],
        ["issue", "show", "OIL-1"],
        ["issue", "create", "-p", "OIL", "-s", "S", "-d", "D"],
        ["comment", "add", "OIL-1", "Text"],
        ["issue-link", "OIL-1", "blocks", "OIL-2"],
        ["person", "list"],
    ],
)
def test_commands_before_init_report_the_config_error(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "never-initialised"

    assert main(["--home", str(missing), *argv]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: AI_OIL is not initialised at {missing} — run `oil init`\n"
    assert not missing.exists()
