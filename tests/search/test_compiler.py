import dataclasses
from datetime import UTC, date, datetime

import pytest
import sqlalchemy
from sqlalchemy import text

from ai_oil import timeutil
from ai_oil.domain import comments, issue_links, issues, people, projects
from ai_oil.errors import ValidationError
from ai_oil.search import compiler, oql
from ai_oil.sources import linking, store
from ai_oil.sources.model import EntryData, ObjectMeta, VersionData

NOW = datetime(2026, 10, 28, 13, 0)  # naive local wall-clock time; a Wednesday, so the week is Oct 26 - Nov 1


def at(month: int, day: int, hour: int = 0, minute: int = 0, *, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute)


def utc_iso(local: datetime) -> str:
    utc = local.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def freeze(monkeypatch: pytest.MonkeyPatch, local: datetime) -> None:
    monkeypatch.setattr(timeutil, "_now", lambda: local.astimezone(UTC))


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, NOW)


# Result order of a query without ORDER BY: updated DESC (SRCH-7), see TIMES below.
DEFAULT_ORDER = ["OIL-3", "OIL-2", "PLT-2", "OIL-6", "OIL-1", "OIL-4", "PLT-1", "OIL-5"]

# key -> (created, updated, resolved) as local times; the fixture writes them as UTC text
TIMES: dict[str, tuple[datetime, datetime, datetime | None]] = {
    "OIL-1": (at(10, 1, 9), at(10, 20, 11), None),
    "OIL-2": (at(10, 5, 9), at(10, 27, 15), None),
    "OIL-3": (at(10, 10, 8), at(10, 28, 9), None),
    "OIL-4": (at(10, 11, 8), at(10, 20, 10), at(10, 20, 10)),
    "OIL-5": (at(10, 12, 8), at(10, 15, 12), None),
    "OIL-6": (at(10, 13, 8), at(10, 22, 18), at(10, 22, 18)),
    "PLT-1": (at(9, 28, 8), at(10, 18, 8), None),
    "PLT-2": (at(10, 26, 0), at(10, 26, 8), None),
}


@pytest.fixture
def world(conn: sqlalchemy.Connection) -> None:
    """Eight issues in two projects with people, labels, components, links, comments and sources."""
    me = people.create_person(conn, "Alex Hultsch", is_me=True)
    bea = people.create_person(conn, "Bea Brandt")
    people.add_identity(conn, bea.id, "email", "bea@example.com")
    carl = people.create_person(conn, "Carl Conrad")
    oil = projects.create_project(conn, "OIL", "Open issues")
    plt = projects.create_project(conn, "PLT", "Platform")
    projects.create_component(conn, oil.id, "Hardware")
    projects.create_component(conn, oil.id, "Software")
    projects.create_component(conn, plt.id, "Firmware")

    def make(project: str, summary: str, description: str, **kwargs) -> issues.Issue:
        return issues.create_issue(conn, project, summary, description, actor_id=me.id, **kwargs)

    make(
        "OIL",
        "Power stage redesign",
        "Rework of the inductor selection",
        issue_type="Epic",
        priority="P2",
        assignee_id=me.id,
        due_date=date(2026, 11, 15),
        labels=["supplier"],
        components=["Hardware"],
    )
    make(
        "OIL",
        "Clarify inductor lead time",
        "Supplier quoted 26 weeks",
        parent_key="OIL-1",
        priority="P1",
        assignee_id=me.id,
        due_date=date(2026, 10, 15),
        labels=["supplier", "b-sample"],
        components=["Hardware"],
        origin_type="email",
    )
    make(
        "OIL",
        "Release gerber files",
        "Gerber release for B-sample",
        parent_key="OIL-1",
        priority="P3",
        assignee_id=bea.id,
        reporter_id=bea.id,
        due_date=date(2026, 11, 25),
        labels=["b-sample"],
        components=["Software"],
    )
    make(
        "OIL",
        "Check thermal pad",
        "Pad material unknown",
        issue_type="Sub-task",
        parent_key="OIL-3",
        priority="P4",
        assignee_id=carl.id,
    )
    make(
        "OIL",
        "Validate EMC report",
        "Needs 100% scope coverage",
        priority="P2",
        reporter_id=bea.id,
        due_date=date(2026, 11, 2),
        labels=["emc"],
    )
    make(
        "OIL",
        "Quote tooling",
        "Tooling cost quote",
        priority="P5",
        assignee_id=bea.id,
        due_date=date(2026, 10, 28),
        origin_type="jira",
    )
    make(
        "PLT",
        "Platform firmware",
        "Firmware platform for Gen6",
        issue_type="Epic",
        priority="P3",
        assignee_id=carl.id,
        due_date=date(2026, 12, 1),
        components=["Firmware"],
        origin_type="confluence",
    )
    make(
        "PLT",
        "bootloader update",
        "flash_layout v2 migration",
        parent_key="PLT-1",
        priority="P1",
        assignee_id=me.id,
        due_date=date(2026, 10, 26),
    )

    issues.update_issue(conn, "OIL-2", {"due_date": date(2026, 10, 20)}, actor_id=me.id)
    issues.update_issue(conn, "OIL-3", {"due_date": date(2026, 10, 30)}, actor_id=me.id)
    issues.transition_issue(conn, "OIL-1", "In Progress", actor_id=me.id)
    issues.transition_issue(conn, "OIL-2", "Blocked", actor_id=me.id, reason="waiting for supplier")
    issues.transition_issue(conn, "OIL-4", "Done", actor_id=me.id)
    issues.transition_issue(conn, "OIL-5", "Ready to Proceed", actor_id=me.id)
    issues.transition_issue(conn, "OIL-6", "Cancelled", actor_id=me.id, reason="not needed")
    issues.transition_issue(conn, "PLT-1", "In Progress", actor_id=me.id)

    comments.add_comment(conn, "OIL-2", "Supplier confirmed 26 weeks", author_id=me.id)
    comments.add_comment(conn, "OIL-3", "Gerber check pending", author_id=me.id)
    deleted = comments.add_comment(conn, "OIL-5", "secretword was posted here", author_id=me.id)
    comments.delete_comment(conn, deleted.id, actor_id=me.id)

    issue_links.link_issues(conn, "OIL-2", "blocks", "OIL-3", actor_id=me.id)
    issue_links.link_issues(conn, "OIL-6", "duplicates", "OIL-2", actor_id=me.id)
    issue_links.link_issues(conn, "OIL-5", "relates_to", "OIL-3", actor_id=me.id)

    connector_id = conn.execute(
        text("INSERT INTO connector (type, name) VALUES ('fake', 'Fake') RETURNING id")
    ).scalar_one()

    def source(object_type: str, external_id: str, display_ref: str, *, parent_id: int | None = None) -> int:
        meta = ObjectMeta(object_type, external_id, title=display_ref, display_ref=display_ref)
        return store.upsert_object(conn, connector_id, meta, parent_id=parent_id)

    def changed(object_id: int, when: datetime) -> None:
        conn.execute(
            text("UPDATE source_object SET last_changed_at = :at WHERE id = :id"),
            {"at": utc_iso(when), "id": object_id},
        )

    email = source("email_thread", "thread-1", "Inductor delay")
    store.write_entry(
        conn,
        email,
        EntryData(
            "email_message", "m1", "2026-10-20T08:00:00Z", "Mail from Jane: zebra tooling date confirmed"
        ),
    )
    attachment = source("attachment", "thread-1#1", "drawing.pdf", parent_id=email)
    jira = source("jira_issue", "HWDEV-1187", "HWDEV-1187")
    page = source("confluence_page", "page-1", "Power spec")
    store.write_version(conn, page, VersionData("torque value 5 Nm"))
    file = source("file", "file-1", "BOM.xlsx")
    note = source("note", "note-1", "Meeting note")
    web = source("web_link", "web-1", "Supplier portal")
    for key, object_id in (
        ("OIL-2", email),
        ("OIL-6", email),
        ("OIL-3", jira),
        ("OIL-1", page),
        ("OIL-4", file),
        ("OIL-5", note),
        ("PLT-1", web),
    ):
        linking.link_source(conn, key, object_id, actor_id=me.id)
    linking.unlink_source(conn, "OIL-6", email, actor_id=me.id)
    changed(email, at(10, 20, 10))
    changed(attachment, at(10, 28, 8))
    changed(jira, at(10, 20, 10))
    changed(page, at(10, 10, 10))
    changed(web, at(10, 27, 12))

    for key, (created, updated, resolved) in TIMES.items():
        conn.execute(
            text(
                "UPDATE issue SET created_at = :created, updated_at = :updated, resolved_at = :resolved"
                " WHERE key = :key"
            ),
            {
                "created": utc_iso(created),
                "updated": utc_iso(updated),
                "resolved": utc_iso(resolved) if resolved else None,
                "key": key,
            },
        )


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def keys(conn: sqlalchemy.Connection, query: str) -> list[str]:
    return [issue.key for issue in compiler.search_issues(conn, query)]


