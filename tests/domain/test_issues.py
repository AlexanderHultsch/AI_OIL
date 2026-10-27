from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.domain import history, issues, people, projects
from ai_oil.errors import NotFoundError, TransitionError, ValidationError

T0 = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def me(conn: sqlalchemy.Connection) -> people.Person:
    return people.create_person(conn, "Alex", is_me=True)


@pytest.fixture
def oil(conn: sqlalchemy.Connection, me: people.Person) -> projects.Project:
    project = projects.create_project(conn, "OIL", "Open issues")
    projects.create_component(conn, project.id, "Electronics")
    return project


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    state = {"now": T0}
    monkeypatch.setattr(timeutil, "_now", lambda: state["now"])

    def advance(**delta: int) -> None:
        state["now"] = state["now"] + timedelta(**delta)

    return advance


def make(conn: sqlalchemy.Connection, me: people.Person, summary: str = "Summary", **kwargs) -> issues.Issue:
    return issues.create_issue(conn, "OIL", summary, "Description", actor_id=me.id, **kwargs)


def events(conn: sqlalchemy.Connection, issue: issues.Issue) -> list[tuple[str, str, str | None, str | None]]:
    return [(e.field, e.op, e.old_value, e.new_value) for e in history.issue_history(conn, issue.id)]


def change_set_count(conn: sqlalchemy.Connection) -> int:
    return conn.execute(text("SELECT count(*) FROM change_set")).scalar_one()


def in_state(conn: sqlalchemy.Connection, me: people.Person, status: str) -> issues.Issue:
    issue = make(conn, me)
    paths = {
        "New": [],
        "Ready to Proceed": ["Ready to Proceed"],
        "In Progress": ["In Progress"],
        "Blocked": ["Blocked"],
        "Done": ["Done"],
        "Cancelled": ["Cancelled"],
    }
    for step in paths[status]:
        issue = issues.transition_issue(conn, issue.key, step, actor_id=me.id, reason="because")
    return issue


# --- constants -----------------------------------------------------------------------------------


def test_status_category_matches_spec() -> None:
    assert issues.STATUS_CATEGORY == {
        "New": "To Do",
        "Ready to Proceed": "To Do",
        "In Progress": "In Progress",
        "Blocked": "In Progress",
        "Done": "Done",
        "Cancelled": "Done",
    }
    assert set(issues.STATUS_CATEGORY) == set(issues.STATUSES) == set(issues.TRANSITIONS)


