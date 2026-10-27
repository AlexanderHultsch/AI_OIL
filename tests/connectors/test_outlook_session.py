import os
import sys
import threading
import time
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_oil.connectors.outlook_session import (
    AttachmentRecord,
    ComOutlookSession,
    MailAddress,
    OutlookError,
)

ID_TAG = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
REPLY_TAG = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"
REFS_TAG = "http://schemas.microsoft.com/mapi/proptag/0x1039001F"
SMTP_TAG = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
CID_TAG = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
NO_DATE = datetime(4501, 1, 1)
CET = "CET-1CEST,M3.5.0,M10.5.0/3"


def claims_utc(*fields: int) -> datetime:
    """A local wall-clock value whose tzinfo wrongly says UTC, as pywin32 returns it."""
    return datetime(*fields, tzinfo=UTC)


def local_to_utc(*fields: int) -> datetime:
    return datetime(*fields).astimezone().astimezone(UTC)


@pytest.fixture(autouse=True)
def fixed_zone() -> Iterator[None]:
    if not hasattr(time, "tzset"):
        yield
        return
    previous = os.environ.get("TZ")
    os.environ["TZ"] = CET
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class FakeList:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self._items)

    def Item(self, index: int) -> Any:  # noqa: N802
        if not 1 <= index <= len(self._items):
            raise IndexError(index)
        return self._items[index - 1]


class FakeItems(FakeList):
    def __init__(self, items: list[Any], world: "FakeOutlook") -> None:
        super().__init__(list(items))
        self._world = world

    def Item(self, index: int) -> Any:  # noqa: N802
        self._world.item_reads += 1
        return super().Item(index)

    def Sort(self, prop: str, descending: bool = False) -> None:  # noqa: N802
        assert prop == "[ReceivedTime]"
        self._items.sort(
            key=lambda i: getattr(i, "ReceivedTime", NO_DATE).timetuple()[:6], reverse=descending
        )

    def Restrict(self, condition: str) -> "FakeItems":  # noqa: N802
        self._world.restrictions.append(condition)
        if condition.startswith("[ConversationTopic] = '") and condition.endswith("'"):
            wanted = condition[len("[ConversationTopic] = '") : -1].replace("''", "'")
            return FakeItems(
                [i for i in self._items if getattr(i, "ConversationTopic", None) == wanted], self._world
            )
        prefix = '@SQL="'
        if condition.startswith(prefix):
            tag, _, rest = condition[len(prefix) :].partition("\" = '")
            wanted = rest[:-1].replace("''", "'")
            return FakeItems([i for i in self._items if _prop_equals(i, tag, wanted)], self._world)
        raise ValueError(f"the fake does not understand {condition!r}")


def _prop_equals(item: Any, tag: str, wanted: str) -> bool:
    try:
        return item.PropertyAccessor.GetProperty(tag) == wanted
    except Exception:
        return False


class FakePropertyAccessor:
    def __init__(self, props: dict[str, Any]) -> None:
        self.props = props

    def GetProperty(self, tag: str) -> Any:  # noqa: N802
        value = self.props[tag]
        if isinstance(value, Exception):
            raise value
        return value


class FakeExchangeUser:
    def __init__(self, smtp: str) -> None:
        self.PrimarySmtpAddress = smtp


class FakeAddressEntry:
    def __init__(self, type_: str = "SMTP", exchange_smtp: str | Exception | None = None) -> None:
        self.Type = type_
        self._exchange_smtp = exchange_smtp

    def GetExchangeUser(self) -> FakeExchangeUser | None:  # noqa: N802
        if isinstance(self._exchange_smtp, Exception):
            raise self._exchange_smtp
        return None if self._exchange_smtp is None else FakeExchangeUser(self._exchange_smtp)


class FakeRecipient:
    def __init__(
        self,
        name: str,
        address: str,
        type_: int = 1,
        entry: FakeAddressEntry | None = None,
        props: dict[str, Any] | None = None,
    ) -> None:
        self.Name = name
        self.Address = address
        self.Type = type_
        self.AddressEntry = entry or FakeAddressEntry()
        self.PropertyAccessor = FakePropertyAccessor(props or {})


