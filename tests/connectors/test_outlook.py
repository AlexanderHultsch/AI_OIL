import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy

from ai_oil import timeutil
from ai_oil.config import load_config, write_default_config
from ai_oil.connectors.outlook import (
    SELECTED_REFERENCE,
    ConversationCandidate,
    OutlookConnector,
)
from ai_oil.connectors.outlook_session import (
    AttachmentRecord,
    MailAddress,
    MailRecord,
    OutlookError,
)
from ai_oil.domain import issues, people, projects
from ai_oil.errors import ValidationError
from ai_oil.search import fts
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    HealthCheck,
    ObjectMeta,
    ObjectState,
    SourceRef,
)
from ai_oil.sources.sync import SyncScope, run_sync

INBOX = "\\\\alex@example.com\\Inbox"
SENT = "\\\\alex@example.com\\Sent Items"
DELETED = "\\\\alex@example.com\\Deleted Items"
JANE = MailAddress("Jane Doe", "jane@acme.com")
ALEX = MailAddress("Alex", "alex@example.com")
BOB = MailAddress("Bob", "bob@acme.com")


def at(day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def mail(entry_id: str, **fields: object) -> MailRecord:
    values: dict[str, object] = {
        "entry_id": entry_id,
        "store_id": "S1",
        "conversation_id": "CONV1",
        "conversation_topic": "Inductor lead time",
        "internet_message_id": f"<{entry_id.lower()}@acme.com>",
        "in_reply_to": None,
        "references": None,
        "subject": "Inductor lead time",
        "sender": JANE,
        "to": (ALEX,),
        "cc": (),
        "sent_on": at(1),
        "received_on": at(1),
        "body": "Body",
        "html_body": "",
        "folder_path": INBOX,
        "attachments": (),
    }
    values.update(fields)
    return MailRecord(**values)  # type: ignore[arg-type]


class FakeSession:
    """An `OutlookSession` over scripted records; every call is logged."""

    def __init__(self, records: Sequence[MailRecord] = ()) -> None:
        self.records = list(records)
        self.selected: list[MailRecord] = []
        self.attachments: dict[tuple[str, int], bytes | Exception] = {}
        self.folders = [INBOX, SENT]
        self.health = [HealthCheck("com", True, "fake")]
        self.health_error: Exception | None = None
        self.calls: list[tuple] = []

    def folder_paths(self) -> list[str]:
        return list(self.folders)

    def selected_messages(self) -> list[MailRecord]:
        return list(self.selected)

    def find_by_subject(self, text: str, *, since: datetime | None, limit: int) -> list[MailRecord]:
        self.calls.append(("find_by_subject", text, since, limit))
        hits = [r for r in self.records if text.casefold() in r.subject.casefold()]
        return sorted(hits, key=lambda r: r.sent_on or r.received_on, reverse=True)[:limit]

    def find_by_message_id(self, internet_message_id: str) -> MailRecord | None:
        return next((r for r in self.records if r.internet_message_id == internet_message_id), None)

    def conversation(self, entry_id: str, store_id: str) -> list[MailRecord] | None:
        self.calls.append(("conversation", entry_id, store_id))
        anchor = next((r for r in self.records if (r.entry_id, r.store_id) == (entry_id, store_id)), None)
        if anchor is None:
            return None
        return [r for r in self.records if r.conversation_id == anchor.conversation_id]

    def conversation_by_topic(self, conversation_id: str, topic: str) -> list[MailRecord]:
        self.calls.append(("conversation_by_topic", conversation_id, topic))
        return [
            r for r in self.records if r.conversation_id == conversation_id and r.conversation_topic == topic
        ]

    def find_replies_to(self, internet_message_ids: Sequence[str]) -> list[MailRecord]:
        self.calls.append(("find_replies_to", tuple(internet_message_ids)))
        return [r for r in self.records if r.in_reply_to in internet_message_ids]

    def recent_messages(self, *, since: datetime) -> list[MailRecord]:
        self.calls.append(("recent_messages", since))
        hits = [r for r in self.records if (r.sent_on or r.received_on or since - timedelta(1)) >= since]
        return sorted(hits, key=lambda r: r.sent_on or r.received_on, reverse=True)

    def attachment_bytes(self, entry_id: str, store_id: str, index: int) -> bytes:
        self.calls.append(("attachment_bytes", entry_id, index))
        data = self.attachments[(entry_id, index)]
        if isinstance(data, Exception):
            raise data
        return data

    def check(self) -> list[HealthCheck]:
        if self.health_error is not None:
            raise self.health_error
        return list(self.health)

    def attachment_calls(self) -> list[tuple[str, int]]:
        return [(call[1], call[2]) for call in self.calls if call[0] == "attachment_bytes"]


def config(settings: dict | None = None, connector_id: int = 1) -> ConnectorConfig:
    return ConnectorConfig(connector_id, "outlook", "Outlook", True, settings or {}, None)


def connector(session: FakeSession, settings: dict | None = None) -> OutlookConnector:
    return OutlookConnector(config(settings), session=session)


def state(
    *,
    external_id: str = "CONV1",
    locator: str | None = "outlook:S1:E1",
    cursor: str | None = None,
    title: str | None = None,
    object_id: int = 1,
) -> ObjectState:
    return ObjectState(
        id=object_id,
        connector_id=1,
        object_type="email_thread",
        external_id=external_id,
        title=title,
        display_ref=None,
        locator=locator,
        cursor=cursor,
        parent_id=None,
        latest_content_hash=None,
    )


def thread_records() -> list[MailRecord]:
    first = mail(
        "E1",
        subject="Inductor lead time",
        body="Lead time is 26 weeks.\n\nBest regards\n--\nJane Doe, Acme",
        to=(ALEX, BOB),
        cc=(MailAddress("Carol", "Carol@Acme.com"),),
        sent_on=at(1),
        received_on=at(1, 10, 1),
        attachments=(
            AttachmentRecord(1, "logo.png", 5000, "logo@cid", False),
            AttachmentRecord(2, "spec.txt", 22, None, False),
        ),
    )
    reply = mail(
        "E2",
        subject="RE: Inductor lead time",
        sender=ALEX,
        to=(JANE,),
        body=(
            "Thanks, please confirm the tolerance.\n\n"
            "From: Jane Doe\nSent: Monday\nTo: Alex\nSubject: Inductor lead time\n\nLead time is 26 weeks."
        ),
        in_reply_to="<e1@acme.com>",
        references="<e1@acme.com>",
        sent_on=at(2),
        received_on=at(2),
        folder_path=SENT,
    )
    answer = mail(
        "E3",
        subject="AW: Inductor lead time",
        sender=MailAddress("Exchange Sys", "/O=EXCH/CN=RECIPIENTS/CN=JANE"),
        body="",
        html_body="<p>Tolerance is 5%.</p>",
        sent_on=at(3),
        received_on=at(3),
    )
    deleted = mail("E9", subject="RE: Inductor lead time", sent_on=at(4), folder_path=DELETED)
    return [first, reply, answer, deleted]


def thread_session() -> FakeSession:
    session = FakeSession(thread_records())
    session.attachments[("E1", 1)] = b"PNG" * 10
    session.attachments[("E1", 2)] = b"Torque 12 Nm confirmed"
    return session


# --- parse_reference -------------------------------------------------------------------------------------


def test_selected_without_a_selection_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no email is selected in Outlook"):
        connector(FakeSession()).parse_reference(SELECTED_REFERENCE)


def test_selected_mails_of_one_conversation_give_the_newest_one() -> None:
    session = FakeSession()
    older = mail("E1", subject="Inductor lead time", sent_on=at(1))
    newer = mail("E2", subject="RE: Inductor lead time", sent_on=at(2))
    session.selected = [newer, older]

    ref = connector(session).parse_reference(f"  {SELECTED_REFERENCE} ")

    assert ref == SourceRef(
        "email_thread",
        "CONV1",
        display_ref="Inductor lead time",
        locator="outlook:S1:E2",
        extra={"topic": "Inductor lead time"},
    )


def test_selected_mails_of_two_conversations_are_rejected_with_their_subjects() -> None:
    session = FakeSession()
    session.selected = [
        mail("E1", subject="Inductor lead time"),
        mail("E2", conversation_id="CONV2", subject="RE: Capacitor quote"),
    ]

    with pytest.raises(ValidationError) as caught:
        connector(session).parse_reference(SELECTED_REFERENCE)

    assert "Inductor lead time" in str(caught.value)
    assert "Capacitor quote" in str(caught.value)


def test_message_id_finds_the_conversation() -> None:
    session = FakeSession([mail("E5", subject="FW: Valve drift", internet_message_id="<m5@acme.com>")])

    ref = connector(session).parse_reference(" <m5@acme.com> ")

    assert ref is not None
    assert (ref.object_type, ref.external_id, ref.display_ref, ref.locator) == (
        "email_thread",
        "CONV1",
        "Valve drift",
        "outlook:S1:E5",
    )


def test_unknown_message_id_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        connector(FakeSession()).parse_reference("<nope@acme.com>")

    assert str(caught.value) == ("no email with message id <nope@acme.com> in the configured Outlook folders")


@pytest.mark.parametrize(
    "text", ["https://example.com/a", "OIL-1", "outlook:other", "<no-at-sign>", "<a@b> <c@d>", ""]
)
def test_other_text_is_not_an_outlook_reference(text: str) -> None:
    assert connector(FakeSession()).parse_reference(text) is None


# --- resolve ---------------------------------------------------------------------------------------------


def test_resolve_email_thread() -> None:
    ref = SourceRef("email_thread", "CONV1", display_ref="Inductor", locator="outlook:S1:E1")

    assert connector(FakeSession()).resolve(ref) == ObjectMeta(
        "email_thread", "CONV1", title="Inductor", display_ref="Inductor", locator="outlook:S1:E1"
    )


def test_resolve_rejects_other_object_types() -> None:
    with pytest.raises(ValidationError):
        connector(FakeSession()).resolve(SourceRef("web_link", "https://example.com"))


# --- fetch: entries --------------------------------------------------------------------------------------


def test_first_fetch_collects_the_conversation_from_the_configured_folders() -> None:
    session = thread_session()

    result = connector(session).fetch(state())

    assert result.status == "ok"
    assert result.version is None
    assert [e.external_id for e in result.entries] == ["<e1@acme.com>", "<e2@acme.com>", "<e3@acme.com>"]
    assert [e.occurred_at for e in result.entries] == [
        "2026-03-01T10:00:00.000Z",
        "2026-03-02T10:00:00.000Z",
        "2026-03-03T10:00:00.000Z",
    ]
    assert {e.entry_type for e in result.entries} == {"email_message"}
    assert all(e.direction is None for e in result.entries)
    assert result.title == "Inductor lead time"
    assert ("conversation", "E1", "S1") in session.calls


def test_entry_fields_of_an_incoming_mail() -> None:
    first = connector(thread_session()).fetch(state()).entries[0]

    assert first.text == "Lead time is 26 weeks.\n\nBest regards\n--\nJane Doe, Acme"
    assert first.new_text == "Lead time is 26 weeks.\n\nBest regards"
    assert first.author is not None
    assert (first.author.system, first.author.external_id, first.author.display_name) == (
        "email",
        "jane@acme.com",
        "Jane Doe",
    )
    assert [(r.external_id, r.display_name) for r in first.recipients] == [
        ("alex@example.com", "Alex"),
        ("bob@acme.com", "Bob"),
        ("carol@acme.com", "Carol"),
    ]
    assert first.metadata == {
        "subject": "Inductor lead time",
        "folder": INBOX,
        "to": ["alex@example.com", "bob@acme.com"],
        "cc": ["Carol@Acme.com"],
        "in_reply_to": None,
        "references": None,
        "attachment_count": 2,
    }


def test_reply_in_sent_items_keeps_only_the_new_text_and_the_reply_headers() -> None:
    reply = connector(thread_session()).fetch(state()).entries[1]

    assert reply.new_text == "Thanks, please confirm the tolerance."
    assert "Lead time is 26 weeks." in reply.text
    assert reply.metadata["folder"] == SENT
    assert reply.metadata["in_reply_to"] == "<e1@acme.com>"
    assert reply.metadata["references"] == "<e1@acme.com>"


def test_html_body_is_the_fallback_and_a_sender_without_address_has_no_author() -> None:
    answer = connector(thread_session()).fetch(state()).entries[2]

    assert answer.text == "Tolerance is 5%."
    assert answer.author is None
    assert answer.metadata["sender_raw"] == "/O=EXCH/CN=RECIPIENTS/CN=JANE"


def test_a_message_in_another_folder_is_excluded() -> None:
    result = connector(thread_session()).fetch(state())

    assert "<e9@acme.com>" not in [e.external_id for e in result.entries]


def test_signature_markers_come_from_the_settings() -> None:
    session = FakeSession([mail("E1", body="Done.\nMit freundlichen Grüßen\nJane")])

    default = connector(session).fetch(state())
    custom = connector(session, {"signature_markers": ["Mit freundlichen"]}).fetch(state())

    assert default.entries[0].new_text == "Done.\nMit freundlichen Grüßen\nJane"
    assert custom.entries[0].new_text == "Done."


def test_a_message_without_a_date_is_skipped_with_a_warning() -> None:
    session = FakeSession(
        [mail("E1"), mail("E2", subject="RE: Inductor lead time", sent_on=None, received_on=None)]
    )

    result = connector(session).fetch(state())

    assert [e.external_id for e in result.entries] == ["<e1@acme.com>"]
    assert result.warnings == ("skipped an email without a date: 'RE: Inductor lead time'",)


def test_an_entry_id_stands_in_for_a_missing_internet_message_id() -> None:
    session = FakeSession([mail("E1", internet_message_id=None)])

    result = connector(session).fetch(state())

    assert result.entries[0].external_id == "entryid:E1"
    assert ("find_replies_to", ()) not in session.calls


def test_the_received_time_stands_in_for_a_missing_sent_time() -> None:
    session = FakeSession([mail("E1", sent_on=None, received_on=at(5, 8, 30))])

    result = connector(session).fetch(state())

    assert result.entries[0].occurred_at == "2026-03-05T08:30:00.000Z"


def test_the_first_fetch_cursor_and_title() -> None:
    result = connector(thread_session()).fetch(state())

    assert result.cursor is not None
    assert json.loads(result.cursor) == {
        "anchor": ["E3", "S1"],
        "topic": "Inductor lead time",
        "latest": "2026-03-03T10:00:00.000Z",
    }
    assert result.cursor == json.dumps(json.loads(result.cursor), sort_keys=True)


# --- fetch: attachments ----------------------------------------------------------------------------------


def test_inline_logo_is_skipped_and_a_real_attachment_becomes_a_child() -> None:
    session = thread_session()

    result = connector(session).fetch(state())

    assert session.attachment_calls() == [("E1", 2)]
    [child] = result.children
    assert child.meta == ObjectMeta(
        "attachment",
        "<e1@acme.com>#2",
        title="spec.txt",
        display_ref="spec.txt",
        cursor=hashlib.sha256(b"Torque 12 Nm confirmed").hexdigest(),
    )
    assert child.version is not None
    assert child.version.text == "Torque 12 Nm confirmed"
    assert child.version.raw == b"Torque 12 Nm confirmed"
    assert child.version.media_type == "text/plain"
    assert child.version.metadata == {
        "filename": "spec.txt",
        "size": 22,
        "extract_status": "ok",
        "extract_detail": None,
        "truncated": False,
    }


def test_a_large_or_non_image_inline_attachment_is_kept() -> None:
    session = FakeSession(
        [
            mail(
                "E1",
                attachments=(
                    AttachmentRecord(1, "big.png", 30000, "cid1", False),
                    AttachmentRecord(2, "notes.txt", 10, "cid2", False),
                    AttachmentRecord(3, "small.PNG", 100, None, False),
                    AttachmentRecord(4, "tiny.jpg", 100, "cid4", False),
                ),
            )
        ]
    )
    for index in (1, 2, 3):
        session.attachments[("E1", index)] = b"data"

    result = connector(session).fetch(state())

    assert session.attachment_calls() == [("E1", 1), ("E1", 2), ("E1", 3)]
    assert [c.meta.display_ref for c in result.children] == ["big.png", "notes.txt", "small.PNG"]
    assert result.children[0].version is not None
    assert result.children[0].version.metadata["extract_status"] == "unsupported"


def test_the_inline_image_limit_is_a_setting() -> None:
    session = FakeSession([mail("E1", attachments=(AttachmentRecord(1, "banner.png", 30000, "cid", False),))])

    result = connector(session, {"inline_image_max_bytes": 40000}).fetch(state())

    assert result.children == ()
    assert session.attachment_calls() == []


def test_an_unreadable_attachment_is_a_warning() -> None:
    session = thread_session()
    session.attachments[("E1", 2)] = OutlookError("cannot read attachment 2")

    result = connector(session).fetch(state())

    assert result.children == ()
    assert len(result.entries) == 3
    assert result.warnings == (
        "cannot read attachment 'spec.txt' of 'Inductor lead time': cannot read attachment 2",
    )


def test_attachment_text_is_limited_by_max_text_chars() -> None:
    session = thread_session()
    session.attachments[("E1", 2)] = b"x" * 100

    result = connector(session, {"max_text_chars": 10}).fetch(state())

    version = result.children[0].version
    assert version is not None
    assert version.text == "x" * 10
    assert version.metadata["truncated"] is True


def test_the_second_fetch_asks_only_for_the_attachments_of_new_messages() -> None:
    session = thread_session()
    first = connector(session).fetch(state())
    session.calls.clear()
    session.records.append(
        mail(
            "E4",
            subject="RE: Inductor lead time",
            sent_on=at(5),
            attachments=(AttachmentRecord(1, "drawing.txt", 8, None, False),),
        )
    )
    session.attachments[("E4", 1)] = b"Drawing!"

    result = connector(session).fetch(state(cursor=first.cursor))

    assert session.attachment_calls() == [("E4", 1)]
    assert [c.meta.external_id for c in result.children] == ["<e4@acme.com>#1"]
    assert [e.external_id for e in result.entries] == [
        "<e1@acme.com>",
        "<e2@acme.com>",
        "<e3@acme.com>",
        "<e4@acme.com>",
    ]
    assert json.loads(result.cursor or "") == {
        "anchor": ["E4", "S1"],
        "topic": "Inductor lead time",
        "latest": "2026-03-05T10:00:00.000Z",
    }


def test_a_fetch_without_new_messages_asks_for_no_attachments() -> None:
    session = thread_session()
    first = connector(session).fetch(state())
    session.calls.clear()

    result = connector(session).fetch(state(cursor=first.cursor))

    assert session.attachment_calls() == []
    assert result.children == ()
    assert len(result.entries) == 3


# --- fetch: anchor fallback, gone, forks -----------------------------------------------------------------


def test_a_moved_anchor_falls_back_to_the_conversation_topic() -> None:
    session = thread_session()

    result = connector(session).fetch(state(locator="outlook:S1:MOVED", title="Inductor lead time"))

    assert ("conversation", "MOVED", "S1") in session.calls
    assert ("conversation_by_topic", "CONV1", "Inductor lead time") in session.calls
    assert len(result.entries) == 3


def test_a_moved_anchor_uses_the_topic_of_the_cursor() -> None:
    session = thread_session()
    cursor = json.dumps({"anchor": ["MOVED", "S1"], "topic": "Inductor lead time", "latest": None})

    result = connector(session).fetch(state(cursor=cursor, title="Something else"))

    assert ("conversation_by_topic", "CONV1", "Inductor lead time") in session.calls
    assert len(result.entries) == 3


def test_an_unparseable_locator_goes_straight_to_the_topic() -> None:
    session = thread_session()

    result = connector(session).fetch(state(locator=None, title="Inductor lead time"))

    assert not any(call[0] == "conversation" for call in session.calls)
    assert len(result.entries) == 3


def test_a_conversation_nobody_finds_is_gone() -> None:
    result = connector(FakeSession()).fetch(state())

    assert result.status == "gone"
    assert result.entries == ()


def test_a_conversation_only_in_other_folders_is_gone() -> None:
    session = FakeSession([mail("E1", folder_path=DELETED)])

    assert connector(session).fetch(state()).status == "gone"


def test_a_reply_in_another_conversation_is_reported_as_a_fork() -> None:
    session = thread_session()
    session.records.append(
        mail(
            "E7",
            conversation_id="CONV2",
            conversation_topic="Inductor lead time - new part number",
            subject="AW: Inductor lead time - new part number",
            in_reply_to="<e3@acme.com>",
            sent_on=at(6),
        )
    )
    session.records.append(
        mail(
            "E8",
            conversation_id="CONV2",
            conversation_topic="Inductor lead time - new part number",
            subject="Inductor lead time - new part number",
            in_reply_to="<e2@acme.com>",
            sent_on=at(7),
        )
    )
    session.records.append(mail("E6", in_reply_to="<e1@acme.com>", sent_on=at(8)))

    result = connector(session).fetch(state())

    assert result.warnings == (
        "forked conversation: 'Inductor lead time - new part number'"
        " — link it with: oil link <KEY> <e7@acme.com>",
    )
    [replies_call] = [call for call in session.calls if call[0] == "find_replies_to"]
    assert replies_call[1] == ("<e1@acme.com>", "<e2@acme.com>", "<e3@acme.com>", "<e6@acme.com>")


# --- find_conversations, scan_mailbox, search_candidates -------------------------------------------------


def listing_session() -> FakeSession:
    return FakeSession(
        [
            mail("A1", conversation_id="CA", subject="Inductor lead time", sent_on=at(1)),
            mail("A2", conversation_id="CA", subject="RE: Inductor lead time", sender=ALEX, sent_on=at(4)),
            mail("A3", conversation_id="CA", subject="AW: Inductor lead time", sent_on=at(2)),
            mail("B1", conversation_id="CB", subject="Inductor sample", sent_on=at(5), sender=BOB),
            mail(
                "C1",
                conversation_id="CC",
                subject="Inductor PPAP",
                sent_on=at(3),
                sender=MailAddress("", "ppap@acme.com"),
            ),
            mail("C2", conversation_id="CD", subject="Capacitor quote", sent_on=at(6)),
            mail("D1", conversation_id="CE", subject="Inductor orphan", sent_on=None, received_on=None),
        ]
    )


def test_find_conversations_groups_orders_and_describes() -> None:
    session = listing_session()
    session.records = [r for r in session.records if r.entry_id != "D1"]

    found = connector(session).find_conversations("inductor")

    assert [(c.subject, c.message_count, c.sender, c.latest) for c in found] == [
        ("Inductor sample", 1, "Bob", at(5)),
        ("Inductor lead time", 3, "Alex", at(4)),
        ("Inductor PPAP", 1, "ppap@acme.com", at(3)),
    ]
    assert found[1].ref == SourceRef(
        "email_thread",
        "CA",
        display_ref="Inductor lead time",
        locator="outlook:S1:A2",
        extra={"topic": "Inductor lead time"},
    )
    assert isinstance(found[0], ConversationCandidate)
    assert ("find_by_subject", "inductor", None, 100) in session.calls


def test_find_conversations_limit() -> None:
    session = listing_session()
    session.records = [r for r in session.records if r.entry_id != "D1"]

    found = connector(session).find_conversations("inductor", limit=2)

    assert [c.subject for c in found] == ["Inductor sample", "Inductor lead time"]
    assert ("find_by_subject", "inductor", None, 10) in session.calls


def test_find_conversations_without_a_sender_or_a_date() -> None:
    session = FakeSession([mail("D1", sender=None, sent_on=None, received_on=None)])

    [found] = connector(session).find_conversations("inductor")

    assert (found.sender, found.latest, found.message_count) == ("-", None, 1)


def test_search_candidates_are_object_metas() -> None:
    session = listing_session()
    session.records = [r for r in session.records if r.entry_id != "D1"]

    metas = connector(session).search_candidates(CandidateQuery("inductor", limit=2))

    assert metas == [
        ObjectMeta(
            "email_thread",
            "CB",
            title="Inductor sample",
            display_ref="Inductor sample",
            locator="outlook:S1:B1",
        ),
        ObjectMeta(
            "email_thread",
            "CA",
            title="Inductor lead time",
            display_ref="Inductor lead time",
            locator="outlook:S1:A2",
        ),
    ]


def test_scan_mailbox_defaults_to_the_scan_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "_now", lambda: now)
    session = listing_session()

    default = connector(session).scan_mailbox()
    short = connector(session, {"scan_days": 3}).scan_mailbox()

    since = [call[1] for call in session.calls if call[0] == "recent_messages"]
    assert since == [now - timedelta(days=14), now - timedelta(days=3)]
    assert len(default) == 4
    assert short == []


