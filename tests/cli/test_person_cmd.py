import pytest

from ai_oil.domain import issues, people
from ai_oil.governance import audit


def test_person_list_shows_me_and_identities(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("person", "list") == 0

    assert capsys.readouterr().out == "1  Alex *me*\n    email: alex@example.com\n"


def test_person_add(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        oil(
            "person",
            "add",
            "Bob Builder",
            "--org",
            "Visteon",
            "--email",
            "Bob@Example.com",
            "--email",
            "bob2@example.com",
            "--atlassian",
            "acc-7",
        )
        == 0
    )

    assert capsys.readouterr().out == "Created person #2 Bob Builder\n"
    assert oil("person", "list") == 0
    assert capsys.readouterr().out.splitlines() == [
        "1  Alex *me*",
        "    email: alex@example.com",
        "2  Bob Builder (Visteon)",
        "    atlassian: acc-7",
        "    email: bob2@example.com",
        "    email: bob@example.com",
    ]
    with db() as conn:
        [entry] = audit.list_entries(conn, action_prefix="person.")
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "person.create",
        "B",
        "user",
        "2",
    )
    assert entry.detail == {
        "name": "Bob Builder",
        "organisation": "Visteon",
        "emails": ["Bob@Example.com", "bob2@example.com"],
        "atlassian": "acc-7",
    }


def test_person_add_without_identities(oil, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("person", "add", "Carol") == 0
    assert capsys.readouterr().out == "Created person #2 Carol\n"


def test_person_add_with_taken_email_is_rolled_back(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    assert oil("person", "add", "Dup", "--email", "alex@example.com") == 1

    assert capsys.readouterr().err.startswith("error: email identity 'alex@example.com' already belongs to")
    with db() as conn:
        assert [p.display_name for p in people.list_persons(conn)] == ["Alex"]
        assert audit.list_entries(conn, action_prefix="person.") == []


def test_person_merge_repoints_and_hides_merged(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("person", "add", "Bob Builder", "--org", "Visteon", "--email", "bob@example.com")
    oil("person", "add", "Robert", "--email", "robert@example.com")
    oil("project", "add", "OIL", "Open issues")
    oil("issue", "create", "-p", "OIL", "-s", "S", "-d", "D", "--assignee", "Robert", "--reporter", "Robert")
    capsys.readouterr()

    assert oil("person", "merge", "Robert", "Bob Builder") == 0

    assert capsys.readouterr().out == "Merged #3 into #2 (2 issue fields re-pointed)\n"
    with db() as conn:
        issue = issues.get_issue(conn, "OIL-1")
        [entry] = audit.list_entries(conn, action_prefix="person.merge")
    assert (issue.assignee_id, issue.reporter_id) == (2, 2)
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "3")
    assert entry.detail == {"source_id": 3, "target_id": 2, "issue_fields_repointed": 2}

    assert oil("person", "list") == 0
    assert capsys.readouterr().out.splitlines() == [
        "1  Alex *me*",
        "    email: alex@example.com",
        "2  Bob Builder (Visteon)",
        "    email: bob@example.com",
        "    email: robert@example.com",
    ]
    assert oil("person", "list", "--all") == 0
    assert capsys.readouterr().out.splitlines() == [
        "1  Alex *me*",
        "    email: alex@example.com",
        "2  Bob Builder (Visteon)",
        "    email: bob@example.com",
        "    email: robert@example.com",
        "3  Robert (merged into 2)",
    ]


def test_person_merge_errors(oil, db, capsys: pytest.CaptureFixture[str]) -> None:
    oil("person", "add", "Bob")
    capsys.readouterr()

    assert oil("person", "merge", "me", "Bob") == 1
    assert capsys.readouterr().err == "error: the 'me' person cannot be merged into another person\n"
    assert oil("person", "merge", "Bob", "Bob") == 1
    assert capsys.readouterr().err == "error: cannot merge a person into itself\n"
    assert oil("person", "merge", "Nobody", "Bob") == 1
    assert capsys.readouterr().err == "error: no person named 'Nobody'\n"
    with db() as conn:
        assert audit.list_entries(conn, action_prefix="person.merge") == []
