"""Jira Cloud connector: issues with fields, comments and changelog (CON-30 .. CON-36)."""

import re
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from ai_oil.connectors.atlassian import (
    AtlassianClient,
    AtlassianError,
    AtlassianNotFound,
    adf_mentions,
    adf_to_text,
    to_utc_iso,
)
from ai_oil.errors import ValidationError
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

_KEY = r"[A-Z][A-Z0-9_]+-\d+"
_ISSUE_KEY = re.compile(rf"^{_KEY}$")
_BROWSE_PATH = re.compile(rf"^/browse/({_KEY})/?$")
_BOARD_ISSUE_PATH = re.compile(rf"^/jira/.*/issues/({_KEY})/?$")
_ISSUE_FIELDS = (
    "summary,description,status,assignee,reporter,priority,labels,components,fixVersions,duedate,"
    "resolution,updated,attachment"
)
_NONE = "-"


def _text_setting(settings: Mapping[str, object], key: str, *, required: bool) -> str | None:
    value = settings.get(key)
    if value is None:
        if required:
            raise ValidationError(f"Jira setting {key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Jira setting {key} must be non-blank text")
    return value.strip()


def _projects_setting(settings: Mapping[str, object]) -> list[str]:
    value = settings.get("projects", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError("Jira setting projects must be a list of project keys")
    return [item.strip() for item in value]


def _timeout_setting(settings: Mapping[str, object]) -> float:
    value = settings.get("timeout", 30)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValidationError("Jira setting timeout must be a positive number of seconds")
    return float(value)


def _base_url_setting(settings: Mapping[str, object]) -> str:
    value = _text_setting(settings, "base_url", required=True)
    assert value is not None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError("Jira setting base_url must be a URL such as https://acme.atlassian.net")
    return value.rstrip("/")


def _name(node: Any) -> str | None:
    return node.get("name") if isinstance(node, Mapping) else None


def _display_name(node: Any) -> str | None:
    return node.get("displayName") if isinstance(node, Mapping) else None


def _names(nodes: Any) -> list[str]:
    return [name for name in (_name(node) for node in nodes or []) if name]


def _joined(values: list[str]) -> str:
    return ", ".join(values) if values else _NONE


def _author(node: Any) -> IdentityRef | None:
    if not isinstance(node, Mapping) or not node.get("accountId"):
        return None
    return IdentityRef("atlassian", str(node["accountId"]), node.get("displayName"))


def _jql_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class JiraConnector:
    """Issues of one Jira Cloud site, read through an injectable REST client (CON-6)."""

    type: ClassVar[str] = "jira"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue", "attachment"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 4

    def __init__(self, config: ConnectorConfig, *, client: AtlassianClient | None = None) -> None:
        self.config = config
        settings = config.settings
        self._base_url = _base_url_setting(settings)
        email = _text_setting(settings, "email", required=True)
        assert email is not None
        self._projects = _projects_setting(settings)
        ca_bundle = _text_setting(settings, "ca_bundle", required=False)
        timeout = _timeout_setting(settings)
        self._host = (urlsplit(self._base_url).hostname or "").lower()
        self._client: AtlassianClient = (
            client
            if client is not None
            else AtlassianClient(
                self._base_url, email, config.credential_ref, ca_bundle=ca_bundle, timeout=timeout
            )
        )

    def _browse_url(self, key: str) -> str:
        return f"{self._base_url}/browse/{key}"

    def check(self) -> HealthReport:
        try:
            me = self._client.get_json("/rest/api/3/myself")
        except AtlassianError as exc:
            return HealthReport((HealthCheck("auth", False, str(exc)),))
        checks = [HealthCheck("auth", True, f"signed in as {_display_name(me) or _NONE}")]
        for key in self._projects:
            name = f"project {key}"
            try:
                project = self._client.get_json(f"/rest/api/3/project/{key}")
            except AtlassianNotFound:
                checks.append(HealthCheck(name, False, "not found or not visible"))
            except AtlassianError as exc:
                checks.append(HealthCheck(name, False, str(exc)))
            else:
                checks.append(HealthCheck(name, True, _name(project) or key))
        return HealthReport(tuple(checks))

    def parse_reference(self, text: str) -> SourceRef | None:
        candidate = text.strip()
        key = candidate if _ISSUE_KEY.match(candidate) else self._key_from_url(candidate)
        if key is None:
            return None
        return SourceRef("jira_issue", key, display_ref=key, locator=self._browse_url(key))

    def _key_from_url(self, text: str) -> str | None:
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != self._host:
            return None
        for selected in parse_qs(parts.query).get("selectedIssue", []):
            if _ISSUE_KEY.match(selected):
                return selected
        for pattern in (_BROWSE_PATH, _BOARD_ISSUE_PATH):
            match = pattern.match(parts.path)
            if match is not None:
                return match.group(1)
        return None

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        if ref.object_type != "jira_issue":
            raise ValidationError(f"the Jira connector cannot resolve a {ref.object_type} reference")
        try:
            issue = self._client.get_json(f"/rest/api/3/issue/{ref.external_id}", {"fields": "summary"})
        except AtlassianNotFound:
            raise ValidationError(f"Jira issue {ref.external_id} not found or not visible") from None
        key = str(issue["key"])
        return ObjectMeta(
            "jira_issue",
            str(issue["id"]),
            title=(issue.get("fields") or {}).get("summary"),
            display_ref=key,
            locator=self._browse_url(key),
        )

    def fetch(self, state: ObjectState) -> FetchResult:
        try:
            issue = self._client.get_json(f"/rest/api/3/issue/{state.external_id}", {"fields": _ISSUE_FIELDS})
        except AtlassianNotFound:
            return FetchResult(status="gone")
        fields = issue.get("fields") or {}
        updated = fields.get("updated")
        entries = [*self._comments(state.external_id), *self._changes(state.external_id)]
        return FetchResult(
            version=self._version(issue, fields),
            entries=tuple(entries),
            children=tuple(self._attachments(state.external_id, fields)),
            cursor=updated,
            title=fields.get("summary"),
        )

    @staticmethod
    def _version(issue: Mapping[str, Any], fields: Mapping[str, Any]) -> VersionData:
        key = str(issue.get("key"))
        assignee, reporter = fields.get("assignee"), fields.get("reporter")
        labels = [str(label) for label in fields.get("labels") or []]
        components = _names(fields.get("components"))
        fix_versions = _names(fields.get("fixVersions"))
        header = [
            f"{key}: {fields.get('summary')}",
            "",
            f"Status: {_name(fields.get('status')) or _NONE}",
            f"Assignee: {_display_name(assignee) or _NONE}",
            f"Reporter: {_display_name(reporter) or _NONE}",
            f"Priority: {_name(fields.get('priority')) or _NONE}",
            f"Due: {fields.get('duedate') or _NONE}",
            f"Labels: {_joined(labels)}",
            f"Components: {_joined(components)}",
            f"Fix versions: {_joined(fix_versions)}",
            f"Resolution: {_name(fields.get('resolution')) or _NONE}",
        ]
        description = adf_to_text(fields.get("description"))
        text = "\n".join(header) + (f"\n\n{description}" if description else "")
        updated = fields.get("updated")
        return VersionData(
            text=text,
            metadata={
                "key": key,
                "status": _name(fields.get("status")),
                "assignee": _display_name(assignee),
                "assignee_account_id": assignee.get("accountId") if isinstance(assignee, Mapping) else None,
                "reporter": _display_name(reporter),
                "priority": _name(fields.get("priority")),
                "due": fields.get("duedate"),
                "labels": labels,
                "components": components,
                "fix_versions": fix_versions,
                "resolution": _name(fields.get("resolution")),
            },
            source_modified_at=to_utc_iso(updated),
            source_version_ref=updated,
        )

    def _comments(self, issue_id: str) -> list[EntryData]:
        comments = self._client.get_paged(
            f"/rest/api/3/issue/{issue_id}/comment", {"orderBy": "created"}, values_key="comments"
        )
        return [
            EntryData(
                "jira_comment",
                str(comment["id"]),
                to_utc_iso(comment.get("created")) or "",
                adf_to_text(comment.get("body")),
                author=_author(comment.get("author")),
                metadata={
                    "updated": to_utc_iso(comment.get("updated")),
                    "mentions": adf_mentions(comment.get("body")),
                },
            )
            for comment in comments
        ]

    def _changes(self, issue_id: str) -> list[EntryData]:
        histories = self._client.get_paged(
            f"/rest/api/3/issue/{issue_id}/changelog", None, values_key="values"
        )
        changes = []
        for history in histories:
            for index, item in enumerate(history.get("items") or []):
                changes.append(
                    EntryData(
                        "jira_change",
                        f"{history['id']}:{index}",
                        to_utc_iso(history.get("created")) or "",
                        f"{item.get('field')}: {item.get('fromString') or _NONE}"
                        f" → {item.get('toString') or _NONE}",
                        author=_author(history.get("author")),
                        metadata={
                            "field": item.get("field"),
                            "from": item.get("fromString"),
                            "to": item.get("toString"),
                            "from_id": item.get("from"),
                            "to_id": item.get("to"),
                        },
                    )
                )
        return changes

    @staticmethod
    def _attachments(issue_id: str, fields: Mapping[str, Any]) -> list[ChildData]:
        return [
            ChildData(
                meta=ObjectMeta(
                    "attachment",
                    f"{issue_id}#{attachment['id']}",
                    title=attachment.get("filename"),
                    display_ref=attachment.get("filename"),
                    locator=attachment.get("content"),
                    cursor=str(attachment["id"]),
                ),
                version=VersionData(
                    text=str(attachment.get("filename") or ""),
                    metadata={
                        "filename": attachment.get("filename"),
                        "size": attachment.get("size"),
                        "mime_type": attachment.get("mimeType"),
                        "author": _display_name(attachment.get("author")),
                        "created": to_utc_iso(attachment.get("created")),
                    },
                ),
            )
            for attachment in fields.get("attachment") or []
        ]

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        jql = f'text ~ "{_jql_text(query.text)}"'
        if self._projects:
            jql += f" AND project in ({', '.join(self._projects)})"
        jql += f' AND updated >= "{query.since[:10]}"' if query.since else " AND updated >= -30d"
        jql += " ORDER BY updated DESC"
        found = self._client.get_json(
            "/rest/api/3/search/jql", {"jql": jql, "maxResults": query.limit, "fields": "summary,updated"}
        )
        return [
            ObjectMeta(
                "jira_issue",
                str(issue["id"]),
                title=(issue.get("fields") or {}).get("summary"),
                display_ref=issue["key"],
                locator=self._browse_url(issue["key"]),
            )
            for issue in found.get("issues") or []
        ]