def test_issue_exposes_status_category(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = in_state(conn, me, "Cancelled")
    assert (issue.status, issue.status_category) == ("Cancelled", "Done")


# --- create --------------------------------------------------------------------------------------


def test_create_defaults_and_key_sequence(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    first = make(conn, me, " First ")
    second = make(conn, me)
    assert (first.key, second.key) == ("OIL-1", "OIL-2")
    assert (first.number, first.project_id, first.project_key) == (1, oil.id, "OIL")
    assert first.summary == "First"
    assert (first.status, first.status_category, first.priority, first.issue_type) == (
        "New",
        "To Do",
        "P4",
        "Task",
    )
    assert first.reporter_id == me.id
    assert first.assignee_id is None
    assert (first.original_due_date, first.revised_due_date, first.snoozed_until) == (None, None, None)
    assert first.origin_type == "manual"
    assert first.parent_id is None and first.parent_key is None
    assert first.created_at == first.updated_at == "2026-10-27T08:00:00.000Z"
    assert first.resolved_at is None
    assert (first.labels, first.components) == ((), ())
    assert issues.get_issue(conn, " oil-1 ") == first
    assert issues.get_issue_by_id(conn, first.id) == first


def test_get_unknown_issue(conn: sqlalchemy.Connection, me, oil) -> None:
    with pytest.raises(NotFoundError, match="issue OIL-9 not found"):
        issues.get_issue(conn, "oil-9")
    with pytest.raises(NotFoundError):
        issues.get_issue_by_id(conn, 999)


def test_create_in_unknown_project(conn: sqlalchemy.Connection, me, oil) -> None:
    with pytest.raises(NotFoundError):
        issues.create_issue(conn, "NOPE", "S", "D", actor_id=me.id)


def test_create_with_all_options(conn: sqlalchemy.Connection, me, oil) -> None:
    other = people.create_person(conn, "Sam")
    issue = issues.create_issue(
        conn,
        "oil",
        "Summary",
        "Description",
        actor_id=me.id,
        priority="P2",
        assignee_id=other.id,
        reporter_id=other.id,
        due_date=date(2026, 12, 1),
        acceptance_criteria=" Works ",
        labels=["Zeta", "alpha", "Beta", "ALPHA"],
        components=["Electronics"],
        origin_type="email",
    )
    assert (issue.priority, issue.assignee_id, issue.reporter_id) == ("P2", other.id, other.id)
    assert issue.original_due_date == issue.revised_due_date == date(2026, 12, 1)
    assert issue.acceptance_criteria == "Works"
    assert issue.labels == ("alpha", "Beta", "Zeta")
    assert issue.components == ("Electronics",)
    assert issue.origin_type == "email"


def test_create_writes_one_create_event(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me, labels=["hw"], components=["Electronics"], due_date=date(2026, 12, 1))
    assert events(conn, issue) == [("issue", "create", None, "OIL-1")]
    entry = history.issue_history(conn, issue.id)[0]
    assert (entry.actor_type, entry.actor_id) == ("user", me.id)
    assert change_set_count(conn) == 1


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"summary": ""}, "summary"),
        ({"summary": "   "}, "summary"),
        ({"summary": "x" * 121}, "summary"),
        ({"description": "  "}, "description"),
        ({"priority": "P6"}, "priority"),
        ({"issue_type": "Story"}, "issue_type"),
        ({"origin_type": "fax"}, "origin_type"),
        ({"labels": ["two words"]}, "label"),
    ],
)
def test_create_validation(conn: sqlalchemy.Connection, me, oil, kwargs, match) -> None:
    args = {"summary": "S", "description": "D", **kwargs}
    with pytest.raises(ValidationError, match=match):
        issues.create_issue(
            conn,
            "OIL",
            args.pop("summary"),
            args.pop("description"),
            actor_id=me.id,
            **args,
        )
    assert conn.execute(text("SELECT count(*) FROM issue")).scalar_one() == 0


def test_create_summary_of_120_characters_is_accepted(conn: sqlalchemy.Connection, me, oil) -> None:
    assert len(make(conn, me, "x" * 120).summary) == 120


def test_create_unknown_component_and_person(conn: sqlalchemy.Connection, me, oil) -> None:
    with pytest.raises(NotFoundError, match="Mechanics"):
        make(conn, me, components=["Mechanics"])
    with pytest.raises(NotFoundError):
        make(conn, me, assignee_id=999)
    with pytest.raises(NotFoundError):
        make(conn, me, reporter_id=999)
    assert conn.execute(text("SELECT next_number FROM project WHERE key = 'OIL'")).scalar_one() == 1


def test_create_component_of_other_project_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    other = projects.create_project(conn, "OTH", "Other")
    projects.create_component(conn, other.id, "Mechanics")
    with pytest.raises(NotFoundError):
        make(conn, me, components=["Mechanics"])


# --- update --------------------------------------------------------------------------------------


def test_update_changed_fields_only(conn: sqlalchemy.Connection, me, oil) -> None:
    sam = people.create_person(conn, "Sam")
    issue = make(conn, me)
    updated = issues.update_issue(
        conn,
        issue.key,
        {
            "summary": "New summary",
            "description": "Description",
            "priority": "P2",
            "assignee_id": sam.id,
            "acceptance_criteria": "Done when done",
            "reporter_id": me.id,
        },
        actor_id=me.id,
    )
    assert (updated.summary, updated.priority, updated.assignee_id) == ("New summary", "P2", sam.id)
    assert updated.acceptance_criteria == "Done when done"
    assert events(conn, updated)[1:] == [
        ("summary", "set", "Summary", "New summary"),
        ("acceptance_criteria", "set", None, "Done when done"),
        ("priority", "set", "P4", "P2"),
        ("assignee", "set", None, str(sam.id)),
    ]
    assert change_set_count(conn) == 2