class FakeAttachment:
    def __init__(
        self,
        filename: str,
        data: bytes = b"",
        size: int | None = None,
        type_: int = 1,
        props: dict[str, Any] | None = None,
        save_error: Exception | None = None,
        world: "FakeOutlook | None" = None,
    ) -> None:
        self.FileName = filename
        self.Size = len(data) if size is None else size
        self.Type = type_
        self.PropertyAccessor = FakePropertyAccessor(props or {})
        self._data = data
        self._save_error = save_error
        self._world = world

    def SaveAsFile(self, path: str) -> None:  # noqa: N802
        if self._world is not None:
            self._world.saved_paths.append(path)
        if self._save_error is not None:
            raise self._save_error
        Path(path).write_bytes(self._data)


class FakeSender:
    def __init__(self, smtp: str | Exception | None) -> None:
        self._smtp = smtp

    def GetExchangeUser(self) -> FakeExchangeUser | None:  # noqa: N802
        if isinstance(self._smtp, Exception):
            raise self._smtp
        return None if self._smtp is None else FakeExchangeUser(self._smtp)


class FakeMail:
    Class = 43

    def __init__(self, entry_id: str, **fields: Any) -> None:
        self.EntryID = entry_id
        self.ConversationID = "CONV1"
        self.ConversationTopic = "Inductor lead time"
        self.Subject = "Inductor lead time"
        self.SenderEmailType = "SMTP"
        self.SenderName = "Jane Doe"
        self.SenderEmailAddress = "Jane@Acme.com"
        self.Sender: FakeSender | None = None
        self.Recipients = FakeList([])
        self.Body = "body"
        self.HTMLBody = "<p>body</p>"
        self.SentOn = claims_utc(2026, 3, 1, 10, 0, 0)
        self.ReceivedTime = claims_utc(2026, 3, 1, 10, 0, 5)
        self.Attachments = FakeList([])
        self.PropertyAccessor = FakePropertyAccessor({ID_TAG: f"<{entry_id}@example.com>"})
        self.conversation: FakeConversation | None = None
        self.Parent: FakeFolder = None  # type: ignore[assignment]
        for key, value in fields.items():
            setattr(self, key, value)

    def GetConversation(self) -> "FakeConversation | None":  # noqa: N802
        return self.conversation


class FakeOther:
    """A non-mail item (calendar entry): it exposes nothing but its class and a subject."""

    Class = 26

    def __init__(self, entry_id: str, subject: str = "Inductor lead time") -> None:
        self.EntryID = entry_id
        self.Subject = subject
        self.ConversationTopic = subject
        self.ReceivedTime = claims_utc(2026, 3, 5, 9, 0, 0)
        self.Parent: FakeFolder = None  # type: ignore[assignment]


class FakeConversation:
    def __init__(self, roots: list[Any], children: dict[str, list[Any]]) -> None:
        self._roots = roots
        self._children = children

    def GetRootItems(self) -> FakeList:  # noqa: N802
        return FakeList(self._roots)

    def GetChildren(self, item: Any) -> FakeList:  # noqa: N802
        return FakeList(self._children.get(item.EntryID, []))


class FakeFolder:
    def __init__(self, world: "FakeOutlook", name: str, path: str, store_id: str) -> None:
        self.Name = name
        self.FolderPath = path
        self.StoreID = store_id
        self.Parent: FakeFolder | None = None
        self._world = world
        self._children: list[FakeFolder] = []
        self._items: list[Any] = []

    @property
    def Folders(self) -> FakeList:  # noqa: N802
        return FakeList(self._children)

    @property
    def Items(self) -> FakeItems:  # noqa: N802
        return FakeItems(self._items, self._world)

    def add_folder(self, name: str) -> "FakeFolder":
        child = FakeFolder(self._world, name, f"{self.FolderPath}\\{name}", self.StoreID)
        child.Parent = self
        self._children.append(child)
        return child

    def add(self, item: Any) -> Any:
        item.Parent = self
        self._items.append(item)
        self._world.items[item.EntryID] = item
        return item


class FakeExplorer:
    def __init__(self, selection: list[Any]) -> None:
        self.Selection = FakeList(selection)


class FakeNamespace:
    def __init__(self, world: "FakeOutlook") -> None:
        self._world = world

    def GetDefaultFolder(self, which: int) -> FakeFolder:  # noqa: N802
        return {6: self._world.inbox, 5: self._world.sent}[which]

    @property
    def Folders(self) -> FakeList:  # noqa: N802
        return FakeList([self._world.root, self._world.other_store])

    def GetItemFromID(self, entry_id: str, store_id: str) -> Any:  # noqa: N802
        item = self._world.items.get(entry_id)
        if item is None or item.Parent.StoreID != store_id:
            raise RuntimeError("the item cannot be found")
        return item


