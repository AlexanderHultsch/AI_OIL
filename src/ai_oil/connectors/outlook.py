"""Outlook connector: email conversations and their attachments (CON-20 .. CON-27)."""

import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import ClassVar

from ai_oil.connectors import email_text
from ai_oil.connectors.outlook_session import (
    ComOutlookSession,
    MailAddress,
    MailRecord,
    OutlookError,
    OutlookSession,
)
from ai_oil.errors import ValidationError
from ai_oil.sources import extract
from ai_oil.sources.model import (
    CandidateQuery,
    ChildData,
    ConnectorConfig,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    IdentityRef,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)
from ai_oil.timeutil import parse_iso, utcnow_iso

SELECTED_REFERENCE = "outlook:selected"

_MESSAGE_ID = re.compile(r"^<[^<>\s]+@[^<>\s]+>$")
_INLINE_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
_EARLIEST = datetime.min.replace(tzinfo=UTC)
_SEARCH_OVERFETCH = 5


@dataclass(frozen=True)
class ConversationCandidate:
    """A conversation found by subject search or mailbox scan (CON-21, CON-26)."""

    ref: SourceRef
    subject: str
    latest: datetime | None
    sender: str
    message_count: int


def _moment(record: MailRecord) -> datetime | None:
    return record.sent_on or record.received_on


def _iso(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _ref_for(record: MailRecord) -> SourceRef:
    return SourceRef(
        "email_thread",
        record.conversation_id,
        display_ref=email_text.subject_root(record.subject),
        locator=f"outlook:{record.store_id}:{record.entry_id}",
        extra={"topic": record.conversation_topic},
    )


def _candidates(records: Sequence[MailRecord]) -> list[ConversationCandidate]:
    """One candidate per conversation, newest conversation first."""
    groups: dict[str, list[MailRecord]] = {}
    for record in records:
        groups.setdefault(record.conversation_id, []).append(record)
    found = []
    for group in groups.values():
        newest = max(group, key=lambda record: _moment(record) or _EARLIEST)
        sender = newest.sender
        found.append(
            ConversationCandidate(
                ref=_ref_for(newest),
                subject=email_text.subject_root(newest.subject),
                latest=_moment(newest),
                sender=(sender.name or sender.address) if sender is not None else "-",
                message_count=len(group),
            )
        )
    return sorted(found, key=lambda candidate: candidate.latest or _EARLIEST, reverse=True)


def _identity(address: MailAddress) -> IdentityRef | None:
    if "@" not in address.address:
        return None
    return IdentityRef("email", address.address.lower(), address.name or None)


def _list_setting(settings: Mapping[str, object], key: str) -> list[str]:
    value = settings.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"Outlook setting {key} must be a list of text values")
    return value