def test_scan_mailbox_with_an_explicit_start() -> None:
    session = listing_session()

    found = connector(session).scan_mailbox(since=at(4))

    assert [c.subject for c in found] == ["Capacitor quote", "Inductor sample", "Inductor lead time"]
    assert ("recent_messages", at(4)) in session.calls


# --- check, settings, registry ---------------------------------------------------------------------------


def test_check_passes_the_session_checks_through() -> None:
    session = FakeSession()
    session.health = [HealthCheck("com", True, "fine"), HealthCheck("inbox", False, "gone")]

    report = connector(session).check()

    assert report.checks == tuple(session.health)
    assert not report.ok


def test_check_turns_an_outlook_error_into_a_failed_report() -> None:
    session = FakeSession()
    session.health_error = OutlookError("classic Outlook is not available")

    report = connector(session).check()

    assert report.checks == (HealthCheck("com", False, "classic Outlook is not available"),)


def test_invalid_settings_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_folders"):
        connector(FakeSession(), {"extra_folders": "Archive"})
    with pytest.raises(ValidationError, match="include_subfolders"):
        connector(FakeSession(), {"include_subfolders": "no"})
    with pytest.raises(ValidationError, match="scan_days"):
        connector(FakeSession(), {"scan_days": True})


def test_the_default_session_does_no_com_work() -> None:
    created = OutlookConnector(config({"extra_folders": ["Archive/2026"], "include_subfolders": False}))

    assert created.type == "outlook"
    assert created.object_types == frozenset({"email_thread", "attachment"})
    assert (created.action_class, created.max_parallel) == ("A", 1)


