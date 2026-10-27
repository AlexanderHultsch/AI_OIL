"""SharePoint / OneDrive connector: files read from the locally synced folder (CON-50 .. CON-55)."""

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol
from urllib.parse import unquote, urlsplit

from ai_oil.errors import ValidationError
from ai_oil.sources import extract
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    FetchResult,
    HealthCheck,
    HealthReport,
    ObjectMeta,
    ObjectState,
    SourceRef,
    VersionData,
)
from ai_oil.timeutil import parse_iso

_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_OFFLINE_MASK = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_SHARING_PREFIX = re.compile(r"^/:[A-Za-z]:/r(?=/)")
_OPAQUE_SHARING_PREFIX = re.compile(r"^/:[A-Za-z]:/s(?=/|$)")
_CONFLICT_SUFFIX = r"-[A-Za-z0-9_]+(-\d+)?"
_LIBRARY_SEGMENTS = 3

_OPAQUE_LINK = (
    "opaque SharePoint sharing links cannot be mapped to a synced file — open the file in the browser"
    " and copy its path-style URL (…/sites/…/Shared Documents/…)"
)


@dataclass(frozen=True)
class FileInfo:
    mtime: float
    size: int
    offline: bool


class FileSystem(Protocol):
    """The file-system access of the connector (CON-6)."""

    def is_file(self, path: str) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def info(self, path: str) -> FileInfo: ...

    def read_bytes(self, path: str) -> bytes: ...

    def list_dir(self, path: str) -> list[str]: ...

    def walk_files(self, root: str) -> Iterator[str]: ...


class LocalFileSystem:
    """The real file system; an offline file is a Files On-Demand placeholder that a read would download."""

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def info(self, path: str) -> FileInfo:
        stat = os.stat(path)
        attributes = getattr(stat, "st_file_attributes", 0)
        return FileInfo(stat.st_mtime, stat.st_size, bool(attributes & _OFFLINE_MASK))

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def list_dir(self, path: str) -> list[str]:
        return os.listdir(path)

    def walk_files(self, root: str) -> Iterator[str]:
        for directory, _, names in os.walk(root):
            for name in names:
                yield os.path.join(directory, name)


@dataclass(frozen=True)
class _SyncRoot:
    url: str
    path: str
    segments: tuple[str, ...]


def _url_segments(host: str, path: str) -> list[str]:
    return [host, *(segment for segment in path.split("/") if segment)]


def _sync_roots_setting(settings: Mapping[str, object]) -> list[_SyncRoot]:
    message = 'SharePoint setting sync_roots must be a list of {"url": ..., "path": ...} objects'
    value = settings.get("sync_roots", [])
    if not isinstance(value, list):
        raise ValidationError(message)
    roots = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValidationError(message)
        url, path = item.get("url"), item.get("path")
        if not isinstance(url, str) or not url.strip() or not isinstance(path, str) or not path.strip():
            raise ValidationError(message)
        parts = urlsplit(url.strip())
        segments = _url_segments(parts.hostname or "", unquote(parts.path))
        roots.append(_SyncRoot(url.strip(), path.strip(), tuple(segment.lower() for segment in segments)))
    return roots