def test_update_reporter_and_unassign(conn: sqlalchemy.Connection, me, oil) -> None:
    sam = people.create_person(conn, "Sam")
    issue = make(conn, me, assignee_id=sam.id)
    updated = issues.update_issue(
        conn, issue.key, {"reporter_id": sam.id, "assignee_id": None}, actor_id=me.id
    )
    assert (updated.reporter_id, updated.assignee_id) == (sam.id, None)
    assert events(conn, updated)[1:] == [
        ("assignee", "set", str(sam.id), None),
        ("reporter", "set", str(me.id), str(sam.id)),
    ]
    with pytest.raises(ValidationError, match="reporter_id"):
        issues.update_issue(conn, issue.key, {"reporter_id": None}, actor_id=me.id)
    with pytest.raises(NotFoundError):
        issues.update_issue(conn, issue.key, {"assignee_id": 999}, actor_id=me.id)


def test_update_snooze_and_clear_acceptance_criteria(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me, acceptance_criteria="AC")
    updated = issues.update_issue(
        conn, issue.key, {"snoozed_until": date(2026, 11, 3), "acceptance_criteria": None}, actor_id=me.id
    )
    assert updated.snoozed_until == date(2026, 11, 3)
    assert updated.acceptance_criteria is None
    assert events(conn, updated)[1:] == [
        ("acceptance_criteria", "set", "AC", None),
        ("snoozed_until", "set", None, "2026-11-03"),
    ]
    cleared = issues.update_issue(conn, issue.key, {"snoozed_until": None}, actor_id=me.id)
    assert cleared.snoozed_until is None
    assert events(conn, cleared)[-1] == ("snoozed_until", "set", "2026-11-03", None)


def test_update_without_difference_is_a_noop(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = make(conn, me, due_date=date(2026, 12, 1))
    clock(hours=1)
    again = issues.update_issue(
        conn,
        issue.key,
        {
            "summary": " Summary ",
            "description": "Description",
            "priority": "P4",
            "issue_type": "Task",
            "parent": None,
            "assignee_id": None,
            "reporter_id": me.id,
            "due_date": date(2026, 12, 1),
            "snoozed_until": None,
            "acceptance_criteria": "  ",
        },
        actor_id=me.id,
    )
    assert again == issue
    assert again.updated_at == issue.updated_at
    assert change_set_count(conn) == 1
    assert issues.update_issue(conn, issue.key, {}, actor_id=me.id) == issue


def test_update_changes_updated_at(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = make(conn, me)
    clock(hours=2)
    updated = issues.update_issue(conn, issue.key, {"summary": "Other"}, actor_id=me.id)
    assert issue.updated_at == "2026-10-27T08:00:00.000Z"
    assert updated.updated_at == "2026-10-27T10:00:00.000Z"
    assert updated.created_at == issue.created_at


def test_first_due_date_sets_original_and_revised(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    updated = issues.update_issue(conn, issue.key, {"due_date": date(2026, 12, 1)}, actor_id=me.id)
    assert updated.original_due_date == updated.revised_due_date == date(2026, 12, 1)
    assert events(conn, updated)[1:] == [
        ("due_date", "set", None, "2026-12-01"),
        ("original_due_date", "set", None, "2026-12-01"),
    ]


def test_later_due_date_change_keeps_original(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me, due_date=date(2026, 12, 1))
    updated = issues.update_issue(conn, issue.key, {"due_date": date(2026, 12, 15)}, actor_id=me.id)
    assert updated.original_due_date == date(2026, 12, 1)
    assert updated.revised_due_date == date(2026, 12, 15)
    assert events(conn, updated)[1:] == [("due_date", "set", "2026-12-01", "2026-12-15")]


def test_removing_due_date_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me, due_date=date(2026, 12, 1))
    with pytest.raises(ValidationError, match=r"cannot be removed once set \(DM-12\)"):
        issues.update_issue(conn, issue.key, {"due_date": None, "summary": "x"}, actor_id=me.id)
    assert issues.get_issue(conn, issue.key).summary == "Summary"
    assert change_set_count(conn) == 1


def test_clearing_absent_due_date_is_a_noop(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    assert issues.update_issue(conn, issue.key, {"due_date": None}, actor_id=me.id) == issue


def test_due_date_must_be_a_date(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    for bad in ("2026-12-01", datetime(2026, 12, 1, 9, 0)):
        with pytest.raises(ValidationError, match="due_date"):
            issues.update_issue(conn, issue.key, {"due_date": bad}, actor_id=me.id)


def test_update_unknown_field_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match="colour"):
        issues.update_issue(conn, issue.key, {"colour": "red"}, actor_id=me.id)
    with pytest.raises(ValidationError, match="origin_type"):
        issues.update_issue(conn, issue.key, {"origin_type": "email"}, actor_id=me.id)


def test_update_status_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match="status"):
        issues.update_issue(conn, issue.key, {"status": "Done"}, actor_id=me.id)
    assert issues.get_issue(conn, issue.key).status == "New"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"summary": ""}, "summary"),
        ({"summary": "x" * 121}, "summary"),
        ({"description": " "}, "description"),
        ({"priority": "P0"}, "priority"),
        ({"issue_type": "Story"}, "issue_type"),
    ],
)
def test_update_validation(conn: sqlalchemy.Connection, me, oil, changes, match) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match=match):
        issues.update_issue(conn, issue.key, changes, actor_id=me.id)