def _bool_setting(settings: Mapping[str, object], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(f"Outlook setting {key} must be true or false")
    return value


def _int_setting(settings: Mapping[str, object], key: str, default: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"Outlook setting {key} must be a non-negative whole number")
    return value


@dataclass(frozen=True)
class _Anchor:
    entry_id: str | None
    store_id: str | None
    topic: str
    latest: str | None


class OutlookConnector:
    """Conversations of the classic Outlook client, read through an injectable session (CON-6)."""

    type: ClassVar[str] = "outlook"
    object_types: ClassVar[frozenset[str]] = frozenset({"email_thread", "attachment"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 1

    def __init__(self, config: ConnectorConfig, *, session: OutlookSession | None = None) -> None:
        self.config = config
        settings = config.settings
        self._extra_folders = _list_setting(settings, "extra_folders")
        self._include_subfolders = _bool_setting(settings, "include_subfolders", True)
        self._scan_days = _int_setting(settings, "scan_days", 14)
        self._inline_image_max_bytes = _int_setting(settings, "inline_image_max_bytes", 20480)
        self._signature_markers = _list_setting(settings, "signature_markers")
        self._max_text_chars = _int_setting(settings, "max_text_chars", 2_000_000)
        self._session: OutlookSession = (
            session
            if session is not None
            else ComOutlookSession(
                extra_folders=self._extra_folders, include_subfolders=self._include_subfolders
            )
        )

    def check(self) -> HealthReport:
        try:
            return HealthReport(tuple(self._session.check()))
        except OutlookError as exc:
            return HealthReport((HealthCheck("com", False, str(exc)),))

    def parse_reference(self, text: str) -> SourceRef | None:
        candidate = text.strip()
        if candidate == SELECTED_REFERENCE:
            return self._selected_reference()
        if _MESSAGE_ID.match(candidate) is None:
            return None
        record = self._session.find_by_message_id(candidate)
        if record is None:
            raise ValidationError(f"no email with message id {candidate} in the configured Outlook folders")
        return _ref_for(record)

    def _selected_reference(self) -> SourceRef:
        selected = self._session.selected_messages()
        if not selected:
            raise ValidationError("no email is selected in Outlook")
        conversations = {record.conversation_id for record in selected}
        if len(conversations) > 1:
            subjects = sorted({email_text.subject_root(record.subject) for record in selected})
            listing = "; ".join(f"'{subject}'" for subject in subjects)
            raise ValidationError(f"the selected emails belong to several conversations: {listing}")
        return _ref_for(max(selected, key=lambda record: _moment(record) or _EARLIEST))

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        if ref.object_type != "email_thread":
            raise ValidationError(f"the Outlook connector cannot resolve a {ref.object_type} reference")
        return ObjectMeta(
            "email_thread",
            ref.external_id,
            title=ref.display_ref,
            display_ref=ref.display_ref,
            locator=ref.locator,
        )

    def find_conversations(self, text: str, *, limit: int = 20) -> list[ConversationCandidate]:
        """Conversations with a mail whose subject contains `text`, newest first (CON-21)."""
        hits = self._session.find_by_subject(text, since=None, limit=limit * _SEARCH_OVERFETCH)
        return _candidates(hits)[:limit]

    def scan_mailbox(self, *, since: datetime | None = None) -> list[ConversationCandidate]:
        """Conversations with new messages in the configured folders since `since` (CON-26)."""
        if since is None:
            since = parse_iso(utcnow_iso()) - timedelta(days=self._scan_days)
        return _candidates(self._session.recent_messages(since=since))

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return [
            ObjectMeta(
                "email_thread",
                candidate.ref.external_id,
                title=candidate.subject,
                display_ref=candidate.subject,
                locator=candidate.ref.locator,
            )
            for candidate in self.find_conversations(query.text, limit=query.limit)
        ]

    @staticmethod
    def _anchor(state: ObjectState) -> _Anchor:
        if state.cursor:
            stored = json.loads(state.cursor)
            entry_id, store_id = stored["anchor"]
            return _Anchor(entry_id, store_id, stored.get("topic") or "", stored.get("latest"))
        topic = state.title or ""
        parts = (state.locator or "").split(":", 2)
        if len(parts) == 3 and parts[0] == "outlook":
            return _Anchor(parts[2], parts[1], topic, None)
        return _Anchor(None, None, topic, None)

    def _messages(self, state: ObjectState, anchor: _Anchor) -> list[MailRecord]:
        """The conversation from its anchor, else by topic (CON-22), in the configured folders only."""
        found = None
        if anchor.entry_id is not None and anchor.store_id is not None:
            found = self._session.conversation(anchor.entry_id, anchor.store_id)
        if found is None:
            found = self._session.conversation_by_topic(state.external_id, anchor.topic)
        allowed = set(self._session.folder_paths())
        return [record for record in found if record.folder_path in allowed]

    def _entry(self, record: MailRecord, occurred_at: str) -> EntryData:
        text = email_text.normalise_body(record.body) or email_text.html_to_text(record.html_body)
        sender = record.sender
        author = _identity(sender) if sender is not None else None
        metadata: dict[str, object] = {
            "subject": record.subject,
            "folder": record.folder_path,
            "to": [address.address for address in record.to],
            "cc": [address.address for address in record.cc],
            "in_reply_to": record.in_reply_to,
            "references": record.references,
            "attachment_count": len(record.attachments),
        }
        if sender is not None and author is None:
            metadata["sender_raw"] = sender.address
        return EntryData(
            "email_message",
            record.internet_message_id or f"entryid:{record.entry_id}",
            occurred_at,
            text,
            new_text=email_text.new_text(text, extra_markers=self._signature_markers),
            author=author,
            recipients=tuple(
                identity for identity in map(_identity, (*record.to, *record.cc)) if identity is not None
            ),
            metadata=metadata,
        )

    def _is_inline_image(self, filename: str, content_id: str | None, size: int) -> bool:
        return (
            bool(content_id)
            and PurePath(filename).suffix.lower() in _INLINE_IMAGE_EXTENSIONS
            and size < self._inline_image_max_bytes
        )

    def _attachments(self, record: MailRecord, message_id: str, warnings: list[str]) -> list[ChildData]:
        children = []
        for attachment in record.attachments:
            if self._is_inline_image(attachment.filename, attachment.content_id, attachment.size):
                continue
            try:
                data = self._session.attachment_bytes(record.entry_id, record.store_id, attachment.index)
            except Exception as exc:
                warnings.append(
                    f"cannot read attachment '{attachment.filename}' of '{record.subject}': {exc}"
                )
                continue
            result = extract.extract_text(data, attachment.filename, max_chars=self._max_text_chars)
            children.append(
                ChildData(
                    meta=ObjectMeta(
                        "attachment",
                        f"{message_id}#{attachment.index}",
                        title=attachment.filename,
                        display_ref=attachment.filename,
                        cursor=hashlib.sha256(data).hexdigest(),
                    ),
                    version=VersionData(
                        text=result.text,
                        metadata={
                            "filename": attachment.filename,
                            "size": len(data),
                            "extract_status": result.status,
                            "extract_detail": result.detail,
                            "truncated": result.truncated,
                        },
                        raw=data,
                        media_type=mimetypes.guess_type(attachment.filename)[0],
                    ),
                )
            )
        return children

    def _forks(self, state: ObjectState, message_ids: list[str]) -> list[str]:
        """One warning per other conversation that replies into this one (CON-25)."""
        if not message_ids:
            return []
        forks: dict[str, MailRecord] = {}
        for reply in self._session.find_replies_to(message_ids):
            if reply.conversation_id != state.external_id and reply.internet_message_id:
                forks.setdefault(reply.conversation_id, reply)
        return [
            f"forked conversation: '{email_text.subject_root(reply.subject)}'"
            f" — link it with: oil link <KEY> {reply.internet_message_id}"
            for reply in forks.values()
        ]

    def fetch(self, state: ObjectState) -> FetchResult:
        anchor = self._anchor(state)
        messages = self._messages(state, anchor)
        if not messages:
            return FetchResult(status="gone")

        warnings: list[str] = []
        dated: list[tuple[str, MailRecord]] = []
        for record in messages:
            moment = _moment(record)
            if moment is None:
                warnings.append(f"skipped an email without a date: '{record.subject}'")
                continue
            dated.append((_iso(moment), record))
        dated.sort(key=lambda pair: pair[0])
        if not dated:
            return FetchResult(status="gone", warnings=tuple(warnings))

        entries = []
        children: list[ChildData] = []
        for occurred_at, record in dated:
            entry = self._entry(record, occurred_at)
            entries.append(entry)
            if anchor.latest is None or occurred_at > anchor.latest:
                children.extend(self._attachments(record, entry.external_id, warnings))
        warnings.extend(
            self._forks(state, [r.internet_message_id for _, r in dated if r.internet_message_id])
        )

        newest_at, newest = dated[-1]
        cursor = json.dumps(
            {
                "anchor": [newest.entry_id, newest.store_id],
                "topic": newest.conversation_topic,
                "latest": newest_at,
            },
            sort_keys=True,
        )
        return FetchResult(
            entries=tuple(entries),
            children=tuple(children),
            cursor=cursor,
            title=email_text.subject_root(dated[0][1].subject),
            warnings=tuple(warnings),
        )