def _bool_setting(settings: Mapping[str, object], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(f"SharePoint setting {key} must be true or false")
    return value


def _int_setting(settings: Mapping[str, object], key: str, default: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"SharePoint setting {key} must be a non-negative whole number")
    return value


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def _iso(timestamp: float) -> str:
    moment = _utc(timestamp)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def _file_ref(path: str, locator: str | None = None) -> SourceRef:
    normalised = os.path.normpath(path)
    return SourceRef(
        "file", normalised, display_ref=os.path.basename(normalised), locator=locator or normalised
    )


class SharePointConnector:
    """Files of synced SharePoint libraries and OneDrive, read through an injectable file system (CON-6)."""

    type: ClassVar[str] = "sharepoint"
    object_types: ClassVar[frozenset[str]] = frozenset({"file"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 4

    def __init__(self, config: ConnectorConfig, *, fs: FileSystem | None = None) -> None:
        self.config = config
        settings = config.settings
        self._roots = _sync_roots_setting(settings)
        self._hydrate = _bool_setting(settings, "hydrate", True)
        self._max_text_chars = _int_setting(settings, "max_text_chars", 2_000_000)
        self._fs: FileSystem = fs if fs is not None else LocalFileSystem()

    def check(self) -> HealthReport:
        if not self._roots:
            return HealthReport((HealthCheck("sync roots", False, "no sync_roots configured"),))
        checks = []
        for root in self._roots:
            present = self._fs.is_dir(root.path)
            checks.append(
                HealthCheck(
                    f"sync root {root.url}",
                    present,
                    root.path if present else f"folder not found: {root.path}",
                )
            )
        return HealthReport(tuple(checks))

    def parse_reference(self, text: str) -> SourceRef | None:
        candidate = text.strip()
        if os.path.isabs(candidate):
            return _file_ref(candidate) if self._fs.is_file(candidate) else None
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(".sharepoint.com"):
            return None
        return _file_ref(self._map_url(host, unquote(parts.path)), candidate)

    def _map_url(self, host: str, path: str) -> str:
        if _OPAQUE_SHARING_PREFIX.match(path):
            raise ValidationError(_OPAQUE_LINK)
        segments = _url_segments(host, _SHARING_PREFIX.sub("", path, count=1))
        folded = [segment.lower() for segment in segments]
        best: _SyncRoot | None = None
        for root in self._roots:
            size = len(root.segments)
            if folded[:size] == list(root.segments) and (best is None or size > len(best.segments)):
                best = root
        if best is None:
            library = "/" + "/".join(segments[1 : 1 + _LIBRARY_SEGMENTS])
            raise ValidationError(
                f"this SharePoint library is not synced on this computer: {host}{library}"
                " — sync it with OneDrive and add it to the connector's sync_roots (CON-51)"
            )
        return os.path.join(best.path, *segments[len(best.segments) :])

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        if ref.object_type != "file":
            raise ValidationError(f"the SharePoint connector cannot resolve a {ref.object_type} reference")
        path = ref.external_id
        if not self._fs.is_file(path):
            raise ValidationError(f"file not found: {path} — is the library synced and the file available?")
        name = os.path.basename(path)
        return ObjectMeta("file", path, title=name, display_ref=name, locator=ref.locator)

    def _conflict_warnings(self, path: str) -> list[str]:
        directory, name = os.path.split(path)
        stem, extension = os.path.splitext(name)
        pattern = re.compile(re.escape(stem) + _CONFLICT_SUFFIX)
        found = []
        for other in sorted(self._fs.list_dir(directory)):
            other_stem, other_extension = os.path.splitext(other)
            if (
                other != name
                and other_extension.lower() == extension.lower()
                and pattern.fullmatch(other_stem)
                and self._fs.is_file(os.path.join(directory, other))
            ):
                found.append(f"conflict copy next to this file: {other} (not followed)")
        return found

    def fetch(self, state: ObjectState) -> FetchResult:
        path = state.external_id
        if not self._fs.is_file(path):
            return FetchResult(status="gone")
        name = os.path.basename(path)
        warnings = self._conflict_warnings(path)
        info = self._fs.info(path)
        previous = json.loads(state.cursor) if state.cursor else {}
        if previous.get("mtime") == info.mtime and previous.get("size") == info.size:
            return FetchResult(cursor=state.cursor, title=name, warnings=tuple(warnings))
        if info.offline:
            if not self._hydrate:
                warnings.append("not available offline — skipped (hydrate is off)")
                return FetchResult(cursor=state.cursor, title=name, warnings=tuple(warnings))
            warnings.append("hydrated: downloaded on fetch (Files On-Demand)")
        data = self._fs.read_bytes(path)
        digest = hashlib.sha256(data).hexdigest()
        cursor = json.dumps({"mtime": info.mtime, "size": info.size, "sha256": digest}, sort_keys=True)
        if previous.get("sha256") == digest:
            return FetchResult(cursor=cursor, title=name, warnings=tuple(warnings))
        result = extract.extract_text(data, name, max_chars=self._max_text_chars)
        if result.status != "ok":
            warnings.append(f"text extraction: {result.status} — {result.detail}")
        version = VersionData(
            text=result.text,
            metadata={
                "filename": name,
                "size": len(data),
                "sha256": digest,
                "extract_status": result.status,
                "extract_detail": result.detail,
                "truncated": result.truncated,
            },
            source_modified_at=_iso(info.mtime),
            source_version_ref=digest[:12],
        )
        return FetchResult(version=version, cursor=cursor, title=name, warnings=tuple(warnings))

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        needle = query.text.casefold()
        since = parse_iso(query.since) if query.since else None
        found: dict[str, float] = {}
        for root in self._roots:
            for path in self._fs.walk_files(root.path):
                normalised = os.path.normpath(path)
                if needle not in os.path.basename(normalised).casefold():
                    continue
                mtime = self._fs.info(normalised).mtime
                if since is None or _utc(mtime) >= since:
                    found[normalised] = mtime
        newest = sorted(found.items(), key=lambda item: (-item[1], item[0]))[: query.limit]
        return [
            ObjectMeta(
                "file",
                path,
                title=os.path.basename(path),
                display_ref=os.path.basename(path),
                locator=path,
            )
            for path, _ in newest
        ]