def test_update_unknown_issue(conn: sqlalchemy.Connection, me, oil) -> None:
    with pytest.raises(NotFoundError):
        issues.update_issue(conn, "OIL-9", {"summary": "x"}, actor_id=me.id)


def test_update_passes_actor_and_provenance_into_change_set(conn: sqlalchemy.Connection, me, oil) -> None:
    conn.execute(text("INSERT INTO sync_run (id, trigger, scope) VALUES (7, 'cli', 'all')"))
    conn.execute(
        text(
            "INSERT INTO proposal (id, kind, payload_json, rationale, dedup_key)"
            " VALUES (5, 'field_change', '{}', 'why', 'k')"
        )
    )
    issue = make(conn, me)
    issues.update_issue(
        conn,
        issue.key,
        {"summary": "From sync"},
        actor_id=None,
        actor_type="sync",
        sync_run_id=7,
    )
    issues.update_issue(conn, issue.key, {"priority": "P1"}, actor_id=me.id, actor_type="user", proposal_id=5)
    sync_entry, proposal_entry = history.issue_history(conn, issue.id)[1:]
    assert (sync_entry.actor_type, sync_entry.actor_id, sync_entry.sync_run_id, sync_entry.proposal_id) == (
        "sync",
        None,
        7,
        None,
    )
    assert (
        proposal_entry.actor_type,
        proposal_entry.actor_id,
        proposal_entry.sync_run_id,
        proposal_entry.proposal_id,
    ) == ("user", me.id, None, 5)


def test_transition_passes_actor_and_proposal(conn: sqlalchemy.Connection, me, oil) -> None:
    conn.execute(
        text(
            "INSERT INTO proposal (id, kind, payload_json, rationale, dedup_key)"
            " VALUES (5, 'field_change', '{}', 'why', 'k')"
        )
    )
    issue = make(conn, me)
    issues.transition_issue(conn, issue.key, "In Progress", actor_id=None, actor_type="system", proposal_id=5)
    entry = history.issue_history(conn, issue.id)[1]
    assert (entry.actor_type, entry.actor_id, entry.proposal_id) == ("system", None, 5)


# --- transitions ---------------------------------------------------------------------------------

ALLOWED = [(source, target) for source, targets in issues.TRANSITIONS.items() for target in targets]


@pytest.mark.parametrize(("source", "target"), ALLOWED)
def test_allowed_transitions(conn: sqlalchemy.Connection, me, oil, source: str, target: str) -> None:
    issue = in_state(conn, me, source)
    moved = issues.transition_issue(conn, issue.key, target, actor_id=me.id, reason="because")
    assert moved.status == target
    assert moved.status_category == issues.STATUS_CATEGORY[target]
    assert ("status", "set", source, target) in events(conn, moved)