def test_the_connector_is_registered_through_its_entry_point() -> None:
    assert registry.available_connector_types()["outlook"] is OutlookConnector


# --- end to end through the store and the sync engine ----------------------------------------------------


@pytest.fixture
def linked_thread(engine: sqlalchemy.Engine, tmp_path):
    home = tmp_path / "sync_home"
    write_default_config(home)
    session = thread_session()
    with engine.begin() as conn:
        me = people.create_person(conn, "Alex", is_me=True)
        people.add_identity(conn, me.id, "email", "alex@example.com")
        projects.create_project(conn, "OIL", "Open issues")
        issue = issues.create_issue(conn, "OIL", "Inductor delivery", "Chase the supplier", actor_id=me.id)
        row = registry.create_connector(conn, "outlook", "Outlook")
        ref = OutlookConnector(registry.to_config(row), session=session).parse_reference("<e1@acme.com>")
        assert ref is not None
        meta = OutlookConnector(registry.to_config(row), session=session).resolve(ref)
        object_id = store.upsert_object(conn, row.id, meta)
        linking.link_source(conn, issue.key, object_id, actor_id=me.id)

    def factory(connector_row: registry.ConnectorRow) -> OutlookConnector:
        return OutlookConnector(registry.to_config(connector_row), session=session)

    report = run_sync(
        engine, SyncScope("source", object_id), config=load_config(home), connector_factory=factory
    )
    return engine, issue.key, object_id, report