class FakeApp:
    def __init__(self, world: "FakeOutlook") -> None:
        self._world = world

    def GetNamespace(self, name: str) -> FakeNamespace:  # noqa: N802
        assert name == "MAPI"
        return FakeNamespace(self._world)

    def ActiveExplorer(self) -> FakeExplorer | None:  # noqa: N802
        return self._world.explorer


class FakeOutlook:
    def __init__(self) -> None:
        self.restrictions: list[str] = []
        self.item_reads = 0
        self.saved_paths: list[str] = []
        self.items: dict[str, Any] = {}
        self.explorer: FakeExplorer | None = None
        self.root = FakeFolder(self, "alex@example.com", "\\\\alex@example.com", "S1")
        self.inbox = self.root.add_folder("Inbox")
        self.sent = self.root.add_folder("Sent Items")
        self.projects = self.inbox.add_folder("Projects")
        self.deep = self.projects.add_folder("Deep")
        self.archive_2026 = self.root.add_folder("Archive").add_folder("2026")
        self.other_store = FakeFolder(self, "Shared", "\\\\Shared", "S2")
        self.shared_2026 = self.other_store.add_folder("2026")
        self.app = FakeApp(self)

    def link(self, roots: list[FakeMail], children: dict[str, list[FakeMail]], members: list[Any]) -> None:
        conversation = FakeConversation(roots, children)
        for member in members:
            member.conversation = conversation


@pytest.fixture
def world() -> FakeOutlook:
    return FakeOutlook()


class ComFakes:
    def __init__(self) -> None:
        self.initialised_in: list[int] = []
        self.dispatched: list[str] = []
        self.dispatch_error: Exception | None = None
        self.app: Any = None


@pytest.fixture
def com(monkeypatch: pytest.MonkeyPatch, world: FakeOutlook) -> ComFakes:
    fakes = ComFakes()
    fakes.app = world.app
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: fakes.initialised_in.append(threading.get_ident())  # type: ignore[attr-defined]
    win32com = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")

    def dispatch(name: str) -> Any:
        fakes.dispatched.append(name)
        if fakes.dispatch_error is not None:
            raise fakes.dispatch_error
        return fakes.app

    client.Dispatch = dispatch  # type: ignore[attr-defined]
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return fakes


@pytest.fixture
def session(com: ComFakes) -> ComOutlookSession:
    return ComOutlookSession()


def subjects(records: list[Any]) -> list[str]:
    return [r.entry_id for r in records]


def test_mapping_of_every_field(world: FakeOutlook, session: ComOutlookSession) -> None:
    mail = FakeMail(
        "E1",
        Subject="RE: Inductor lead time",
        Body="plain",
        HTMLBody="<p>html</p>",
        SentOn=claims_utc(2026, 3, 1, 10, 0, 0),
        ReceivedTime=claims_utc(2026, 3, 1, 10, 0, 5),
        Recipients=FakeList(
            [FakeRecipient("Alex", "alex@example.com", 1), FakeRecipient("Bob", "bob@acme.com", 2)]
        ),
        Attachments=FakeList(
            [
                FakeAttachment("spec.pdf", b"%PDF", props={CID_TAG: ""}),
                FakeAttachment("logo.png", b"x" * 12, props={CID_TAG: "logo@cid"}),
                FakeAttachment("forwarded.msg", b"", size=900, type_=5),
            ]
        ),
        PropertyAccessor=FakePropertyAccessor(
            {ID_TAG: "<m1@acme.com>", REPLY_TAG: "<m0@acme.com>", REFS_TAG: "<m0@acme.com> <m00@acme.com>"}
        ),
    )
    world.inbox.add(mail)
    world.explorer = FakeExplorer([mail])

    [record] = session.selected_messages()

    assert record.entry_id == "E1"
    assert record.store_id == "S1"
    assert record.conversation_id == "CONV1"
    assert record.conversation_topic == "Inductor lead time"
    assert record.internet_message_id == "<m1@acme.com>"
    assert record.in_reply_to == "<m0@acme.com>"
    assert record.references == "<m0@acme.com> <m00@acme.com>"
    assert record.subject == "RE: Inductor lead time"
    assert record.sender == MailAddress("Jane Doe", "jane@acme.com")
    assert record.to == (MailAddress("Alex", "alex@example.com"),)
    assert record.cc == (MailAddress("Bob", "bob@acme.com"),)
    assert record.sent_on == local_to_utc(2026, 3, 1, 10, 0, 0)
    assert record.received_on == local_to_utc(2026, 3, 1, 10, 0, 5)
    assert record.body == "plain"
    assert record.html_body == "<p>html</p>"
    assert record.folder_path == "\\\\alex@example.com\\Inbox"
    assert record.attachments == (
        AttachmentRecord(1, "spec.pdf", 4, None, False),
        AttachmentRecord(2, "logo.png", 12, "logo@cid", False),
        AttachmentRecord(3, "forwarded.msg", 900, None, True),
    )