def test_allowed_transition_table_is_complete() -> None:
    assert len(ALLOWED) == 5 + 4 + 3 + 4 + 1 + 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Done", "Blocked"),
        ("Cancelled", "Done"),
        ("New", "New"),
        ("In Progress", "New"),
        ("In Progress", "In Progress"),
        ("Done", "Cancelled"),
        ("Done", "Ready to Proceed"),
        ("In Progress", "Ready to Proceed"),
    ],
)
def test_disallowed_transitions(conn: sqlalchemy.Connection, me, oil, source: str, target: str) -> None:
    issue = in_state(conn, me, source)
    before = len(events(conn, issue))
    with pytest.raises(TransitionError, match=f"cannot move {issue.key} from {source} to {target}"):
        issues.transition_issue(conn, issue.key, target, actor_id=me.id, reason="because")
    assert issues.get_issue(conn, issue.key) == issue
    assert len(events(conn, issue)) == before


def test_unknown_status_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match="status"):
        issues.transition_issue(conn, issue.key, "Closed", actor_id=me.id)


def test_blocked_needs_reason_and_leaving_clears_it(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    for reason in (None, "", "   "):
        with pytest.raises(ValidationError, match="reason"):
            issues.transition_issue(conn, issue.key, "Blocked", actor_id=me.id, reason=reason)
    assert issues.get_issue(conn, issue.key).status == "New"

    blocked = issues.transition_issue(conn, issue.key, "Blocked", actor_id=me.id, reason="  waiting for Sam ")
    assert (blocked.status, blocked.blocked_reason) == ("Blocked", "waiting for Sam")
    assert events(conn, blocked)[1:] == [
        ("status", "set", "New", "Blocked"),
        ("blocked_reason", "set", None, "waiting for Sam"),
    ]

    resumed = issues.transition_issue(conn, issue.key, "In Progress", actor_id=me.id)
    assert resumed.blocked_reason is None
    assert events(conn, resumed)[3:] == [
        ("status", "set", "Blocked", "In Progress"),
        ("blocked_reason", "set", "waiting for Sam", None),
    ]


def test_blocked_to_done_clears_blocked_reason_and_resolves(
    conn: sqlalchemy.Connection, me, oil, clock
) -> None:
    issue = in_state(conn, me, "Blocked")
    clock(days=1)
    done = issues.transition_issue(conn, issue.key, "Done", actor_id=me.id)
    assert (done.blocked_reason, done.resolved_at) == (None, "2026-10-28T08:00:00.000Z")
    assert events(conn, done)[-3:] == [
        ("status", "set", "Blocked", "Done"),
        ("blocked_reason", "set", "because", None),
        ("resolved_at", "set", None, "2026-10-28T08:00:00.000Z"),
    ]


def test_blocked_to_cancelled_swaps_reasons(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = in_state(conn, me, "Blocked")
    cancelled = issues.transition_issue(conn, issue.key, "Cancelled", actor_id=me.id, reason="duplicate")
    assert (cancelled.blocked_reason, cancelled.cancel_reason) == (None, "duplicate")
    fields = [e[0] for e in events(conn, cancelled)[-4:]]
    assert fields == ["status", "blocked_reason", "cancel_reason", "resolved_at"]


def test_cancelled_needs_reason_and_resolves(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError, match="reason"):
        issues.transition_issue(conn, issue.key, "Cancelled", actor_id=me.id)
    clock(minutes=5)
    cancelled = issues.transition_issue(
        conn, issue.key, "Cancelled", actor_id=me.id, reason=" Moved to OIL-9 "
    )
    assert cancelled.cancel_reason == "Moved to OIL-9"
    assert cancelled.resolved_at == cancelled.updated_at == "2026-10-27T08:05:00.000Z"
    assert events(conn, cancelled)[1:] == [
        ("status", "set", "New", "Cancelled"),
        ("cancel_reason", "set", None, "Moved to OIL-9"),
        ("resolved_at", "set", None, "2026-10-27T08:05:00.000Z"),
    ]


def test_done_sets_resolved_at_only(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = in_state(conn, me, "In Progress")
    done = issues.transition_issue(conn, issue.key, "Done", actor_id=me.id, reason="ignored")
    assert done.resolved_at is not None
    assert (done.cancel_reason, done.blocked_reason) == (None, None)
    assert [e[0] for e in events(conn, done)[-2:]] == ["status", "resolved_at"]


def test_reopen_clears_resolved_at_and_cancel_reason(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = in_state(conn, me, "In Progress")
    clock(hours=1)
    cancelled = issues.transition_issue(
        conn, issue.key, "Cancelled", actor_id=me.id, reason="no longer needed"
    )
    clock(hours=1)
    reopened = issues.transition_issue(conn, issue.key, "In Progress", actor_id=me.id)
    assert (reopened.status, reopened.resolved_at, reopened.cancel_reason) == ("In Progress", None, None)
    assert events(conn, reopened)[-3:] == [
        ("status", "set", "Cancelled", "In Progress"),
        ("cancel_reason", "set", "no longer needed", None),
        ("resolved_at", "set", cancelled.resolved_at, None),
    ]


def test_reopen_done_clears_resolved_at(conn: sqlalchemy.Connection, me, oil) -> None:
    done = in_state(conn, me, "Done")
    reopened = issues.transition_issue(conn, done.key, "In Progress", actor_id=me.id)
    assert reopened.resolved_at is None
    assert events(conn, reopened)[-2:] == [
        ("status", "set", "Done", "In Progress"),
        ("resolved_at", "set", done.resolved_at, None),
    ]


def test_transition_is_one_change_set_and_touches_updated_at(
    conn: sqlalchemy.Connection, me, oil, clock
) -> None:
    issue = make(conn, me)
    clock(hours=3)
    moved = issues.transition_issue(conn, issue.key, "Cancelled", actor_id=me.id, reason="r")
    assert change_set_count(conn) == 2
    assert moved.updated_at == "2026-10-27T11:00:00.000Z"


# --- hierarchy -----------------------------------------------------------------------------------


def test_epic_cannot_have_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    with pytest.raises(ValidationError, match="Epic cannot have a parent"):
        make(conn, me, issue_type="Epic", parent_key=epic.key)


def test_task_with_and_without_epic_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    task = make(conn, me, parent_key=epic.key.lower())
    assert (task.parent_id, task.parent_key) == (epic.id, epic.key)
    assert make(conn, me).parent_id is None
    assert [child.key for child in issues.list_children(conn, epic.key)] == [task.key]


def test_task_parent_must_be_epic(conn: sqlalchemy.Connection, me, oil) -> None:
    task = make(conn, me)
    with pytest.raises(ValidationError, match="must be an Epic"):
        make(conn, me, parent_key=task.key)


def test_subtask_requires_task_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    task = make(conn, me)
    with pytest.raises(ValidationError, match="requires a Task"):
        make(conn, me, issue_type="Sub-task")
    with pytest.raises(ValidationError, match="requires a Task"):
        make(conn, me, issue_type="Sub-task", parent_key=epic.key)
    subtask = make(conn, me, issue_type="Sub-task", parent_key=task.key)
    assert subtask.parent_key == task.key
    with pytest.raises(ValidationError, match="requires a Task"):
        make(conn, me, issue_type="Sub-task", parent_key=subtask.key)


def test_unknown_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    with pytest.raises(NotFoundError):
        make(conn, me, parent_key="OIL-99")
    task = make(conn, me)
    with pytest.raises(NotFoundError):
        issues.update_issue(conn, task.key, {"parent": "OIL-99"}, actor_id=me.id)


def test_parent_in_other_project_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    projects.create_project(conn, "OTH", "Other")
    foreign_epic = issues.create_issue(conn, "OTH", "Epic", "D", actor_id=me.id, issue_type="Epic")
    with pytest.raises(ValidationError, match="another project"):
        make(conn, me, parent_key=foreign_epic.key)
    task = make(conn, me)
    with pytest.raises(ValidationError, match="another project"):
        issues.update_issue(conn, task.key, {"parent": foreign_epic.key}, actor_id=me.id)


def test_issue_cannot_be_its_own_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    task = make(conn, me)
    with pytest.raises(ValidationError, match="own parent"):
        issues.update_issue(conn, task.key, {"parent": task.key}, actor_id=me.id)


def test_update_parent_records_keys(conn: sqlalchemy.Connection, me, oil) -> None:
    epic_one = make(conn, me, issue_type="Epic")
    epic_two = make(conn, me, issue_type="Epic")
    task = make(conn, me)
    attached = issues.update_issue(conn, task.key, {"parent": epic_one.key}, actor_id=me.id)
    moved = issues.update_issue(conn, task.key, {"parent": epic_two.key}, actor_id=me.id)
    detached = issues.update_issue(conn, task.key, {"parent": None}, actor_id=me.id)
    assert (attached.parent_key, moved.parent_key, detached.parent_key) == (epic_one.key, epic_two.key, None)
    assert events(conn, detached)[1:] == [
        ("parent", "set", None, epic_one.key),
        ("parent", "set", epic_one.key, epic_two.key),
        ("parent", "set", epic_two.key, None),
    ]
    assert issues.update_issue(conn, task.key, {"parent": None}, actor_id=me.id) == detached


def test_change_type_records_event(conn: sqlalchemy.Connection, me, oil) -> None:
    task = make(conn, me)
    epic = issues.update_issue(conn, task.key, {"issue_type": "Epic"}, actor_id=me.id)
    assert epic.issue_type == "Epic"
    assert events(conn, epic)[1:] == [("issue_type", "set", "Task", "Epic")]


def test_type_and_parent_change_together_are_checked_as_a_pair(conn: sqlalchemy.Connection, me, oil) -> None:
    task = make(conn, me)
    other_task = make(conn, me)
    subtask = issues.update_issue(
        conn, task.key, {"issue_type": "Sub-task", "parent": other_task.key}, actor_id=me.id
    )
    assert (subtask.issue_type, subtask.parent_key) == ("Sub-task", other_task.key)
    with pytest.raises(ValidationError, match="requires a Task"):
        issues.update_issue(conn, other_task.key, {"issue_type": "Sub-task"}, actor_id=me.id)


def test_type_change_requires_valid_parent(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    task = make(conn, me, parent_key=epic.key)
    with pytest.raises(ValidationError, match="Epic cannot have a parent"):
        issues.update_issue(conn, task.key, {"issue_type": "Epic"}, actor_id=me.id)
    with pytest.raises(ValidationError, match="requires a Task"):
        issues.update_issue(conn, task.key, {"issue_type": "Sub-task"}, actor_id=me.id)


def test_type_change_breaking_children_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    epic_task = make(conn, me, parent_key=epic.key)
    task = make(conn, me)
    subtask = make(conn, me, issue_type="Sub-task", parent_key=task.key)
    spare_task = make(conn, me)

    with pytest.raises(ValidationError, match=subtask.key):
        issues.update_issue(conn, task.key, {"issue_type": "Epic"}, actor_id=me.id)
    with pytest.raises(ValidationError, match=subtask.key):
        issues.update_issue(
            conn, task.key, {"issue_type": "Sub-task", "parent": spare_task.key}, actor_id=me.id
        )
    with pytest.raises(ValidationError, match=epic_task.key):
        issues.update_issue(conn, epic.key, {"issue_type": "Task"}, actor_id=me.id)
    with pytest.raises(ValidationError, match=epic_task.key):
        issues.update_issue(
            conn, epic.key, {"issue_type": "Sub-task", "parent": spare_task.key}, actor_id=me.id
        )
    assert issues.get_issue(conn, task.key).issue_type == "Task"
    assert issues.get_issue(conn, epic.key).issue_type == "Epic"


def test_type_change_of_childless_issue_is_allowed(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    assert issues.update_issue(conn, epic.key, {"issue_type": "Task"}, actor_id=me.id).issue_type == "Task"


def test_list_children_ordered_by_number(conn: sqlalchemy.Connection, me, oil) -> None:
    epic = make(conn, me, issue_type="Epic")
    children = [make(conn, me, parent_key=epic.key) for _ in range(3)]
    make(conn, me)
    assert [c.key for c in issues.list_children(conn, epic.key)] == [c.key for c in children]
    assert issues.list_children(conn, children[0].key) == []
    with pytest.raises(NotFoundError):
        issues.list_children(conn, "OIL-99")


# --- labels and components -----------------------------------------------------------------------


def test_add_and_remove_label(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = make(conn, me)
    clock(hours=1)
    added = issues.add_label(conn, issue.key, " hw ", actor_id=me.id)
    assert added.labels == ("hw",)
    assert added.updated_at == "2026-10-27T09:00:00.000Z"
    clock(hours=1)
    removed = issues.remove_label(conn, issue.key, "HW", actor_id=me.id)
    assert removed.labels == ()
    assert removed.updated_at == "2026-10-27T10:00:00.000Z"
    assert events(conn, removed)[1:] == [("label", "add", None, "hw"), ("label", "remove", "hw", None)]
    entry = history.issue_history(conn, issue.id)[1]
    assert (entry.actor_type, entry.actor_id) == ("user", me.id)


def test_labels_sorted_case_insensitively(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    for name in ("beta", "Zeta", "Alpha"):
        issue = issues.add_label(conn, issue.key, name, actor_id=me.id)
    assert issue.labels == ("Alpha", "beta", "Zeta")


def test_label_noops(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    issue = issues.add_label(conn, make(conn, me).key, "hw", actor_id=me.id)
    clock(hours=1)
    sets_before = change_set_count(conn)
    assert issues.add_label(conn, issue.key, "HW", actor_id=me.id) == issue
    assert issues.remove_label(conn, issue.key, "unknown", actor_id=me.id) == issue
    other = projects.get_or_create_label(conn, "other")
    assert other.name == "other"
    assert issues.remove_label(conn, issue.key, "other", actor_id=me.id) == issue
    assert change_set_count(conn) == sets_before


def test_invalid_label_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    issue = make(conn, me)
    with pytest.raises(ValidationError):
        issues.add_label(conn, issue.key, "two words", actor_id=me.id)


def test_add_and_remove_component(conn: sqlalchemy.Connection, me, oil, clock) -> None:
    projects.create_component(conn, oil.id, "Firmware")
    issue = make(conn, me)
    clock(hours=1)
    issue = issues.add_component(conn, issue.key, "Firmware", actor_id=me.id)
    issue = issues.add_component(conn, issue.key, "Electronics", actor_id=me.id)
    assert issue.components == ("Electronics", "Firmware")
    assert issue.updated_at == "2026-10-27T09:00:00.000Z"
    clock(hours=1)
    issue = issues.remove_component(conn, issue.key, "Firmware", actor_id=me.id)
    assert issue.components == ("Electronics",)
    assert issue.updated_at == "2026-10-27T10:00:00.000Z"
    assert events(conn, issue)[1:] == [
        ("component", "add", None, "Firmware"),
        ("component", "add", None, "Electronics"),
        ("component", "remove", "Firmware", None),
    ]


def test_component_noops(conn: sqlalchemy.Connection, me, oil) -> None:
    projects.create_component(conn, oil.id, "Firmware")
    issue = issues.add_component(conn, make(conn, me).key, "Electronics", actor_id=me.id)
    sets_before = change_set_count(conn)
    assert issues.add_component(conn, issue.key, "Electronics", actor_id=me.id) == issue
    assert issues.remove_component(conn, issue.key, "Firmware", actor_id=me.id) == issue
    assert change_set_count(conn) == sets_before


def test_component_of_other_project_rejected(conn: sqlalchemy.Connection, me, oil) -> None:
    other = projects.create_project(conn, "OTH", "Other")
    projects.create_component(conn, other.id, "Mechanics")
    issue = make(conn, me)
    with pytest.raises(NotFoundError):
        issues.add_component(conn, issue.key, "Mechanics", actor_id=me.id)
    with pytest.raises(NotFoundError):
        issues.remove_component(conn, issue.key, "Mechanics", actor_id=me.id)
    with pytest.raises(NotFoundError):
        issues.add_component(conn, issue.key, "Nope", actor_id=me.id)
    assert change_set_count(conn) == 1
