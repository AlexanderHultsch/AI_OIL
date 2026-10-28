import pytest
import sqlalchemy

from ai_oil.domain import issues, people, projects
from ai_oil.errors import ConflictError, NotFoundError, ValidationError


@pytest.mark.parametrize("key", ["oil", "O", "ABCDEFGHIJK", "1AB", "", "A B", "OIL-1"])
def test_invalid_keys_rejected(conn: sqlalchemy.Connection, key: str) -> None:
    with pytest.raises(ValidationError, match="project key"):
        projects.create_project(conn, key, "Name")


@pytest.mark.parametrize("key", ["OIL", "HW2", "AB", "ABCDEFGHIJ"])
def test_valid_keys_accepted(conn: sqlalchemy.Connection, key: str) -> None:
    project = projects.create_project(conn, key, "Name")
    assert project.key == key


def test_create_project_returns_defaults(conn: sqlalchemy.Connection) -> None:
    project = projects.create_project(conn, "OIL", " Open issues ", description="d")
    assert project == projects.Project(
        id=project.id,
        key="OIL",
        name="Open issues",
        description="d",
        lead_id=None,
        next_number=1,
        archived=False,
    )


def test_blank_name_rejected(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(ValidationError):
        projects.create_project(conn, "OIL", "   ")


def test_duplicate_key_rejected(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "OIL", "One")
    with pytest.raises(ConflictError):
        projects.create_project(conn, "OIL", "Two")


def test_get_project_is_case_insensitive(conn: sqlalchemy.Connection) -> None:
    created = projects.create_project(conn, "OIL", "One")
    assert projects.get_project(conn, "oil") == created
    assert projects.get_project_by_id(conn, created.id) == created
    with pytest.raises(NotFoundError):
        projects.get_project(conn, "NOPE")
    with pytest.raises(NotFoundError):
        projects.get_project_by_id(conn, 999)


def test_list_projects_ordered_and_filters_archived(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "ZED", "Z")
    projects.create_project(conn, "ABC", "A")
    projects.create_project(conn, "MID", "M")
    projects.update_project(conn, "MID", archived=True)
    assert [p.key for p in projects.list_projects(conn)] == ["ABC", "ZED"]
    assert [p.key for p in projects.list_projects(conn, include_archived=True)] == ["ABC", "MID", "ZED"]


def test_update_project_changes_only_given_fields(conn: sqlalchemy.Connection) -> None:
    conn.execute(sqlalchemy.text("INSERT INTO person(id, display_name) VALUES (1, 'Lead')"))
    projects.create_project(conn, "OIL", "One", description="old")
    updated = projects.update_project(conn, "oil", name="Renamed", lead_id=1)
    assert (updated.key, updated.name, updated.description, updated.lead_id, updated.archived) == (
        "OIL",
        "Renamed",
        "old",
        1,
        False,
    )
    updated = projects.update_project(conn, "OIL", description="new", archived=True)
    assert (updated.name, updated.description, updated.lead_id, updated.archived) == (
        "Renamed",
        "new",
        1,
        True,
    )
    updated = projects.update_project(conn, "OIL", archived=False)
    assert updated.archived is False
    assert projects.update_project(conn, "OIL") == updated


def test_update_project_blank_name_and_unknown_key(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "OIL", "One")
    with pytest.raises(ValidationError):
        projects.update_project(conn, "OIL", name=" ")
    with pytest.raises(NotFoundError):
        projects.update_project(conn, "NOPE", name="x")


def test_allocate_issue_number_counts_up(conn: sqlalchemy.Connection) -> None:
    project = projects.create_project(conn, "OIL", "One")
    assert [projects.allocate_issue_number(conn, project.id) for _ in range(3)] == [1, 2, 3]
    assert projects.get_project(conn, "OIL").next_number == 4


def test_allocate_issue_number_unknown_project(conn: sqlalchemy.Connection) -> None:
    with pytest.raises(NotFoundError):
        projects.allocate_issue_number(conn, 999)


def test_components(conn: sqlalchemy.Connection) -> None:
    oil = projects.create_project(conn, "OIL", "One")
    other = projects.create_project(conn, "OTH", "Two")
    projects.create_component(conn, oil.id, "Hardware", "boards")
    projects.create_component(conn, oil.id, "Firmware")
    projects.create_component(conn, other.id, "Hardware")

    with pytest.raises(ConflictError):
        projects.create_component(conn, oil.id, "Hardware")
    with pytest.raises(ValidationError):
        projects.create_component(conn, oil.id, "  ")
    with pytest.raises(NotFoundError):
        projects.create_component(conn, 999, "X")

    assert [c.name for c in projects.list_components(conn, oil.id)] == ["Firmware", "Hardware"]
    assert projects.get_component(conn, oil.id, "Hardware").description == "boards"
    with pytest.raises(NotFoundError):
        projects.get_component(conn, oil.id, "Mechanics")


def test_list_components_filters_archived(conn: sqlalchemy.Connection) -> None:
    oil = projects.create_project(conn, "OIL", "One")
    projects.create_component(conn, oil.id, "Old")
    projects.create_component(conn, oil.id, "New")
    conn.execute(sqlalchemy.text("UPDATE component SET archived = 1 WHERE name = 'Old'"))
    assert [c.name for c in projects.list_components(conn, oil.id)] == ["New"]
    listed = projects.list_components(conn, oil.id, include_archived=True)
    assert [(c.name, c.archived) for c in listed] == [("New", False), ("Old", True)]


@pytest.mark.parametrize("name", ["", "  ", "two words", "a,b"])
def test_invalid_labels_rejected(conn: sqlalchemy.Connection, name: str) -> None:
    with pytest.raises(ValidationError):
        projects.get_or_create_label(conn, name)


def test_label_reuse_is_case_insensitive(conn: sqlalchemy.Connection) -> None:
    first = projects.get_or_create_label(conn, " Hardware ")
    again = projects.get_or_create_label(conn, "HARDWARE")
    assert first.name == "Hardware"
    assert again == first
    projects.get_or_create_label(conn, "alpha")
    assert [label.name for label in projects.list_labels(conn)] == ["alpha", "Hardware"]


# --- archiving (DM-1, OP-30) ---------------------------------------------------------------------------


def _project_with_issues(conn: sqlalchemy.Connection, count: int) -> people.Person:
    me = people.create_person(conn, "Alex", is_me=True)
    projects.create_project(conn, "OIL", "Open issues")
    for number in range(count):
        issues.create_issue(conn, "OIL", f"Issue {number + 1}", "Description", actor_id=me.id)
    return me


def test_closed_statuses_are_the_done_category() -> None:
    assert projects._CLOSED_STATUSES == tuple(
        status for status, category in issues.STATUS_CATEGORY.items() if category == "Done"
    )


def test_archiving_is_refused_while_issues_are_open_and_changes_nothing(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 7)
    issues.transition_issue(conn, "OIL-2", "Done", actor_id=me.id)
    issues.transition_issue(conn, "OIL-3", "Cancelled", actor_id=me.id, reason="not needed")
    issues.transition_issue(conn, "OIL-5", "Blocked", actor_id=me.id, reason="waiting")
    issues.transition_issue(conn, "OIL-6", "Done", actor_id=me.id)

    with pytest.raises(ValidationError) as excinfo:
        projects.update_project(conn, "OIL", name="Renamed", archived=True)

    assert str(excinfo.value) == (
        "project OIL cannot be archived: 4 issues are not Done or Cancelled: OIL-1, OIL-4, OIL-5, OIL-7"
    )
    project = projects.get_project(conn, "OIL")
    assert (project.archived, project.name) == (False, "Open issues")


def test_archive_refusal_message_names_the_open_issues_in_number_order(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 7)
    for number in (2, 3, 5, 6):
        issues.transition_issue(conn, f"OIL-{number}", "Done", actor_id=me.id)

    with pytest.raises(ValidationError) as excinfo:
        projects.update_project(conn, "OIL", archived=True)

    assert str(excinfo.value) == (
        "project OIL cannot be archived: 3 issues are not Done or Cancelled: OIL-1, OIL-4, OIL-7"
    )


def test_archive_refusal_for_a_single_open_issue(conn: sqlalchemy.Connection) -> None:
    _project_with_issues(conn, 1)

    with pytest.raises(ValidationError) as excinfo:
        projects.update_project(conn, "OIL", archived=True)

    assert str(excinfo.value) == "project OIL cannot be archived: 1 issue is not Done or Cancelled: OIL-1"


def test_archive_refusal_lists_ten_keys_by_number_and_counts_the_rest(conn: sqlalchemy.Connection) -> None:
    _project_with_issues(conn, 13)

    with pytest.raises(ValidationError) as excinfo:
        projects.update_project(conn, "OIL", archived=True)

    keys = ", ".join(f"OIL-{number}" for number in range(1, 11))
    assert str(excinfo.value) == (
        f"project OIL cannot be archived: 13 issues are not Done or Cancelled: {keys} and 3 more"
    )
    assert projects.get_project(conn, "OIL").archived is False


def test_archive_refusal_exactly_at_the_cap_has_no_more_suffix(conn: sqlalchemy.Connection) -> None:
    _project_with_issues(conn, 10)

    with pytest.raises(ValidationError) as excinfo:
        projects.update_project(conn, "OIL", archived=True)

    assert str(excinfo.value).endswith("OIL-9, OIL-10")


def test_archive_refusal_ignores_issues_of_other_projects(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 1)
    projects.create_project(conn, "ABC", "Alphabet")
    issues.transition_issue(conn, "OIL-1", "Done", actor_id=me.id)
    issues.create_issue(conn, "ABC", "Open elsewhere", "Description", actor_id=me.id)

    assert projects.update_project(conn, "OIL", archived=True).archived is True


def test_archiving_is_allowed_when_all_issues_are_done_or_cancelled(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 2)
    issues.transition_issue(conn, "OIL-1", "Done", actor_id=me.id)
    issues.transition_issue(conn, "OIL-2", "Cancelled", actor_id=me.id, reason="not needed")

    assert projects.update_project(conn, "OIL", archived=True).archived is True


def test_archiving_is_allowed_without_issues(conn: sqlalchemy.Connection) -> None:
    projects.create_project(conn, "OIL", "Open issues")

    assert projects.update_project(conn, "OIL", archived=True).archived is True


def test_unarchiving_is_always_allowed(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 2)
    issues.transition_issue(conn, "OIL-1", "Done", actor_id=me.id)
    issues.transition_issue(conn, "OIL-2", "Done", actor_id=me.id)
    projects.update_project(conn, "OIL", archived=True)
    issues.transition_issue(conn, "OIL-2", "In Progress", actor_id=me.id)

    assert projects.update_project(conn, "OIL", archived=False).archived is False


def test_archiving_an_archived_project_again_is_a_noop(conn: sqlalchemy.Connection) -> None:
    me = _project_with_issues(conn, 1)
    issues.transition_issue(conn, "OIL-1", "Done", actor_id=me.id)
    projects.update_project(conn, "OIL", archived=True)
    issues.transition_issue(conn, "OIL-1", "In Progress", actor_id=me.id)

    assert projects.update_project(conn, "OIL", archived=True).archived is True


# --- project lead and merged persons (DM-23, OP-31) ----------------------------------------------------


def test_project_lead_is_redirected_to_the_surviving_person(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    duplicate = people.create_person(conn, "Sam")
    survivor = people.create_person(conn, "Samuel Smith")
    people.merge_persons(conn, duplicate.id, survivor.id, actor_id=me.id)

    created = projects.create_project(conn, "OIL", "Open issues", lead_id=duplicate.id)
    updated = projects.update_project(conn, "OIL", lead_id=duplicate.id)
    other = projects.create_project(conn, "ABC", "Alphabet")
    redirected = projects.update_project(conn, other.key, lead_id=duplicate.id)

    assert created.lead_id == survivor.id
    assert updated.lead_id == survivor.id
    assert redirected.lead_id == survivor.id


def test_project_lead_follows_a_two_step_merge_chain(conn: sqlalchemy.Connection) -> None:
    me = people.create_person(conn, "Alex", is_me=True)
    first = people.create_person(conn, "S.")
    second = people.create_person(conn, "Sam")
    survivor = people.create_person(conn, "Samuel Smith")
    people.merge_persons(conn, first.id, second.id, actor_id=me.id)
    people.merge_persons(conn, second.id, survivor.id, actor_id=me.id)
    projects.create_project(conn, "OIL", "Open issues")

    assert projects.update_project(conn, "OIL", lead_id=first.id).lead_id == survivor.id