def test_exchange_sender_is_resolved_to_lower_case_smtp(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(
        FakeMail(
            "E1",
            SenderEmailType="EX",
            SenderEmailAddress="/O=VISTEON/OU=EXCHANGE/CN=JDOE",
            Sender=FakeSender("Jane.Doe@Visteon.com"),
        )
    )

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.sender == MailAddress("Jane Doe", "jane.doe@visteon.com")


@pytest.mark.parametrize("sender", [None, FakeSender(RuntimeError("no GAL entry")), FakeSender(None)])
def test_exchange_sender_falls_back_to_the_raw_address(
    world: FakeOutlook, session: ComOutlookSession, sender: FakeSender | None
) -> None:
    dn = "/O=VISTEON/OU=EXCHANGE/CN=JDOE"
    world.inbox.add(FakeMail("E1", SenderEmailType="EX", SenderEmailAddress=dn, Sender=sender))

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.sender == MailAddress("Jane Doe", dn)


def test_sender_of_other_address_types_is_kept_as_given(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(FakeMail("E1", SenderEmailType="FAX", SenderEmailAddress="Fax-123"))
    world.inbox.add(
        FakeMail("E2", SenderName="", SenderEmailAddress="", ReceivedTime=claims_utc(2026, 2, 1, 8, 0, 0))
    )

    first, second = session.find_by_subject("inductor", since=None, limit=5)

    assert first.sender == MailAddress("Jane Doe", "Fax-123")
    assert second.sender is None


def test_recipients_by_type_and_smtp_resolution(world: FakeOutlook, session: ComOutlookSession) -> None:
    recipients = FakeList(
        [
            FakeRecipient("Exchange Ed", "/O=X/CN=ED", 1, FakeAddressEntry("EX", "Ed.Exchange@Visteon.com")),
            FakeRecipient("Broken Bea", "/O=X/CN=BEA", 2, FakeAddressEntry("EX", RuntimeError("gone"))),
            FakeRecipient("Smtp Sam", "unused@x.com", 2, props={SMTP_TAG: "Sam@Partner.com"}),
            FakeRecipient("Hidden Hal", "hal@x.com", 3, props={SMTP_TAG: "hal@x.com"}),
            FakeRecipient("Raw Rae", "Rae@Raw.Example", 1, props={SMTP_TAG: RuntimeError("no property")}),
            FakeRecipient("Resource", "room@x.com", 4),
        ]
    )
    world.inbox.add(FakeMail("E1", Recipients=recipients))

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.to == (
        MailAddress("Exchange Ed", "ed.exchange@visteon.com"),
        MailAddress("Raw Rae", "Rae@Raw.Example"),
    )
    assert record.cc == (
        MailAddress("Broken Bea", "/O=X/CN=BEA"),
        MailAddress("Smtp Sam", "sam@partner.com"),
    )


def test_property_accessor_errors_become_none(world: FakeOutlook, session: ComOutlookSession) -> None:
    mail = FakeMail(
        "E1",
        PropertyAccessor=FakePropertyAccessor({ID_TAG: RuntimeError("boom"), REPLY_TAG: ""}),
        Attachments=FakeList([FakeAttachment("a.txt", b"a", props={CID_TAG: RuntimeError("boom")})]),
    )
    world.inbox.add(mail)

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.internet_message_id is None
    assert record.in_reply_to is None
    assert record.references is None
    assert record.attachments[0].content_id is None


def test_datetimes_are_treated_as_local_wall_clock_even_when_tzinfo_claims_utc(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(
        FakeMail(
            "E1",
            SentOn=claims_utc(2026, 7, 1, 10, 0, 0),
            ReceivedTime=claims_utc(2026, 3, 1, 10, 0, 0),
        )
    )

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.sent_on == local_to_utc(2026, 7, 1, 10, 0, 0)
    assert record.received_on == local_to_utc(2026, 3, 1, 10, 0, 0)
    assert record.sent_on is not None and record.sent_on.utcoffset() == UTC.utcoffset(None)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset to pin the local zone")
def test_datetime_conversion_in_a_fixed_zone(world: FakeOutlook, session: ComOutlookSession) -> None:
    world.inbox.add(
        FakeMail(
            "E1",
            SentOn=claims_utc(2026, 7, 1, 10, 0, 0),
            ReceivedTime=claims_utc(2026, 3, 1, 10, 0, 0),
        )
    )

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.received_on == datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
    assert record.sent_on == datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)


def test_the_no_date_sentinel_becomes_none(world: FakeOutlook, session: ComOutlookSession) -> None:
    world.inbox.add(FakeMail("E1", SentOn=NO_DATE, ReceivedTime=claims_utc(4501, 1, 1, 0, 0, 0)))

    [record] = session.find_by_subject("inductor", since=None, limit=5)

    assert record.sent_on is None
    assert record.received_on is None


def test_find_by_subject_walks_all_configured_folders(world: FakeOutlook, com: ComFakes) -> None:
    world.inbox.add(FakeMail("INBOX", ReceivedTime=claims_utc(2026, 3, 1, 10, 0, 0)))
    world.projects.add(FakeMail("SUB", ReceivedTime=claims_utc(2026, 3, 2, 10, 0, 0)))
    world.deep.add(FakeMail("DEEP", ReceivedTime=claims_utc(2026, 3, 3, 10, 0, 0)))
    world.sent.add(FakeMail("SENT", ReceivedTime=claims_utc(2026, 3, 4, 10, 0, 0)))
    world.archive_2026.add(FakeMail("ARCH", ReceivedTime=claims_utc(2026, 3, 5, 10, 0, 0)))
    world.shared_2026.add(FakeMail("SHARED", ReceivedTime=claims_utc(2026, 3, 6, 10, 0, 0)))
    world.root.add(FakeMail("ROOT", ReceivedTime=claims_utc(2026, 3, 7, 10, 0, 0)))

    default = ComOutlookSession()
    flat = ComOutlookSession(include_subfolders=False)
    extra = ComOutlookSession(
        extra_folders=["Archive/2026", "shared/2026", "Archive/Missing", "Nowhere/2026"]
    )

    assert subjects(default.find_by_subject("inductor", since=None, limit=50)) == [
        "SENT",
        "DEEP",
        "SUB",
        "INBOX",
    ]
    assert subjects(flat.find_by_subject("inductor", since=None, limit=50)) == ["SENT", "INBOX"]
    assert subjects(extra.find_by_subject("inductor", since=None, limit=50)) == [
        "SHARED",
        "ARCH",
        "SENT",
        "DEEP",
        "SUB",
        "INBOX",
    ]


def test_check_reports_folders_and_skips_unresolvable_ones(world: FakeOutlook, com: ComFakes) -> None:
    session = ComOutlookSession(extra_folders=["Archive/2026", "Archive/Missing", "Shared/2026"])

    checks = session.check()

    assert [(c.name, c.ok, c.detail) for c in checks] == [
        ("com", True, "classic Outlook reachable through COM"),
        ("mailbox", True, "alex@example.com"),
        ("inbox", True, "Inbox"),
        ("sent", True, "Sent Items"),
        ("folder Archive/2026", True, "found"),
        ("folder Archive/Missing", False, "not found"),
        ("folder Shared/2026", True, "found"),
    ]


def test_check_reports_a_failing_folder_lookup(
    world: FakeOutlook, com: ComFakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: FakeNamespace, which: int) -> FakeFolder:
        if which == 5:
            raise RuntimeError("no sent folder")
        return world.inbox

    monkeypatch.setattr(FakeNamespace, "GetDefaultFolder", broken)

    checks = {c.name: c for c in ComOutlookSession().check()}

    assert checks["com"].ok and checks["inbox"].ok
    assert not checks["sent"].ok
    assert "no sent folder" in checks["sent"].detail


def test_check_reports_a_missing_outlook(com: ComFakes) -> None:
    com.dispatch_error = RuntimeError("Invalid class string")

    [check] = ComOutlookSession().check()

    assert check.name == "com" and not check.ok
    assert "new Outlook" in check.detail


def test_non_mail_items_are_skipped_everywhere(world: FakeOutlook, session: ComOutlookSession) -> None:
    mail = world.inbox.add(
        FakeMail("MAIL", PropertyAccessor=FakePropertyAccessor({ID_TAG: "<x@y>", REPLY_TAG: "<p@y>"}))
    )
    meeting = world.inbox.add(FakeOther("MEETING"))
    meeting.PropertyAccessor = FakePropertyAccessor({ID_TAG: "<x@y>", REPLY_TAG: "<p@y>"})
    world.explorer = FakeExplorer([meeting, mail])
    world.link([mail], {"MAIL": [meeting]}, [mail])

    assert subjects(session.find_by_subject("inductor", since=None, limit=5)) == ["MAIL"]
    assert subjects(session.recent_messages(since=claims_utc(2026, 1, 1, 0, 0, 0))) == ["MAIL"]
    assert subjects(session.conversation_by_topic("CONV1", "Inductor lead time")) == ["MAIL"]
    assert session.find_by_message_id("<x@y>").entry_id == "MAIL"  # type: ignore[union-attr]
    assert subjects(session.find_replies_to(["<p@y>"])) == ["MAIL"]
    assert subjects(session.selected_messages()) == ["MAIL"]
    assert subjects(session.conversation("MAIL", "S1") or []) == ["MAIL"]


def test_a_non_mail_anchor_is_not_a_conversation(world: FakeOutlook, session: ComOutlookSession) -> None:
    world.inbox.add(FakeOther("MEETING"))

    assert session.conversation("MEETING", "S1") is None


def test_conversation_spans_inbox_and_sent_items_deduplicated_and_ordered(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    first = world.inbox.add(FakeMail("A", SentOn=claims_utc(2026, 3, 1, 9, 0, 0)))
    reply = world.sent.add(FakeMail("B", SentOn=claims_utc(2026, 3, 1, 11, 0, 0)))
    answer = world.projects.add(FakeMail("C", SentOn=claims_utc(2026, 3, 2, 8, 0, 0)))
    undated = world.inbox.add(FakeMail("D", SentOn=NO_DATE))
    world.link(
        [first],
        {"A": [answer, reply], "B": [answer, undated], "C": [reply]},
        [first, reply, answer, undated],
    )

    records = session.conversation("B", "S1")

    assert records is not None
    assert subjects(records) == ["A", "B", "C", "D"]
    assert [r.folder_path.rsplit("\\", 1)[1] for r in records] == ["Inbox", "Sent Items", "Projects", "Inbox"]


def test_conversation_with_a_missing_anchor_is_none(session: ComOutlookSession) -> None:
    assert session.conversation("GONE", "S1") is None


def test_conversation_with_the_anchor_in_another_store_is_none(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(FakeMail("A"))

    assert session.conversation("A", "S2") is None


def test_conversation_without_get_conversation_is_the_single_item(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(FakeMail("A"))

    records = session.conversation("A", "S1")

    assert records is not None and subjects(records) == ["A"]


def test_conversation_by_topic_filters_on_conversation_id(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(FakeMail("A", SentOn=claims_utc(2026, 3, 2, 9, 0, 0)))
    world.sent.add(FakeMail("B", SentOn=claims_utc(2026, 3, 1, 9, 0, 0)))
    world.inbox.add(FakeMail("OTHER_CONV", ConversationID="CONV2"))
    world.inbox.add(FakeMail("OTHER_TOPIC", ConversationTopic="Something else"))

    records = session.conversation_by_topic("CONV1", "Inductor lead time")

    assert subjects(records) == ["B", "A"]
    assert set(world.restrictions) == {"[ConversationTopic] = 'Inductor lead time'"}


def test_conversation_by_topic_escapes_apostrophes(world: FakeOutlook, session: ComOutlookSession) -> None:
    topic = "Jane's 'urgent' request"
    world.inbox.add(FakeMail("A", ConversationTopic=topic))

    records = session.conversation_by_topic("CONV1", topic)

    assert subjects(records) == ["A"]
    assert world.restrictions[0] == "[ConversationTopic] = 'Jane''s ''urgent'' request'"


def test_find_by_message_id(world: FakeOutlook, session: ComOutlookSession) -> None:
    world.inbox.add(FakeMail("A"))
    world.sent.add(FakeMail("B", PropertyAccessor=FakePropertyAccessor({ID_TAG: "<it's@acme.com>"})))

    found = session.find_by_message_id("<it's@acme.com>")

    assert found is not None and found.entry_id == "B"
    assert (
        "@SQL=\"http://schemas.microsoft.com/mapi/proptag/0x1035001F\" = '<it''s@acme.com>'"
        in world.restrictions
    )
    assert session.find_by_message_id("<A@example.com>") is not None
    assert session.find_by_message_id("<unknown@acme.com>") is None


def test_find_replies_to(world: FakeOutlook, session: ComOutlookSession) -> None:
    world.inbox.add(
        FakeMail(
            "R1",
            SentOn=claims_utc(2026, 3, 3, 9, 0, 0),
            PropertyAccessor=FakePropertyAccessor({ID_TAG: "<r1@x>", REPLY_TAG: "<m1@x>"}),
        )
    )
    world.sent.add(
        FakeMail(
            "R2",
            SentOn=claims_utc(2026, 3, 2, 9, 0, 0),
            PropertyAccessor=FakePropertyAccessor({ID_TAG: "<r2@x>", REPLY_TAG: "<m2@x>"}),
        )
    )
    world.inbox.add(FakeMail("NOREPLY"))

    replies = session.find_replies_to(["<m1@x>", "<m2@x>", "<m1@x>", "<none@x>"])

    assert subjects(replies) == ["R2", "R1"]
    assert "@SQL=\"http://schemas.microsoft.com/mapi/proptag/0x1042001F\" = '<m2@x>'" in world.restrictions
    assert session.find_replies_to([]) == []


def test_find_by_subject_since_limit_and_case(world: FakeOutlook, session: ComOutlookSession) -> None:
    for day in range(1, 6):
        world.inbox.add(
            FakeMail(
                f"I{day}",
                Subject=f"Re: INDUCTOR lead time {day}",
                ReceivedTime=claims_utc(2026, 3, day, 8, 0, 0),
            )
        )
    world.sent.add(FakeMail("S1", Subject="inductor reminder", ReceivedTime=claims_utc(2026, 3, 3, 12, 0, 0)))
    world.inbox.add(FakeMail("X", Subject="Capacitor", ReceivedTime=claims_utc(2026, 3, 4, 12, 0, 0)))

    everything = session.find_by_subject("Inductor", since=None, limit=50)
    recent = session.find_by_subject("inductor", since=local_to_utc(2026, 3, 3, 12, 0, 0), limit=50)
    limited = session.find_by_subject("inductor", since=None, limit=3)

    assert subjects(everything) == ["I5", "I4", "S1", "I3", "I2", "I1"]
    assert subjects(recent) == ["I5", "I4", "S1"]
    assert subjects(limited) == ["I5", "I4", "S1"]
    assert session.find_by_subject("inductor", since=None, limit=0) == []


def test_recent_messages_stop_at_since_and_use_sent_on_for_sent_items(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    for day in range(1, 6):
        world.inbox.add(FakeMail(f"I{day}", ReceivedTime=claims_utc(2026, 3, day, 8, 0, 0)))
    world.sent.add(
        FakeMail(
            "S_NEW", ReceivedTime=claims_utc(2026, 3, 5, 9, 0, 0), SentOn=claims_utc(2026, 3, 5, 9, 0, 0)
        )
    )
    world.sent.add(
        FakeMail(
            "S_OLD", ReceivedTime=claims_utc(2026, 3, 4, 9, 0, 0), SentOn=claims_utc(2026, 2, 1, 9, 0, 0)
        )
    )
    world.inbox.add(FakeMail("NODATE", ReceivedTime=NO_DATE))
    since = local_to_utc(2026, 3, 4, 8, 0, 0)

    records = session.recent_messages(since=since)

    assert subjects(records) == ["S_NEW", "I5", "I4"]


def test_recent_messages_do_not_read_beyond_since(world: FakeOutlook, session: ComOutlookSession) -> None:
    for day in range(1, 11):
        world.inbox.add(FakeMail(f"I{day}", ReceivedTime=claims_utc(2026, 3, day, 8, 0, 0)))
    start = world.item_reads

    session.recent_messages(since=local_to_utc(2026, 3, 9, 0, 0, 0))

    assert world.item_reads - start <= 3 + 1  # I10, I9 and the first stale item; the sent folder is empty


def test_attachment_bytes_reads_and_deletes_the_temp_file(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(
        FakeMail(
            "A",
            Attachments=FakeList(
                [
                    FakeAttachment("a.txt", b"first", world=world),
                    FakeAttachment("b.bin", b"\x00\x01\x02", world=world),
                ]
            ),
        )
    )

    data = session.attachment_bytes("A", "S1", 2)

    assert data == b"\x00\x01\x02"
    [path] = world.saved_paths
    assert not Path(path).exists()


def test_attachment_bytes_deletes_the_temp_file_on_failure(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.inbox.add(
        FakeMail(
            "A",
            Attachments=FakeList(
                [FakeAttachment("a.txt", save_error=RuntimeError("disk full"), world=world)]
            ),
        )
    )

    with pytest.raises(OutlookError, match="disk full"):
        session.attachment_bytes("A", "S1", 1)
    with pytest.raises(OutlookError):
        session.attachment_bytes("A", "S1", 7)
    with pytest.raises(OutlookError):
        session.attachment_bytes("GONE", "S1", 1)

    assert world.saved_paths
    assert not any(Path(p).exists() for p in world.saved_paths)


def test_selected_messages_with_mixed_classes(world: FakeOutlook, session: ComOutlookSession) -> None:
    one = world.inbox.add(FakeMail("ONE"))
    meeting = world.inbox.add(FakeOther("MEETING"))
    two = world.sent.add(FakeMail("TWO"))
    world.explorer = FakeExplorer([one, meeting, two])

    assert subjects(session.selected_messages()) == ["ONE", "TWO"]


def test_selected_messages_without_explorer_or_selection(
    world: FakeOutlook, session: ComOutlookSession
) -> None:
    world.explorer = None
    assert session.selected_messages() == []

    world.explorer = FakeExplorer([])
    assert session.selected_messages() == []


def test_the_constructor_does_no_com_work(com: ComFakes) -> None:
    ComOutlookSession(extra_folders=["Archive/2026"])

    assert com.dispatched == [] and com.initialised_in == []


def test_com_is_initialised_once_per_thread(world: FakeOutlook, com: ComFakes) -> None:
    world.inbox.add(FakeMail("A"))
    session = ComOutlookSession()
    errors: list[BaseException] = []

    session.find_by_subject("inductor", since=None, limit=1)
    session.recent_messages(since=claims_utc(2026, 1, 1, 0, 0, 0))
    session.check()

    def worker() -> None:
        try:
            session.find_by_subject("inductor", since=None, limit=1)
            session.check()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == []
    assert com.initialised_in == [threading.get_ident(), thread.ident]
    assert com.dispatched == ["Outlook.Application", "Outlook.Application"]


def test_missing_pywin32_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    monkeypatch.setitem(sys.modules, "win32com", None)
    monkeypatch.setitem(sys.modules, "win32com.client", None)
    session = ComOutlookSession()

    for call in (
        lambda: session.selected_messages(),
        lambda: session.find_by_subject("x", since=None, limit=1),
        lambda: session.attachment_bytes("A", "S1", 1),
    ):
        with pytest.raises(OutlookError, match="pywin32"):
            call()


def test_failing_dispatch_names_the_new_outlook(com: ComFakes) -> None:
    com.dispatch_error = OSError("Invalid class string")

    with pytest.raises(
        OutlookError, match="classic Outlook is not available: Invalid class string"
    ) as raised:
        ComOutlookSession().find_by_message_id("<a@b>")

    assert "new Outlook has no COM interface" in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_a_failed_connection_is_retried(com: ComFakes, world: FakeOutlook) -> None:
    session = ComOutlookSession()
    com.dispatch_error = OSError("not running")
    with pytest.raises(OutlookError):
        session.selected_messages()

    com.dispatch_error = None

    assert session.selected_messages() == []
    assert com.dispatched == ["Outlook.Application", "Outlook.Application"]
