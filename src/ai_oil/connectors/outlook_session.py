"""Outlook system access through COM on the classic desktop client (CON-20, CON-22, CON-23, CON-6)."""

import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ai_oil.errors import AiOilError
from ai_oil.sources.model import HealthCheck

_OL_MAIL_ITEM = 43
_OL_FOLDER_SENT_MAIL = 5
_OL_FOLDER_INBOX = 6
_OL_EMBEDDED_ITEM = 5
_OL_TO = 1
_OL_CC = 2
_NO_DATE_YEAR = 4501
_EARLIEST = datetime.min.replace(tzinfo=UTC)

_PROPTAG = "http://schemas.microsoft.com/mapi/proptag/"
_PR_INTERNET_MESSAGE_ID = f"{_PROPTAG}0x1035001F"
_PR_IN_REPLY_TO = f"{_PROPTAG}0x1042001F"
_PR_REFERENCES = f"{_PROPTAG}0x1039001F"
_PR_SMTP_ADDRESS = f"{_PROPTAG}0x39FE001E"
_PR_ATTACH_CONTENT_ID = f"{_PROPTAG}0x3712001F"


class OutlookError(AiOilError):
    """Outlook is not reachable through COM, or an item could not be read."""


@dataclass(frozen=True)
class MailAddress:
    """A mail address; `address` is lower-cased SMTP when resolvable, else the raw address as given."""

    name: str
    address: str


@dataclass(frozen=True)
class AttachmentRecord:
    index: int  # 1-based COM index
    filename: str
    size: int
    content_id: str | None
    is_embedded_item: bool


@dataclass(frozen=True)
class MailRecord:
    """One mail item as plain data; datetimes are timezone-aware UTC."""

    entry_id: str
    store_id: str
    conversation_id: str
    conversation_topic: str
    internet_message_id: str | None
    in_reply_to: str | None
    references: str | None
    subject: str
    sender: MailAddress | None
    to: tuple[MailAddress, ...]
    cc: tuple[MailAddress, ...]
    sent_on: datetime | None
    received_on: datetime | None
    body: str
    html_body: str
    folder_path: str
    attachments: tuple[AttachmentRecord, ...]


class OutlookSession(Protocol):
    """Read-only access to the configured Outlook folders: Inbox, Sent Items and extra folders (CON-27)."""

    def selected_messages(self) -> list[MailRecord]: ...

    def folder_paths(self) -> list[str]:
        """The FolderPath of every searched folder (CON-27)."""
        ...

    def find_by_subject(self, text: str, *, since: datetime | None, limit: int) -> list[MailRecord]:
        """Mails whose subject contains `text` (case-insensitive), newest first."""
        ...

    def find_by_message_id(self, internet_message_id: str) -> MailRecord | None: ...

    def conversation(self, entry_id: str, store_id: str) -> list[MailRecord] | None:
        """All mails of the item's conversation, or None if the anchor item cannot be found (CON-22)."""
        ...

    def conversation_by_topic(self, conversation_id: str, topic: str) -> list[MailRecord]:
        """Fallback (CON-22): items with this ConversationTopic, filtered to this ConversationID."""
        ...

    def find_replies_to(self, internet_message_ids: Sequence[str]) -> list[MailRecord]:
        """Mails whose In-Reply-To is one of the ids (fork detection, CON-25)."""
        ...

    def recent_messages(self, *, since: datetime) -> list[MailRecord]:
        """Mails received (sent, for Sent Items) at or after `since`, newest first (mailbox scan, CON-26)."""
        ...

    def attachment_bytes(self, entry_id: str, store_id: str, index: int) -> bytes: ...

    def check(self) -> list[HealthCheck]: ...


def _each(collection: Any) -> Iterator[Any]:
    """The items of a COM collection (Count and 1-based Item)."""
    for index in range(1, collection.Count + 1):
        yield collection.Item(index)