def test_the_synced_thread_is_stored_with_directions_children_and_search(linked_thread) -> None:
    engine, key, object_id, report = linked_thread

    assert report.status == "ok"
    with engine.connect() as conn:
        thread = store.get_object(conn, object_id)
        entries = store.list_entries(conn, object_id)
        [child] = store.list_children(conn, object_id)
        issue_id = conn.execute(
            sqlalchemy.text("SELECT id FROM issue WHERE key = :key"), {"key": key}
        ).scalar_one()
        found = fts.issue_ids_matching(conn, "torque")
        child_versions = store.list_versions(conn, child.id)

    assert (thread.fetch_status, thread.title) == ("ok", "Inductor lead time")
    assert json.loads(thread.cursor or "")["latest"] == "2026-03-03T10:00:00.000Z"
    assert [(e.external_id, e.direction) for e in entries] == [
        ("<e1@acme.com>", "in"),
        ("<e2@acme.com>", "out"),
        ("<e3@acme.com>", None),
    ]
    assert (child.parent_id, child.object_type, child.external_id) == (
        object_id,
        "attachment",
        "<e1@acme.com>#2",
    )
    assert [v.text for v in child_versions] == ["Torque 12 Nm confirmed"]
    assert found == {issue_id}


def test_a_second_sync_adds_only_the_new_message(linked_thread, tmp_path) -> None:
    engine, key, object_id, first = linked_thread
    assert first.stats["new_entries"] == 3

    home = tmp_path / "sync_home"
    session = thread_session()
    session.records.append(mail("E4", subject="RE: Inductor lead time", sent_on=at(5), body="Ordered."))

    def factory(row: registry.ConnectorRow) -> OutlookConnector:
        return OutlookConnector(registry.to_config(row), session=session)

    report = run_sync(
        engine, SyncScope("source", object_id), config=load_config(home), connector_factory=factory
    )

    assert report.status == "ok"
    assert report.stats["new_entries"] == 1
    assert report.stats["new_versions"] == 0
    assert session.attachment_calls() == []