def default_order(*wanted: str) -> list[str]:
    assert set(wanted) <= set(DEFAULT_ORDER), wanted
    return [key for key in DEFAULT_ORDER if key in wanted]


def everything_but(*excluded: str) -> list[str]:
    assert set(excluded) <= set(DEFAULT_ORDER), excluded
    return [key for key in DEFAULT_ORDER if key not in excluded]


def error_of(conn: sqlalchemy.Connection, query: str) -> oql.OqlSyntaxError:
    with pytest.raises(oql.OqlSyntaxError) as info:
        compiler.compile_query(conn, query)
    return info.value


FIELD_CASES = [
    # project
    ("project = oil", ["OIL-1", "OIL-2", "OIL-3", "OIL-4", "OIL-5", "OIL-6"]),
    ("PROJECT IN (plt)", ["PLT-1", "PLT-2"]),
    ("project != OIL", ["PLT-1", "PLT-2"]),
    ("project not in (oil, XYZ)", ["PLT-1", "PLT-2"]),
    # key
    ("key = oil-3", ["OIL-3"]),
    ("key in (oil-1, plt-2)", ["OIL-1", "PLT-2"]),
    ("key != OIL-3", everything_but("OIL-3")),
    ("key not in (OIL-3, OIL-2, PLT-1)", everything_but("OIL-3", "OIL-2", "PLT-1")),
    # type
    ("type = epic", ["OIL-1", "PLT-1"]),
    ("type in (task, sub-task)", everything_but("OIL-1", "PLT-1")),
    ("type != Epic", everything_but("OIL-1", "PLT-1")),
    ("type not in (Epic, Task)", ["OIL-4"]),
    # status
    ("status = blocked", ["OIL-2"]),
    ("status in (new, DONE)", ["OIL-3", "PLT-2", "OIL-4"]),
    ("status != Blocked", everything_but("OIL-2")),
    ("status not in (New, Done, Cancelled)", ["OIL-1", "OIL-2", "OIL-5", "PLT-1"]),
    # statusCategory
    ('statusCategory = "to do"', ["OIL-3", "PLT-2", "OIL-5"]),
    ('statusCategory = "In Progress"', ["OIL-1", "OIL-2", "PLT-1"]),
    ("STATUSCATEGORY = done", ["OIL-4", "OIL-6"]),
    ('statusCategory in ("to do", done)', ["OIL-3", "PLT-2", "OIL-5", "OIL-4", "OIL-6"]),
    ("statusCategory != Done", everything_but("OIL-4", "OIL-6")),
    ('statusCategory not in ("To Do", "In Progress")', ["OIL-4", "OIL-6"]),
    # priority
    ("priority = p1", ["OIL-2", "PLT-2"]),
    ("priority in (p2, P3)", ["OIL-1", "OIL-5", "OIL-3", "PLT-1"]),
    ("priority != P1", everything_but("OIL-2", "PLT-2")),
    ("priority not in (P1, P2, P3)", ["OIL-4", "OIL-6"]),
    # origin
    ("origin = email", ["OIL-2"]),
    ("origin in (confluence, JIRA)", ["OIL-6", "PLT-1"]),
    ("origin != manual", ["OIL-2", "OIL-6", "PLT-1"]),
    ("origin not in (manual, email)", ["OIL-6", "PLT-1"]),
    # assignee
    ("assignee = me()", ["OIL-1", "OIL-2", "PLT-2"]),
    ("assignee = me", ["OIL-1", "OIL-2", "PLT-2"]),
    ('assignee = "Bea Brandt"', ["OIL-3", "OIL-6"]),
    ("assignee = bea@example.com", ["OIL-3", "OIL-6"]),
    ('assignee in (me(), "carl conrad")', ["OIL-1", "OIL-2", "PLT-2", "OIL-4", "PLT-1"]),
    ("assignee != me()", ["OIL-3", "OIL-4", "OIL-5", "OIL-6", "PLT-1"]),
    ('assignee not in (me(), "Bea Brandt")', ["OIL-4", "OIL-5", "PLT-1"]),
    ("assignee is empty", ["OIL-5"]),
    ("assignee is not empty", everything_but("OIL-5")),
    # reporter
    ("reporter = me()", everything_but("OIL-3", "OIL-5")),
    ("reporter = bea@example.com", ["OIL-3", "OIL-5"]),
    ('reporter in ("Bea Brandt", "Carl Conrad")', ["OIL-3", "OIL-5"]),
    ("reporter != me()", ["OIL-3", "OIL-5"]),
    ("reporter not in (bea@example.com)", everything_but("OIL-3", "OIL-5")),
    ("reporter is empty", []),
    ("reporter is not empty", DEFAULT_ORDER),
    # label
    ("label = supplier", ["OIL-2", "OIL-1"]),
    ("label = B-SAMPLE", ["OIL-3", "OIL-2"]),
    ("label in (emc, Supplier)", ["OIL-2", "OIL-1", "OIL-5"]),
    ("label != supplier", everything_but("OIL-1", "OIL-2")),
    ("label not in (supplier, emc)", everything_but("OIL-1", "OIL-2", "OIL-5")),
    ("label is empty", ["PLT-2", "OIL-6", "OIL-4", "PLT-1"]),
    ("label is not empty", ["OIL-3", "OIL-2", "OIL-1", "OIL-5"]),
    # component
    ("component = hardware", ["OIL-2", "OIL-1"]),
    ("component = FIRMWARE", ["PLT-1"]),
    ("component in (software, firmware)", ["OIL-3", "PLT-1"]),
    ("component != Hardware", everything_but("OIL-1", "OIL-2")),
    ("component not in (Hardware, Software)", everything_but("OIL-1", "OIL-2", "OIL-3")),
    ("component is empty", ["PLT-2", "OIL-6", "OIL-4", "OIL-5"]),
    ("component is not empty", ["OIL-3", "OIL-2", "OIL-1", "PLT-1"]),
    # parent
    ("parent = oil-1", ["OIL-3", "OIL-2"]),
    ("parent in (OIL-3, PLT-1)", ["PLT-2", "OIL-4"]),
    ("parent != OIL-1", everything_but("OIL-2", "OIL-3")),
    ("parent not in (OIL-1, PLT-1)", ["OIL-1", "OIL-6", "OIL-4", "PLT-1", "OIL-5"]),
    ("parent is empty", ["OIL-1", "OIL-6", "PLT-1", "OIL-5"]),
    ("parent is not empty", ["OIL-3", "OIL-2", "PLT-2", "OIL-4"]),
    ("parent = OIL-99", []),
    # issue links
    ("issueLink = OIL-3", ["OIL-2", "OIL-5"]),
    ("issueLink = OIL-2", ["OIL-3", "OIL-6"]),
    ("issueLink in (oil-2, OIL-3)", ["OIL-3", "OIL-2", "OIL-6", "OIL-5"]),
    ("blocks = OIL-3", ["OIL-2"]),
    ("blocks = OIL-2", []),
    ("blocks in (OIL-3, OIL-9)", ["OIL-2"]),
    ("blockedBy = OIL-2", ["OIL-3"]),
    ("blockedBy = OIL-3", []),
    ("blockedBy in (OIL-2, OIL-5)", ["OIL-3"]),
    ("duplicates = OIL-2", ["OIL-6"]),
    ("duplicates = OIL-6", []),
    ("duplicates in (OIL-2, OIL-1)", ["OIL-6"]),
    # due (revised_due_date)
    ("due = 2026-10-28", ["OIL-6"]),
    ("due = startOfDay()", ["OIL-6"]),
    ("due != 2026-10-28", everything_but("OIL-6", "OIL-4")),
    ("due > 2026-10-28", ["OIL-3", "OIL-5", "OIL-1", "PLT-1"]),
    ("due >= 2026-10-28", ["OIL-3", "OIL-6", "OIL-5", "OIL-1", "PLT-1"]),
    ("due < startOfDay()", ["OIL-2", "PLT-2"]),
    ("due <= startOfDay()", ["OIL-2", "PLT-2", "OIL-6"]),
    ("due > endOfDay()", ["OIL-3", "OIL-5", "OIL-1", "PLT-1"]),
    ("due >= startOfWeek() AND due <= endOfWeek()", ["OIL-3", "PLT-2", "OIL-6"]),
    ("due >= -7d", everything_but("OIL-2", "OIL-4")),
    ("due < +2w", ["OIL-3", "OIL-2", "PLT-2", "OIL-6", "OIL-5"]),
    ("due >= startOfWeek(-1w) AND due < startOfWeek()", ["OIL-2"]),
    ("due <= endOfWeek(+1w)", ["OIL-3", "OIL-2", "PLT-2", "OIL-6", "OIL-5"]),
    ("due is empty", ["OIL-4"]),
    ("due is not empty", everything_but("OIL-4")),
    # originalDue
    ("originalDue < 2026-10-16", ["OIL-2"]),
    ("originalDue = 2026-11-25", ["OIL-3"]),
    ("originalDue != 2026-10-28", everything_but("OIL-6", "OIL-4")),
    ("originalDue > 2026-11-14", ["OIL-3", "OIL-1", "PLT-1"]),
    ("originalDue >= 2026-11-15", ["OIL-3", "OIL-1", "PLT-1"]),
    ("originalDue <= 2026-10-26", ["OIL-2", "PLT-2"]),
    ("originalDue is empty", ["OIL-4"]),
    ("originalDue is not empty", everything_but("OIL-4")),
    # created
    ("created = 2026-10-26", ["PLT-2"]),
    ("created = 2026-10-25", []),
    ("created != 2026-10-26", everything_but("PLT-2")),
    ("created >= 2026-10-26", ["PLT-2"]),
    ("created > 2026-10-26", []),
    ("created < 2026-10-05", ["OIL-1", "PLT-1"]),
    ("created <= 2026-10-05", ["OIL-1", "PLT-1"]),
    ("created >= startOfWeek()", ["PLT-2"]),
    ("created < -20d", ["OIL-2", "OIL-1", "PLT-1"]),
    ("created <= endOfWeek(-1w)", everything_but("PLT-2")),
    ("created is empty", []),
    ("created is not empty", DEFAULT_ORDER),
    # updated
    ("updated = 2026-10-27", ["OIL-2"]),
    ("updated != 2026-10-27", everything_but("OIL-2")),
    ("updated <= -5d", ["OIL-6", "OIL-1", "OIL-4", "PLT-1", "OIL-5"]),
    ("updated >= now()", []),
    ("updated > startOfDay()", ["OIL-3"]),
    ("updated >= startOfDay(-1d)", ["OIL-3", "OIL-2"]),
    ("updated >= endOfDay(-1d)", ["OIL-3"]),
    ("updated < startOfWeek()", everything_but("OIL-3", "OIL-2", "PLT-2")),
    ("updated is empty", []),
    ("updated is not empty", DEFAULT_ORDER),
    # resolved
    ("resolved is empty", everything_but("OIL-4", "OIL-6")),
    ("resolved is not empty", ["OIL-6", "OIL-4"]),
    ("resolved >= 2026-10-21", ["OIL-6"]),
    ("resolved < 2026-10-21", ["OIL-4"]),
    ("resolved = 2026-10-22", ["OIL-6"]),
    ("resolved != 2026-10-22", ["OIL-4"]),
    ("resolved <= endOfDay(-6d)", ["OIL-6", "OIL-4"]),
    # sourceUpdated: newest change over the linked objects, children included
    ("sourceUpdated >= -2d", ["OIL-2", "PLT-1"]),
    ("sourceUpdated >= 2026-10-28", ["OIL-2"]),
    ("sourceUpdated > 2026-10-20", ["OIL-3", "OIL-2", "PLT-1"]),
    ("sourceUpdated < 2026-10-15", ["OIL-1"]),
    ("sourceUpdated <= 2026-10-10", []),
    ("sourceUpdated is empty", ["PLT-2", "OIL-6", "OIL-4", "OIL-5"]),
    ("sourceUpdated is not empty", ["OIL-3", "OIL-2", "OIL-1", "PLT-1"]),
    # summary and description
    ("summary ~ INDUCTOR", ["OIL-2"]),
    ("summary ~ report", ["OIL-5"]),
    ('summary ~ "lead time"', ["OIL-2"]),
    ("summary !~ o", ["OIL-3", "OIL-4"]),
    ("summary ~ BOOTLOADER", ["PLT-2"]),
    ("description ~ inductor", ["OIL-1"]),
    ("description ~ QUOTE", ["OIL-2", "OIL-6"]),
    ("description !~ quote", everything_but("OIL-2", "OIL-6")),
    # comment: live comments only
    ("comment ~ supplier", ["OIL-2"]),
    ("comment ~ SECRETWORD", []),
    ("comment !~ pending", everything_but("OIL-3")),
    ("comment !~ supplier", everything_but("OIL-2")),
    # text: issue, comments and linked sources (SRCH-2)
    ("text ~ zebra", ["OIL-2"]),
    ("text !~ zebra", everything_but("OIL-2")),
    ("text ~ inductor", ["OIL-2", "OIL-1"]),
    ('text ~ "inductor lead time"', ["OIL-2"]),
    ("text ~ torque", ["OIL-1"]),
    ("text ~ tooling", ["OIL-2", "OIL-6"]),
    ("text ~ jane", ["OIL-2"]),
    ("text ~ thermal", ["OIL-4"]),
    # source: active links by object type
    ("source = email", ["OIL-2"]),
    ("source = JIRA", ["OIL-3"]),
    ("source = confluence", ["OIL-1"]),
    ("source = sharepoint", ["OIL-4"]),
    ("source = note", ["OIL-5"]),
    ("source = web", ["PLT-1"]),
    ("source in (email, jira)", ["OIL-3", "OIL-2"]),
    ("source != email", everything_but("OIL-2")),
    ("source not in (email, web, note)", everything_but("OIL-2", "PLT-1", "OIL-5")),
    # sourceRef
    ('sourceRef = "hwdev-1187"', ["OIL-3"]),
    ("sourceRef = BOM.xlsx", ["OIL-4"]),
    ('sourceRef = "drawing.pdf"', []),
    ("sourceRef ~ delay", ["OIL-2"]),
    ("sourceRef ~ SUPPLIER", ["PLT-1"]),
    ('sourceRef != "HWDEV-1187"', everything_but("OIL-3")),
    ('sourceRef ~ "%"', []),
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize(("query", "expected"), FIELD_CASES, ids=[case[0] for case in FIELD_CASES])
def test_field_and_operator(conn: sqlalchemy.Connection, query: str, expected: list[str]) -> None:
    assert keys(conn, query) == default_order(*expected)


SPEC_EXAMPLES = [
    ("status = Blocked AND assignee = me() ORDER BY priority, due", ["OIL-2"]),
    ("project = OIL AND statusCategory != Done AND due < startOfDay() ORDER BY due", ["OIL-2"]),
    ('text ~ "inductor lead time" AND label IN (supplier, b-sample)', ["OIL-2"]),
    ("source = email AND sourceUpdated >= -2d", ["OIL-2"]),
    ('priority IN (P1, P2) AND updated <= -5d AND statusCategory = "In Progress"', ["OIL-1"]),
    ("parent = OIL-10 OR issueLink = OIL-10", []),
    ("parent = OIL-1 OR issueLink = OIL-5", ["OIL-3", "OIL-2"]),
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize(("query", "expected"), SPEC_EXAMPLES, ids=[case[0] for case in SPEC_EXAMPLES])
def test_spec_examples(conn: sqlalchemy.Connection, query: str, expected: list[str]) -> None:
    assert keys(conn, query) == expected


PRECEDENCE_CASES = [
    ("project = PLT OR project = OIL AND status = Done", ["PLT-2", "OIL-4", "PLT-1"]),
    ("(project = PLT OR project = OIL) AND status = Done", ["OIL-4"]),
    ("status = Done AND project = OIL OR project = PLT", ["PLT-2", "OIL-4", "PLT-1"]),
    ("NOT project = OIL AND priority = P1", ["PLT-2"]),
    ("NOT (project = OIL AND priority = P1)", everything_but("OIL-2")),
    ("NOT NOT status = Blocked", ["OIL-2"]),
    ("NOT status = Blocked OR priority = P1", DEFAULT_ORDER),
    ("NOT (status = Blocked OR priority = P1)", everything_but("OIL-2", "PLT-2")),
    ("NOT label IN (supplier) AND NOT label IN (emc)", everything_but("OIL-1", "OIL-2", "OIL-5")),
    ("status = New OR status = Done OR priority = P5", ["OIL-3", "PLT-2", "OIL-6", "OIL-4"]),
    ("priority = P1 AND status = New AND project = PLT", ["PLT-2"]),
    # NULL satisfies no comparison, but its negation holds
    ("NOT due < 2026-10-28", ["OIL-3", "OIL-1", "OIL-6", "OIL-4", "PLT-1", "OIL-5"]),
    ("NOT assignee = me()", everything_but("OIL-1", "OIL-2", "PLT-2")),
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize(("query", "expected"), PRECEDENCE_CASES, ids=[case[0] for case in PRECEDENCE_CASES])
def test_boolean_precedence(conn: sqlalchemy.Connection, query: str, expected: list[str]) -> None:
    assert keys(conn, query) == default_order(*expected)


ORDER_CASES = [
    ("project", ["OIL-6", "OIL-5", "OIL-4", "OIL-3", "OIL-2", "OIL-1", "PLT-2", "PLT-1"]),
    ("project DESC", ["PLT-2", "PLT-1", "OIL-6", "OIL-5", "OIL-4", "OIL-3", "OIL-2", "OIL-1"]),
    ("key", ["OIL-1", "OIL-2", "OIL-3", "OIL-4", "OIL-5", "OIL-6", "PLT-1", "PLT-2"]),
    ("key desc", ["PLT-2", "PLT-1", "OIL-6", "OIL-5", "OIL-4", "OIL-3", "OIL-2", "OIL-1"]),
    ("type", ["PLT-1", "OIL-1", "PLT-2", "OIL-6", "OIL-5", "OIL-3", "OIL-2", "OIL-4"]),
    ("type DESC", ["OIL-4", "PLT-2", "OIL-6", "OIL-5", "OIL-3", "OIL-2", "PLT-1", "OIL-1"]),
    ("status", ["PLT-2", "OIL-3", "OIL-5", "PLT-1", "OIL-1", "OIL-2", "OIL-4", "OIL-6"]),
    ("status DESC", ["OIL-6", "OIL-4", "OIL-2", "PLT-1", "OIL-1", "OIL-5", "PLT-2", "OIL-3"]),
    ("statusCategory", ["PLT-2", "OIL-5", "OIL-3", "PLT-1", "OIL-2", "OIL-1", "OIL-6", "OIL-4"]),
    ("statusCategory DESC", ["OIL-6", "OIL-4", "PLT-1", "OIL-2", "OIL-1", "PLT-2", "OIL-5", "OIL-3"]),
    ("priority", ["PLT-2", "OIL-2", "OIL-5", "OIL-1", "PLT-1", "OIL-3", "OIL-4", "OIL-6"]),
    ("priority DESC", ["OIL-6", "OIL-4", "PLT-1", "OIL-3", "OIL-5", "OIL-1", "PLT-2", "OIL-2"]),
    ("assignee", ["PLT-2", "OIL-2", "OIL-1", "OIL-6", "OIL-3", "PLT-1", "OIL-4", "OIL-5"]),
    ("assignee DESC", ["PLT-1", "OIL-4", "OIL-6", "OIL-3", "PLT-2", "OIL-2", "OIL-1", "OIL-5"]),
    ("reporter", ["PLT-2", "PLT-1", "OIL-6", "OIL-4", "OIL-2", "OIL-1", "OIL-5", "OIL-3"]),
    ("reporter DESC", ["OIL-5", "OIL-3", "PLT-2", "PLT-1", "OIL-6", "OIL-4", "OIL-2", "OIL-1"]),
    ("due", ["OIL-2", "PLT-2", "OIL-6", "OIL-3", "OIL-5", "OIL-1", "PLT-1", "OIL-4"]),
    ("due DESC", ["PLT-1", "OIL-1", "OIL-5", "OIL-3", "OIL-6", "PLT-2", "OIL-2", "OIL-4"]),
    ("originalDue", ["OIL-2", "PLT-2", "OIL-6", "OIL-5", "OIL-1", "OIL-3", "PLT-1", "OIL-4"]),
    ("originalDue DESC", ["PLT-1", "OIL-3", "OIL-1", "OIL-5", "OIL-6", "PLT-2", "OIL-2", "OIL-4"]),
    ("created", ["PLT-1", "OIL-1", "OIL-2", "OIL-3", "OIL-4", "OIL-5", "OIL-6", "PLT-2"]),
    ("created DESC", ["PLT-2", "OIL-6", "OIL-5", "OIL-4", "OIL-3", "OIL-2", "OIL-1", "PLT-1"]),
    ("updated", ["OIL-5", "PLT-1", "OIL-4", "OIL-1", "OIL-6", "PLT-2", "OIL-2", "OIL-3"]),
    ("updated DESC", ["OIL-3", "OIL-2", "PLT-2", "OIL-6", "OIL-1", "OIL-4", "PLT-1", "OIL-5"]),
    ("resolved", ["OIL-4", "OIL-6", "PLT-2", "PLT-1", "OIL-5", "OIL-3", "OIL-2", "OIL-1"]),
    ("resolved DESC", ["OIL-6", "OIL-4", "PLT-2", "PLT-1", "OIL-5", "OIL-3", "OIL-2", "OIL-1"]),
    ("summary", ["PLT-2", "OIL-4", "OIL-2", "PLT-1", "OIL-1", "OIL-6", "OIL-3", "OIL-5"]),
    ("summary DESC", ["OIL-5", "OIL-3", "OIL-6", "OIL-1", "PLT-1", "OIL-2", "OIL-4", "PLT-2"]),
    ("priority, due", ["OIL-2", "PLT-2", "OIL-5", "OIL-1", "OIL-3", "PLT-1", "OIL-4", "OIL-6"]),
    ("priority DESC, key", ["OIL-6", "OIL-4", "OIL-3", "PLT-1", "OIL-1", "OIL-5", "OIL-2", "PLT-2"]),
    ("PRIORITY desc, Due ASC", ["OIL-6", "OIL-4", "OIL-3", "PLT-1", "OIL-5", "OIL-1", "OIL-2", "PLT-2"]),
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize(("order", "expected"), ORDER_CASES, ids=[case[0] for case in ORDER_CASES])
def test_order_by(conn: sqlalchemy.Connection, order: str, expected: list[str]) -> None:
    assert keys(conn, f"ORDER BY {order}") == expected
    assert keys(conn, f"project in (OIL, PLT) order by {order}") == expected


@pytest.mark.usefixtures("world")
def test_default_order_is_updated_desc(conn: sqlalchemy.Connection) -> None:
    assert keys(conn, "") == DEFAULT_ORDER
    assert keys(conn, "project = OIL") == default_order("OIL-1", "OIL-2", "OIL-3", "OIL-4", "OIL-5", "OIL-6")
    assert keys(conn, "ORDER BY updated DESC") == DEFAULT_ORDER
    assert compiler.compile_query(conn, "").sql.endswith("ORDER BY i.updated_at DESC, i.id DESC")
    assert compiler.compile_query(conn, "ORDER BY key").sql.endswith(
        "ORDER BY p.key ASC, i.number ASC, i.id DESC"
    )


@pytest.mark.usefixtures("world")
def test_id_is_the_final_tie_break(conn: sqlalchemy.Connection) -> None:
    conn.execute(text("UPDATE issue SET priority = 'P1'"))
    assert keys(conn, "ORDER BY priority") == [
        "PLT-2",
        "PLT-1",
        "OIL-6",
        "OIL-5",
        "OIL-4",
        "OIL-3",
        "OIL-2",
        "OIL-1",
    ]
    assert keys(conn, "ORDER BY priority DESC") == keys(conn, "ORDER BY priority")


@pytest.mark.usefixtures("world")
def test_search_issues_returns_issues_and_honours_limit(conn: sqlalchemy.Connection) -> None:
    found = compiler.search_issues(conn, "project = PLT")
    assert all(isinstance(issue, issues.Issue) for issue in found)
    assert [issue.key for issue in found] == ["PLT-2", "PLT-1"]
    assert found[0] == issues.get_issue(conn, "PLT-2")
    assert [issue.key for issue in compiler.search_issues(conn, "", limit=3)] == DEFAULT_ORDER[:3]
    assert compiler.search_issues(conn, "", limit=0) == []


@pytest.mark.usefixtures("world")
def test_accepts_a_parsed_query(conn: sqlalchemy.Connection) -> None:
    ast = oql.parse("status = Blocked ORDER BY key")
    assert compiler.compile_query(conn, ast) == compiler.compile_query(conn, "status = Blocked ORDER BY key")
    assert [issue.key for issue in compiler.search_issues(conn, ast)] == ["OIL-2"]


@pytest.mark.usefixtures("world")
def test_compiled_query_is_frozen_and_parameterised(conn: sqlalchemy.Connection) -> None:
    compiled = compiler.compile_query(conn, "status = blocked AND summary ~ lead AND priority in (p1, p2)")
    assert compiled.sql.startswith("SELECT i.id FROM issue i")
    assert compiled.params == {"p0": "Blocked", "p1": "%lead%", "p2": "P1", "p3": "P2"}
    assert ":p0" in compiled.sql and ":p3" in compiled.sql
    with pytest.raises(dataclasses.FrozenInstanceError):
        compiled.sql = "SELECT 1"  # type: ignore[misc]


def test_field_names() -> None:
    assert compiler.FIELD_NAMES == (
        "project",
        "key",
        "type",
        "status",
        "statusCategory",
        "priority",
        "origin",
        "assignee",
        "reporter",
        "label",
        "component",
        "parent",
        "issueLink",
        "blocks",
        "blockedBy",
        "duplicates",
        "due",
        "originalDue",
        "created",
        "updated",
        "resolved",
        "sourceUpdated",
        "summary",
        "description",
        "comment",
        "text",
        "source",
        "sourceRef",
    )


ERROR_CASES = [
    # fields
    ("bogus = 1", "unknown field 'bogus'", "bogus"),
    ("statu = New", "unknown field 'statu' — did you mean 'status'?", "statu"),
    ("Asignee = me()", "unknown field 'Asignee' — did you mean 'assignee'?", "Asignee"),
    (
        "status = New AND statuscatgory = Done",
        "unknown field 'statuscatgory' — did you mean 'statusCategory'?",
        "statuscatgory",
    ),
    ("hasProposal = true", "field 'hasProposal' is not available before phase 3", "hasProposal"),
    (
        "status = New OR waitingOnMe = true",
        "field 'waitingOnMe' is not available before phase 4",
        "waitingOnMe",
    ),
    ("HASPROPOSAL = true", "field 'HASPROPOSAL' is not available before phase 3", "HASPROPOSAL"),
    # enum values
    ("status = Blokced", "status 'Blokced' — did you mean 'Blocked'?", "Blokced"),
    ("status in (New, Blokced)", "status 'Blokced' — did you mean 'Blocked'?", "Blokced"),
    ("priority = P7", "priority 'P7' is not one of: P1, P2, P3, P4, P5", "P7"),
    ("type = Story", "type 'Story' is not one of: Epic, Task, Sub-task", "Story"),
    ('statusCategory = "In Progres"', "statusCategory 'In Progres' — did you mean 'In Progress'?", '"In'),
    ("source = emial", "source 'emial' — did you mean 'email'?", "emial"),
    ("origin not in (manual, jria)", "origin 'jria' — did you mean 'jira'?", "jria"),
    # operators
    ("priority > P1", "operator '>' is not supported for priority", "priority"),
    ("status = New AND summary = x", "operator '=' is not supported for summary", "summary"),
    ("sourceUpdated = 2026-10-20", "operator '=' is not supported for sourceUpdated", "sourceUpdated"),
    ("sourceUpdated != 2026-10-20", "operator '!=' is not supported for sourceUpdated", "sourceUpdated"),
    ("LABEL ~ x", "operator '~' is not supported for label", "LABEL"),
    ("text = x", "operator '=' is not supported for text", "text"),
    ("blocks != OIL-1", "operator '!=' is not supported for blocks", "blocks"),
    ("blockedBy not in (OIL-1)", "operator 'not in' is not supported for blockedBy", "blockedBy"),
    ("project is empty", "operator 'is empty' is not supported for project", "project"),
    ("status is not empty", "operator 'is not empty' is not supported for status", "status"),
    ("due in (2026-10-20)", "operator 'in' is not supported for due", "due"),
    ("source ~ email", "operator '~' is not supported for source", "source"),
    ("comment = x", "operator '=' is not supported for comment", "comment"),
    ("sourceRef in (a)", "operator 'in' is not supported for sourceRef", "sourceRef"),
    ("sourceRef !~ a", "operator '!~' is not supported for sourceRef", "sourceRef"),
    ("issueLink != OIL-1", "operator '!=' is not supported for issueLink", "issueLink"),
    ("duplicates is empty", "operator 'is empty' is not supported for duplicates", "duplicates"),
    ("assignee ~ x", "operator '~' is not supported for assignee", "assignee"),
    ("parent < OIL-1", "operator '<' is not supported for parent", "parent"),
    # functions
    ("status = me()", "function 'me' is not supported here", "me"),
    ("assignee = now()", "function 'now' is not supported here", "now"),
    ("assignee = foo()", "function 'foo' is not supported here", "foo"),
    ("due = me()", "function 'me' is not supported here", "me"),
    ("due = foo()", "function 'foo' is not supported here", "foo"),
    ("label = startOfDay()", "function 'startOfDay' is not supported here", "startOfDay"),
    ("summary ~ now()", "function 'now' is not supported here", "now"),
    ("text ~ me()", "function 'me' is not supported here", "me"),
    ("assignee = me(1)", "me() takes no arguments", "1"),
    ("due = now(-1d)", "now() takes no arguments", "-1d"),
    ("due = startOfDay(-1w)", "invalid offset '-1w' for startOfDay() (use e.g. -1d or +1d)", "-1w"),
    ("due = endOfWeek(-1d)", "invalid offset '-1d' for endOfWeek() (use e.g. -1w or +1w)", "-1d"),
    ("due = startOfDay(foo)", "invalid offset 'foo' for startOfDay() (use e.g. -1d or +1d)", "foo"),
    ("due = startOfDay(-1d, -2d)", "invalid offset '-1d' for startOfDay() (use e.g. -1d or +1d)", "-1d"),
    # people
    ('assignee = "Nobody Here"', "unknown person 'Nobody Here'", '"Nobody'),
    ("reporter = ghost@example.com", "unknown person 'ghost@example.com'", "ghost"),
    ("assignee in (me(), ghost)", "unknown person 'ghost'", "ghost"),
    ("assignee != 99", "unknown person '99'", "99"),
    # dates
    ("due = 2026-13-45", "invalid date '2026-13-45' (use YYYY-MM-DD, -7d or a date function)", "2026"),
    ("due = tomorrow", "invalid date 'tomorrow' (use YYYY-MM-DD, -7d or a date function)", "tomorrow"),
    ("created > 20261028", "invalid date '20261028' (use YYYY-MM-DD, -7d or a date function)", "20261028"),
    ('updated < "soon"', "invalid date 'soon' (use YYYY-MM-DD, -7d or a date function)", '"soon"'),
    ("due >= -99999999d", "invalid date '-99999999d' (use YYYY-MM-DD, -7d or a date function)", "-99999999d"),
    ("due >= +100000m", "invalid date '+100000m' (use YYYY-MM-DD, -7d or a date function)", "+100000m"),
    # text search and ordering
    ('text ~ ""', "empty search text", '""'),
    ('status = New AND text ~ "  "', "empty search text", '"  "'),
    ("ORDER BY description", "cannot order by 'description'", "description"),
    ("status = New ORDER BY label", "cannot order by 'label'", "label"),
    ("ORDER BY hasProposal", "cannot order by 'hasProposal'", "hasProposal"),
    ("ORDER BY priority, sourceUpdated", "cannot order by 'sourceUpdated'", "sourceUpdated"),
    ("ORDER BY priorty", "cannot order by 'priorty' — did you mean 'priority'?", "priorty"),
    ("status = New ORDER BY zzz", "cannot order by 'zzz'", "zzz"),
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize(
    ("query", "detail", "marker"), ERROR_CASES, ids=[f"{case[0]} -> {case[1]}" for case in ERROR_CASES]
)
def test_semantic_errors(conn: sqlalchemy.Connection, query: str, detail: str, marker: str) -> None:
    error = error_of(conn, query)
    assert error.detail == detail
    assert error.position == query.index(marker)
    assert isinstance(error, ValidationError)


@pytest.mark.usefixtures("world")
def test_syntax_errors_from_the_parser_propagate(conn: sqlalchemy.Connection) -> None:
    assert error_of(conn, "status =").detail == "expected a value"
    with pytest.raises(oql.OqlSyntaxError):
        compiler.search_issues(conn, "status = (")


def test_person_functions_need_a_me_person(conn: sqlalchemy.Connection) -> None:
    error = error_of(conn, "assignee = me()")
    assert "no 'me' person" in error.detail
    assert error.position == len("assignee = ")


@pytest.mark.usefixtures("world")
def test_enum_values_are_normalised_to_canonical_spelling(conn: sqlalchemy.Connection) -> None:
    compiled = compiler.compile_query(conn, 'status in ("ready to proceed", BLOCKED, "in PROGRESS")')
    assert list(compiled.params.values()) == ["Ready to Proceed", "Blocked", "In Progress"]
    compiled = compiler.compile_query(conn, "type = sub-task AND origin = CONFLUENCE AND priority = p3")
    assert list(compiled.params.values()) == ["Sub-task", "confluence", "P3"]
    compiled = compiler.compile_query(conn, 'statusCategory in (DONE, "to do")')
    assert list(compiled.params.values()) == ["New", "Ready to Proceed", "Done", "Cancelled"]
    compiled = compiler.compile_query(conn, "source in (EMAIL, Web)")
    assert list(compiled.params.values()) == ["email_thread", "web_link"]


@pytest.mark.usefixtures("world")
def test_keywords_and_fields_are_case_insensitive(conn: sqlalchemy.Connection) -> None:
    assert keys(conn, "STATUS = blocked and ASSIGNEE = ME() order by KEY desc") == ["OIL-2"]
    assert keys(conn, "Status Not In (Done, Cancelled) AND Label Is Not Empty ORDER BY Key") == [
        "OIL-1",
        "OIL-2",
        "OIL-3",
        "OIL-5",
    ]


def test_relative_dates_and_date_functions_resolve_against_the_clock(conn: sqlalchemy.Connection) -> None:
    end = (23, 59, 59, 999_000)
    cases = {
        "now()": NOW,
        "startOfDay()": at(10, 28),
        "startOfDay(-1d)": at(10, 27),
        "startOfDay(+2d)": at(10, 30),
        "startOfDay(-30d)": at(9, 28),
        "endOfDay()": datetime(2026, 10, 28, *end),
        "endOfDay(-1d)": datetime(2026, 10, 27, *end),
        "startOfWeek()": at(10, 26),
        "startOfWeek(-1w)": at(10, 19),
        "startOfWeek(+1w)": at(11, 2),
        "endOfWeek()": datetime(2026, 11, 1, *end),
        "endOfWeek(-1w)": datetime(2026, 10, 25, *end),
        "endOfWeek(+2w)": datetime(2026, 11, 15, *end),
        "-7d": at(10, 21, 13),
        "+2d": at(10, 30, 13),
        "-2w": at(10, 14, 13),
        "+2w": at(11, 11, 13),
        "-1m": at(9, 28, 13),
        "+3m": at(1, 28, 13, year=2027),
        "-0d": NOW,
        "-10M": at(12, 28, 13, year=2025),
        "2026-10-26": at(10, 26),
    }
    for value, local in cases.items():
        assert compiler.compile_query(conn, f"updated >= {value}").params == {"p0": utc_iso(local)}, value
        assert compiler.compile_query(conn, f"sourceUpdated < {value}").params == {"p0": utc_iso(local)}, (
            value
        )
        assert compiler.compile_query(conn, f"due >= {value}").params == {"p0": local.date().isoformat()}, (
            value
        )
        assert compiler.compile_query(conn, f"originalDue <= {value}").params == {
            "p0": local.date().isoformat()
        }, value


@pytest.mark.parametrize(
    ("now", "value", "expected"),
    [
        (at(3, 31, 13), "-1m", "2026-02-28"),
        (at(1, 31, 13), "+1m", "2026-02-28"),
        (at(1, 31, 13, year=2028), "+1m", "2028-02-29"),
        (at(10, 31, 13), "+1m", "2026-11-30"),
        (at(12, 31, 13), "+2m", "2027-02-28"),
        (at(1, 15, 13), "-1m", "2025-12-15"),
        (at(3, 30, 13), "-1m", "2026-02-28"),
        (at(10, 28, 13), "-12m", "2025-10-28"),
        # week boundaries
        (at(11, 1, 13), "startOfWeek()", "2026-10-26"),
        (at(11, 1, 13), "endOfWeek()", "2026-11-01"),
        (at(11, 2, 0), "startOfWeek()", "2026-11-02"),
        (at(10, 26, 0), "startOfWeek()", "2026-10-26"),
        (at(10, 26, 0), "endOfWeek()", "2026-11-01"),
        (at(10, 25, 23, 59), "startOfWeek()", "2026-10-19"),
        (at(10, 25, 23, 59), "endOfWeek()", "2026-10-25"),
        (at(1, 1, 12), "startOfWeek()", "2025-12-29"),
        (at(1, 1, 12), "endOfWeek()", "2026-01-04"),
        (at(10, 28, 13), "startOfWeek(-1w)", "2026-10-19"),
        (at(10, 28, 13), "endOfDay(+4d)", "2026-11-01"),
    ],
)
def test_clock_edge_cases(
    conn: sqlalchemy.Connection, monkeypatch: pytest.MonkeyPatch, now: datetime, value: str, expected: str
) -> None:
    freeze(monkeypatch, now)
    assert compiler.compile_query(conn, f"due = {value}").params == {"p0": expected}


@pytest.mark.usefixtures("world")
def test_date_literal_equality_covers_the_local_day(conn: sqlalchemy.Connection) -> None:
    compiled = compiler.compile_query(conn, "created = 2026-10-26")
    assert compiled.params == {"p0": utc_iso(at(10, 26)), "p1": utc_iso(at(10, 27))}
    compiled = compiler.compile_query(conn, "created != 2026-10-26")
    assert compiled.params == {"p0": utc_iso(at(10, 26)), "p1": utc_iso(at(10, 27))}
    # anything other than a plain date literal compares the instant itself
    assert compiler.compile_query(conn, "created = now()").params == {"p0": utc_iso(NOW)}
    assert compiler.compile_query(conn, "created > 2026-10-26").params == {"p0": utc_iso(at(10, 26))}
    assert compiler.compile_query(conn, "created <= endOfDay()").params == {
        "p0": utc_iso(datetime(2026, 10, 28, 23, 59, 59, 999_000))
    }


@pytest.mark.usefixtures("world")
def test_timestamp_boundaries_use_the_stored_utc_text(conn: sqlalchemy.Connection) -> None:
    conn.execute(
        text("UPDATE issue SET updated_at = :at WHERE key = 'OIL-5'"),
        {"at": utc_iso(datetime(2026, 10, 28, 23, 59, 59, 999_000))},
    )
    conn.execute(text("UPDATE issue SET updated_at = :at WHERE key = 'OIL-4'"), {"at": utc_iso(at(10, 29))})
    assert keys(conn, "updated <= endOfDay() AND updated >= startOfDay() ORDER BY key") == ["OIL-3", "OIL-5"]
    assert keys(conn, "updated > endOfDay()") == ["OIL-4"]
    assert keys(conn, "updated = 2026-10-28 ORDER BY key") == ["OIL-3", "OIL-5"]
    assert keys(conn, "updated != 2026-10-28 AND updated > 2026-10-27 ORDER BY key") == ["OIL-2", "OIL-4"]


@pytest.mark.usefixtures("world")
def test_text_search_finds_an_issue_through_a_linked_email_entry(conn: sqlalchemy.Connection) -> None:
    # "zebra" only occurs inside an email message linked to OIL-2; OIL-6's link was removed (SRCH-2)
    assert keys(conn, "text ~ zebra") == ["OIL-2"]
    assert keys(conn, "summary ~ zebra OR description ~ zebra OR comment ~ zebra") == []
    assert keys(conn, "text ~ ZEBRA OR text ~ zeb") == ["OIL-2"]
    assert keys(conn, 'text ~ "tooling date"') == ["OIL-2"]
    assert keys(conn, "text ~ zebra AND source = email AND key = OIL-2") == ["OIL-2"]


@pytest.mark.usefixtures("world")
def test_text_is_combined_with_other_clauses_and_parameters(conn: sqlalchemy.Connection) -> None:
    compiled = compiler.compile_query(conn, "text ~ zebra AND text !~ gerber AND status = Blocked")
    assert compiled.params == {"p0": '"zebra"*', "p1": '"gerber"*', "p2": "Blocked"}
    assert ":q" not in compiled.sql
    assert keys(conn, "text ~ zebra AND text !~ gerber AND status = Blocked") == ["OIL-2"]
    assert keys(conn, "text ~ tooling AND NOT text ~ zebra") == ["OIL-6"]


@pytest.mark.usefixtures("world")
def test_wildcard_characters_match_literally(conn: sqlalchemy.Connection) -> None:
    assert keys(conn, 'description ~ "%"') == ["OIL-5"]
    assert keys(conn, 'description ~ "100%"') == ["OIL-5"]
    assert keys(conn, 'description ~ "1%0"') == []
    assert keys(conn, 'summary ~ "%"') == []
    assert keys(conn, "description ~ _") == ["PLT-2"]
    assert keys(conn, "description ~ flash_layout") == ["PLT-2"]
    assert keys(conn, 'description ~ "flash layout"') == []
    assert keys(conn, 'description ~ "flas_ layout"') == []
    assert keys(conn, "summary ~ _") == []
    assert keys(conn, "summary !~ _") == DEFAULT_ORDER
    assert keys(conn, 'description ~ "\\\\"') == []
    assert keys(conn, 'comment ~ "%"') == []
    assert keys(conn, "sourceRef ~ _") == []
    assert keys(conn, 'summary ~ ""') == DEFAULT_ORDER


INJECTION_VALUES = [
    "x' OR 1=1 --",
    '"; DROP TABLE issue; --',
    "'); DELETE FROM issue; --",
    "%",
    "a%b_c\\d",
    "OIL-1' OR '1'='1",
]
INJECTION_FIELDS = [
    "summary ~",
    "summary !~",
    "description ~",
    "comment ~",
    "comment !~",
    "text ~",
    "text !~",
    "label =",
    "label in (supplier,",
    "component =",
    "project =",
    "key =",
    "parent =",
    "issueLink =",
    "blocks =",
    "blockedBy =",
    "duplicates =",
    "sourceRef =",
    "sourceRef !=",
    "sourceRef ~",
]


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize("value", INJECTION_VALUES)
@pytest.mark.parametrize("prefix", INJECTION_FIELDS)
def test_user_text_is_never_part_of_the_sql(conn: sqlalchemy.Connection, prefix: str, value: str) -> None:
    suffix = ")" if prefix.endswith(",") else ""
    query = f"{prefix} {quote(value)}{suffix}"
    compiled = compiler.compile_query(conn, query)
    assert value not in compiled.sql
    assert "DROP" not in compiled.sql and "DELETE" not in compiled.sql
    found = keys(conn, query)
    negated = prefix.endswith(("!~", "!="))
    if prefix.startswith("label in"):
        assert found == ["OIL-2", "OIL-1"]
    elif negated:
        assert found == DEFAULT_ORDER
    else:
        assert found == (["OIL-5"] if (prefix, value) == ("description ~", "%") else [])
    assert conn.execute(text("SELECT count(*) FROM issue")).scalar_one() == 8


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize("value", INJECTION_VALUES)
def test_hostile_values_for_enums_people_and_dates_fail_cleanly(
    conn: sqlalchemy.Connection, value: str
) -> None:
    for prefix in (
        "status =",
        "priority in (P1,",
        "assignee =",
        "reporter =",
        "due >=",
        "created <",
        "source =",
    ):
        query = f"{prefix} {quote(value)}" + (")" if prefix.endswith(",") else "")
        error = error_of(conn, query)
        assert error.position == query.index('"')
    assert conn.execute(text("SELECT count(*) FROM issue")).scalar_one() == 8
    assert conn.execute(text("SELECT count(*) FROM sqlite_master WHERE name = 'issue'")).scalar_one() == 1


@pytest.mark.usefixtures("world")
def test_sql_text_has_no_user_values_in_a_combined_query(conn: sqlalchemy.Connection) -> None:
    query = (
        f"summary ~ {quote(INJECTION_VALUES[0])} OR label = {quote(INJECTION_VALUES[1])}"
        f" OR text ~ {quote(INJECTION_VALUES[2])} ORDER BY priority"
    )
    compiled = compiler.compile_query(conn, query)
    assert all(value not in compiled.sql for value in INJECTION_VALUES[:3])
    assert set(compiled.params) == {"p0", "p1", "p2"}
    assert keys(conn, query) == []