def _optional(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except Exception:
        return None


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _to_utc(value: Any) -> datetime | None:
    """Outlook time to aware UTC; pywin32 hands out local wall-clock values with an unreliable tzinfo."""
    if value is None:
        return None
    try:
        if value.year >= _NO_DATE_YEAR:
            return None
        wall = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
        return wall.astimezone().astimezone(UTC)
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


def _property(owner: Any, tag: str) -> str | None:
    try:
        value = owner.PropertyAccessor.GetProperty(tag)
    except Exception:
        return None
    return str(value) if value else None


def _is_mail(item: Any) -> bool:
    return _optional(lambda: item.Class) == _OL_MAIL_ITEM


def _sender(item: Any) -> MailAddress | None:
    name = str(item.SenderName or "")
    raw = str(item.SenderEmailAddress or "")
    if not name and not raw:
        return None
    email_type = item.SenderEmailType
    if email_type == "EX":
        smtp = _optional(lambda: item.Sender.GetExchangeUser().PrimarySmtpAddress)
        return MailAddress(name, str(smtp).lower() if smtp else raw)
    if email_type == "SMTP":
        return MailAddress(name, raw.lower())
    return MailAddress(name, raw)


def _recipient(recipient: Any) -> MailAddress | None:
    name = str(recipient.Name or "")
    smtp = None
    try:
        entry = recipient.AddressEntry
        if entry.Type == "EX":
            smtp = entry.GetExchangeUser().PrimarySmtpAddress
    except Exception:
        smtp = None
    if not smtp:
        smtp = _property(recipient, _PR_SMTP_ADDRESS)
    if smtp:
        return MailAddress(name, str(smtp).lower())
    raw = str(recipient.Address or "")
    if not name and not raw:
        return None
    return MailAddress(name, raw)


def _attachment(index: int, attachment: Any) -> AttachmentRecord:
    return AttachmentRecord(
        index=index,
        filename=str(attachment.FileName or ""),
        size=int(attachment.Size or 0),
        content_id=_property(attachment, _PR_ATTACH_CONTENT_ID),
        is_embedded_item=attachment.Type == _OL_EMBEDDED_ITEM,
    )


def _map_item(item: Any) -> MailRecord:
    to: list[MailAddress] = []
    cc: list[MailAddress] = []
    for recipient in _each(item.Recipients):
        target = {_OL_TO: to, _OL_CC: cc}.get(recipient.Type)
        address = _recipient(recipient) if target is not None else None
        if target is not None and address is not None:
            target.append(address)
    parent = item.Parent
    return MailRecord(
        entry_id=str(item.EntryID),
        store_id=str(parent.StoreID),
        conversation_id=str(item.ConversationID or ""),
        conversation_topic=str(item.ConversationTopic or ""),
        internet_message_id=_property(item, _PR_INTERNET_MESSAGE_ID),
        in_reply_to=_property(item, _PR_IN_REPLY_TO),
        references=_property(item, _PR_REFERENCES),
        subject=str(item.Subject or ""),
        sender=_sender(item),
        to=tuple(to),
        cc=tuple(cc),
        sent_on=_to_utc(item.SentOn),
        received_on=_to_utc(item.ReceivedTime),
        body=str(item.Body or ""),
        html_body=str(item.HTMLBody or ""),
        folder_path=str(parent.FolderPath),
        attachments=tuple(_attachment(i, a) for i, a in enumerate(_each(item.Attachments), start=1)),
    )


def _chronological(records: Sequence[MailRecord]) -> list[MailRecord]:
    """Oldest first by sent time, records without one last."""
    return sorted(records, key=lambda r: (r.sent_on is None, r.sent_on or _EARLIEST))


def _newest_first(hits: Sequence[tuple[datetime, MailRecord]]) -> list[MailRecord]:
    return [record for _, record in sorted(hits, key=lambda hit: hit[0], reverse=True)]


def _mail_items(folder: Any) -> Iterator[Any]:
    """Mail items of a folder, newest received first."""
    items = folder.Items
    items.Sort("[ReceivedTime]", True)
    return (item for item in _each(items) if _is_mail(item))


def _restricted(folder: Any, condition: str) -> Iterator[Any]:
    return (item for item in _each(folder.Items.Restrict(condition)) if _is_mail(item))


def _same_name(left: Any, right: str) -> bool:
    return str(left).casefold() == right.casefold()


def _child_folder(parent: Any, name: str) -> Any | None:
    return next((child for child in _each(parent.Folders) if _same_name(child.Name, name)), None)


def _walk(start: Any, names: Sequence[str]) -> Any | None:
    folder = start
    for name in names:
        folder = _child_folder(folder, name)
        if folder is None:
            return None
    return folder


def _resolve_folder(ns: Any, path: str) -> Any | None:
    """A folder by `/`-separated path: from the default mailbox root first, then from a top-level store."""
    names = [part.strip() for part in path.split("/") if part.strip()]
    if not names:
        return None
    try:
        found = _walk(ns.GetDefaultFolder(_OL_FOLDER_INBOX).Parent, names)
        if found is None and len(names) > 1:
            for store in _each(ns.Folders):
                if _same_name(store.Name, names[0]):
                    found = _walk(store, names[1:])
                    if found is not None:
                        break
    except Exception:
        return None
    return found


@dataclass(frozen=True)
class _Source:
    folder: Any
    is_sent: bool


class ComOutlookSession:
    """`OutlookSession` over COM; the connection is made lazily, once per thread (COM apartments)."""

    def __init__(self, *, extra_folders: Sequence[str] = (), include_subfolders: bool = True) -> None:
        self._extra_folders = tuple(extra_folders)
        self._include_subfolders = include_subfolders
        self._local = threading.local()

    def _com(self) -> tuple[Any, Any]:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
        return connection

    def _ns(self) -> Any:
        return self._com()[1]

    def _app(self) -> Any:
        return self._com()[0]

    @staticmethod
    def _connect() -> tuple[Any, Any]:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise OutlookError(
                "Outlook access needs pywin32 on Windows with the classic Outlook desktop client (CON-20)"
            ) from exc
        try:
            pythoncom.CoInitialize()
            app = win32com.client.Dispatch("Outlook.Application")
            return app, app.GetNamespace("MAPI")
        except Exception as exc:
            raise OutlookError(
                f"classic Outlook is not available: {exc} — the new Outlook has no COM interface (CON-20)"
            ) from exc

    def _sources(self, ns: Any) -> list[_Source]:
        inbox = ns.GetDefaultFolder(_OL_FOLDER_INBOX)
        folders = [inbox]
        if self._include_subfolders:
            pending = list(_each(inbox.Folders))[::-1]
            while pending:
                folder = pending.pop()
                folders.append(folder)
                pending.extend(list(_each(folder.Folders))[::-1])
        sources = [_Source(folder, False) for folder in folders]
        sources.append(_Source(ns.GetDefaultFolder(_OL_FOLDER_SENT_MAIL), True))
        for path in self._extra_folders:
            extra = _resolve_folder(ns, path)
            if extra is not None:
                sources.append(_Source(extra, False))
        return sources

    def folder_paths(self) -> list[str]:
        return [str(source.folder.FolderPath) for source in self._sources(self._ns())]

    def selected_messages(self) -> list[MailRecord]:
        explorer = self._app().ActiveExplorer()
        if explorer is None:
            return []
        return [_map_item(item) for item in _each(explorer.Selection) if _is_mail(item)]

    def find_by_subject(self, text: str, *, since: datetime | None, limit: int) -> list[MailRecord]:
        ns = self._ns()
        if limit <= 0:
            return []
        sources = self._sources(ns)
        needle = text.casefold()
        hits: list[tuple[datetime, MailRecord]] = []
        for source in sources:
            found = 0
            for item in _mail_items(source.folder):
                received = _to_utc(item.ReceivedTime)
                if since is not None:
                    if received is None:
                        continue
                    if received < since:
                        break
                if needle in str(item.Subject or "").casefold():
                    hits.append((received or _EARLIEST, _map_item(item)))
                    found += 1
                    if found >= limit:
                        break
        return _newest_first(hits)[:limit]

    def find_by_message_id(self, internet_message_id: str) -> MailRecord | None:
        condition = f"@SQL=\"{_PR_INTERNET_MESSAGE_ID}\" = '{_quote(internet_message_id)}'"
        for source in self._sources(self._ns()):
            for item in _restricted(source.folder, condition):
                return _map_item(item)
        return None

    def conversation(self, entry_id: str, store_id: str) -> list[MailRecord] | None:
        ns = self._ns()
        try:
            anchor = ns.GetItemFromID(entry_id, store_id)
        except Exception:
            return None
        if not _is_mail(anchor):
            return None
        conversation = _optional(anchor.GetConversation)
        if conversation is None:
            return [_map_item(anchor)]
        records: dict[str, MailRecord] = {}
        visited: set[str] = set()
        pending = list(_each(conversation.GetRootItems()))[::-1]
        while pending:
            item = pending.pop()
            item_id = str(item.EntryID)
            if item_id in visited:
                continue
            visited.add(item_id)
            if _is_mail(item) and item_id not in records:
                records[item_id] = _map_item(item)
            pending.extend(list(_each(conversation.GetChildren(item)))[::-1])
        return _chronological(list(records.values()))

    def conversation_by_topic(self, conversation_id: str, topic: str) -> list[MailRecord]:
        condition = f"[ConversationTopic] = '{_quote(topic)}'"
        records = [
            _map_item(item)
            for source in self._sources(self._ns())
            for item in _restricted(source.folder, condition)
            if item.ConversationID == conversation_id
        ]
        return _chronological(records)

    def find_replies_to(self, internet_message_ids: Sequence[str]) -> list[MailRecord]:
        sources = self._sources(self._ns())
        records: dict[str, MailRecord] = {}
        for message_id in internet_message_ids:
            condition = f"@SQL=\"{_PR_IN_REPLY_TO}\" = '{_quote(message_id)}'"
            for source in sources:
                for item in _restricted(source.folder, condition):
                    records.setdefault(str(item.EntryID), _map_item(item))
        return _chronological(list(records.values()))

    def recent_messages(self, *, since: datetime) -> list[MailRecord]:
        hits: list[tuple[datetime, MailRecord]] = []
        for source in self._sources(self._ns()):
            for item in _mail_items(source.folder):
                moment = _to_utc(item.SentOn if source.is_sent else item.ReceivedTime)
                if moment is None:
                    continue
                if moment < since:
                    break
                hits.append((moment, _map_item(item)))
        return _newest_first(hits)

    def attachment_bytes(self, entry_id: str, store_id: str, index: int) -> bytes:
        ns = self._ns()
        handle, name = tempfile.mkstemp(prefix="ai_oil_attachment_")
        os.close(handle)
        path = Path(name)
        try:
            try:
                item = ns.GetItemFromID(entry_id, store_id)
                item.Attachments.Item(index).SaveAsFile(name)
                return path.read_bytes()
            except Exception as exc:
                raise OutlookError(f"cannot read attachment {index} of mail {entry_id}: {exc}") from exc
        finally:
            path.unlink(missing_ok=True)

    def check(self) -> list[HealthCheck]:
        try:
            ns = self._ns()
        except OutlookError as exc:
            return [HealthCheck("com", False, str(exc))]
        checks = [HealthCheck("com", True, "classic Outlook reachable through COM")]
        for name, read in (
            ("mailbox", lambda: ns.GetDefaultFolder(_OL_FOLDER_INBOX).Parent.Name),
            ("inbox", lambda: ns.GetDefaultFolder(_OL_FOLDER_INBOX).Name),
            ("sent", lambda: ns.GetDefaultFolder(_OL_FOLDER_SENT_MAIL).Name),
        ):
            try:
                checks.append(HealthCheck(name, True, str(read())))
            except Exception as exc:
                checks.append(HealthCheck(name, False, f"{type(exc).__name__}: {exc}"))
        for path in self._extra_folders:
            found = _resolve_folder(ns, path) is not None
            checks.append(HealthCheck(f"folder {path}", found, "found" if found else "not found"))
        return checks
