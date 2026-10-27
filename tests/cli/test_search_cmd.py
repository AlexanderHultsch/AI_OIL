import json
from datetime import UTC, datetime

import pytest

from ai_oil import timeutil
from ai_oil.cli.main import main
from ai_oil.governance import audit

NOW = "2026-10-27T08:00:00.000Z"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC))


@pytest.fixture
def two_issues(oil, clock, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("project", "add", "OIL", "Open issues") == 0
    assert oil("person", "add", "Bob Builder", "--email", "bob@example.com") == 0
    assert oil("issue", "create", "-p", "OIL", "-s", "Plain", "-d", "D") == 0
    assert (
        oil(
            "issue",
            "create",
            "-p",
            "OIL",
            "-s",
            "Fit the filter",
            "-d",
            "D",
            "--priority",
            "P2",
            "--assignee",
            "Bob Builder",
            "--due",
            "2026-11-15",
        )
        == 0
    )
    assert oil("issue", "edit", "OIL-2", "--status", "In Progress") == 0
    capsys.readouterr()


@pytest.mark.usefixtures("two_issues")
def test_table_layout(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "project = OIL ORDER BY key") == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "KEY    TYPE  STATUS       PRIORITY  ASSIGNEE     DUE         SUMMARY",
        "OIL-1  Task  New          P4        -            -           Plain",
        "OIL-2  Task  In Progress  P2        Bob Builder  2026-11-15  Fit the filter",
        "2 issues",
    ]


@pytest.mark.usefixtures("two_issues")
def test_single_result_uses_the_singular(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "key = OIL-1") == 0

    assert capsys.readouterr().out.splitlines()[-1] == "1 issue"


@pytest.mark.usefixtures("two_issues")
def test_no_result_prints_only_the_count(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "status = Blocked") == 0

    captured = capsys.readouterr()
    assert captured.out == "0 issues\n"
    assert captured.err == ""


@pytest.mark.usefixtures("two_issues")
def test_limit_keeps_the_first_results(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "project = OIL ORDER BY key DESC", "--limit", "1") == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["KEY", "OIL-2", "1"]


@pytest.mark.usefixtures("two_issues")
@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_bad_limit_exits_with_two(oil, value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        oil("search", "project = OIL", "--limit", value)
    assert excinfo.value.code == 2
    assert "invalid limit" in capsys.readouterr().err


@pytest.mark.usefixtures("two_issues")
def test_json_output(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "project = OIL ORDER BY key", "--json") == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == [
        {
            "key": "OIL-1",
            "type": "Task",
            "status": "New",
            "priority": "P4",
            "assignee": None,
            "reporter": "Alex",
            "due": None,
            "summary": "Plain",
            "updated": NOW,
        },
        {
            "key": "OIL-2",
            "type": "Task",
            "status": "In Progress",
            "priority": "P2",
            "assignee": "Bob Builder",
            "reporter": "Alex",
            "due": "2026-11-15",
            "summary": "Fit the filter",
            "updated": NOW,
        },
    ]
    assert captured.out.startswith("[\n  {\n    ")


@pytest.mark.usefixtures("two_issues")
def test_json_without_results_is_an_empty_array(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "status = Blocked", "--json") == 0

    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.usefixtures("two_issues")
def test_json_keeps_non_ascii_text(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("issue", "edit", "OIL-1", "--summary", "Größe prüfen") == 0
    capsys.readouterr()

    assert oil("search", "key = OIL-1", "--json") == 0

    out = capsys.readouterr().out
    assert "Größe prüfen" in out


@pytest.mark.usefixtures("two_issues")
def test_syntax_error_prints_three_lines_on_stderr(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "status =") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: expected a value at position 8\nstatus =\n        ^\n"


@pytest.mark.usefixtures("two_issues")
def test_syntax_error_with_json_prints_nothing_on_stdout(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "status =", "--json") == 1

    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("two_issues")
def test_unknown_field_suggests_the_closest_name(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("search", "statu = New") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: unknown field 'statu' — did you mean 'status'? at position 0\nstatu = New\n^\n"
    )


@pytest.mark.usefixtures("two_issues")
def test_search_writes_no_audit_entry(oil, db) -> None:
    with db() as conn:
        before = len(audit.list_entries(conn, limit=1000))
    assert before > 0

    assert oil("search", "project = OIL") == 0
    assert oil("search", "project = OIL", "--json") == 0
    assert oil("search", "status =") == 1

    with db() as conn:
        assert len(audit.list_entries(conn, limit=1000)) == before


def test_search_before_init_reports_the_config_error(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "never-initialised"

    assert main(["--home", str(missing), "search", "project = OIL"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: AI_OIL is not initialised at {missing} — run `oil init`\n"
