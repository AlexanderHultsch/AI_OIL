"""The wizard stages that talk to the outside world: Outlook, the network, Jira/Confluence, SharePoint and
the Teams bridge (installer concept, stages 4-7).

Everything here is read-only towards Outlook, Jira, Confluence, SharePoint and the Teams bridge files; the
network stage sends nothing but plain GET requests and uses no credentials, the Atlassian stage sends GET
requests only.
"""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
import sqlalchemy

from ai_oil import secrets
from ai_oil.connectors.atlassian import AtlassianClient, AtlassianError, system_certificate_session
from ai_oil.connectors.outlook_session import ComOutlookSession, OutlookSession
from ai_oil.connectors.teams import TeamsConnector
from ai_oil.domain import people
from ai_oil.errors import ConfigError, NotFoundError
from ai_oil.governance import audit
from ai_oil.setup.console import SkipStage
from ai_oil.setup.model import SetupContext, StageResult
from ai_oil.setup.probe import InternetSettings, SyncedLibrary
from ai_oil.setup.stages_basic import _as_path
from ai_oil.sources import registry
from ai_oil.sources.model import ConnectorConfig

_OUTLOOK_CONNECTOR_NAME = "Outlook"
_DEFAULT_PROBE_URL = "https://api.atlassian.com/"
_DETAIL_LIMIT = 200
_DETAIL_CHECKS = (("mailbox", "Mailbox"), ("inbox", "Inbox"), ("sent", "Sent"))
_JIRA_CONNECTOR_NAME = "Jira"
_CONFLUENCE_CONNECTOR_NAME = "Confluence"
_SHAREPOINT_CONNECTOR_NAME = "SharePoint"
_TEAMS_CONNECTOR_NAME = "Teams"
_TEAMS_FOLDER_NAME = "AI_OIL-Teams"
_TEAMS_OPEN_ITEM = (
    "Teams: set up the bridge with docs/teams_bridge_setup.md, then run `oil setup --only teams`"
)
_TOKEN_PAGE = "https://id.atlassian.com/manage-profile/security/api-tokens"
_AUTH_FAILED = "authentication failed"
_LIST_LIMIT = 30
_SPACE_PAGE = 100
_SYNC_ACTION = "open the library in the browser, click Sync, then run `oil setup --only sharepoint`"


class OutlookStage:
    name = "outlook"
    title = "Outlook"

    def __init__(self, *, session_factory: Callable[[], OutlookSession] = ComOutlookSession) -> None:
        self._session_factory = session_factory

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        probe = ctx.probe
        if not probe.is_windows():
            return StageResult(
                "skipped",
                ["Outlook is only available on Windows"],
                ["Outlook: not available on this computer"],
            )
        if not probe.outlook_installed():
            return StageResult(
                "skipped",
                ["Classic Outlook is not installed"],
                [
                    "Outlook: install classic Outlook (not the new Outlook), "
                    "then run `oil setup --only outlook`"
                ],
            )

        while probe.new_outlook_enabled():
            console.say("The new Outlook is switched on. It has no interface AI_OIL can use.")
            console.say(
                'Switch off the "New Outlook" toggle at the top right of Outlook and start classic Outlook.'
            )
            if not console.confirm("Check again?", default=True):
                raise SkipStage

        while True:
            checks = self._session_factory().check()
            for check in checks:
                console.say(f"[{'ok' if check.ok else '!!'}] {check.name}: {check.detail}")
            if all(check.ok for check in checks):
                break
            console.say("Start classic Outlook and sign in.")
            if not console.confirm("Check again?", default=True):
                raise SkipStage

        lines = ["Outlook: classic, reachable"]
        open_items: list[str] = []

        with ctx.engine.begin() as conn:
            existing = registry.list_connectors(conn, type_name="outlook")
            if existing:
                connector_name = existing[0].name
                console.say(f"Using the existing connector {connector_name}")
            else:
                row = registry.create_connector(conn, "outlook", _OUTLOOK_CONNECTOR_NAME)
                registry.instantiate(row)
                audit.log(
                    conn,
                    "connector.create",
                    "B",
                    actor_type="user",
                    target=row.name,
                    detail={"type": "outlook", "settings": {}, "credential": "none"},
                )
                connector_name = row.name
        lines.append(f"Connector: {connector_name}")
        details = {check.name: check.detail for check in checks}
        lines += [f"{label}: {details[key]}" for key, label in _DETAIL_CHECKS if key in details]

        added = self._offer_addresses(ctx, open_items)
        lines += [f"Added e-mail address: {address}" for address in added]
        return StageResult("done", lines, open_items)

    def _offer_addresses(self, ctx: SetupContext, open_items: list[str]) -> list[str]:
        """Offer every Outlook account address that is not yet one of mine; returns the ones added."""
        console = ctx.console
        with ctx.engine.connect() as conn:
            try:
                me = people.get_me(conn)
            except NotFoundError:
                open_items.append("About you: run `oil setup --only me`")
                return []
            accepted: list[str] = []
            for address in ctx.probe.outlook_account_addresses():
                owner = people.find_person_by_identity(conn, "email", address)
                if owner is not None and owner.id == me.id:
                    continue
                if owner is not None:
                    console.say(
                        f"{address} belongs to {owner.display_name} — "
                        "merge that person into you on the People page"
                    )
                    continue
                if console.confirm(f"Add {address} to your e-mail addresses?", default=True):
                    accepted.append(address)
            my_id = me.id
        if accepted:
            with ctx.engine.begin() as conn:
                for address in accepted:
                    people.add_identity(conn, my_id, "email", address)
        return accepted


@dataclass(frozen=True)
class ProbeOutcome:
    """Result of one GET: `kind` is "ok", "ssl", "proxy", "connect" or "other"."""

    kind: str
    detail: str


def _detail(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= _DETAIL_LIMIT else text[: _DETAIL_LIMIT - 3] + "..."


def probe_https(
    session: requests.Session,
    url: str,
    *,
    verify: bool | str = True,
    proxies: dict | None = None,
    timeout: float = 15.0,
) -> ProbeOutcome:
    """One GET without following redirects; any HTTP answer, whatever its status, means "reachable"."""
    try:
        response = session.get(url, allow_redirects=False, timeout=timeout, verify=verify, proxies=proxies)
    except requests.exceptions.SSLError as exc:
        return ProbeOutcome("ssl", _detail(exc))
    except requests.exceptions.ProxyError as exc:
        return ProbeOutcome("proxy", _detail(exc))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        return ProbeOutcome("connect", _detail(exc))
    except requests.exceptions.RequestException as exc:
        return ProbeOutcome("other", _detail(exc))
    try:
        return ProbeOutcome("ok", f"HTTP {response.status_code}")
    finally:
        response.close()


def _probe_url(rows: list[registry.ConnectorRow]) -> str:
    for row in rows:
        base_url = row.settings.get("base_url")
        parts = urlsplit(base_url) if isinstance(base_url, str) else None
        if parts is not None and parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/"
    return _DEFAULT_PROBE_URL


def _proxy_format_error(text: str) -> str | None:
    example = "http://proxy.company.com:8080"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"Enter the address as {example}."
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        return f"The address needs a port, like {example}."
    return None


class NetworkStage:
    name = "network"
    title = "Network"

    def __init__(
        self,
        *,
        system_session: Callable[[], requests.Session] = system_certificate_session,
        plain_session: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        self._system_session = system_session
        self._plain_session = plain_session

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        with ctx.engine.connect() as conn:
            atlassian = registry.list_connectors(conn, type_name="jira") + registry.list_connectors(
                conn, type_name="confluence"
            )
        url = _probe_url(atlassian)
        console.say(f"Checking the connection to {url}")

        settings = ctx.probe.internet_settings()
        pac = settings.auto_config_url is not None and settings.proxy_server is None
        self._describe_proxy(ctx, settings, pac)

        lines: list[str] = []
        open_items: list[str] = []
        proxies: dict[str, str] | None = None
        store_proxy: str | None = None

        system, plain = self._assess(url, proxies)
        if system.kind not in ("ok", "ssl"):
            if not pac and (settings.proxy_server or settings.env_proxy):
                return StageResult(
                    "failed",
                    [system.detail],
                    [
                        f"Network: {url} is not reachable ({system.detail}) — check the VPN or ask IT, "
                        "then run `oil setup --only network`"
                    ],
                )
            console.say(f"{url} is not reachable: {system.detail}")
            console.say("A proxy may be needed. Ask IT for the address if you do not know it.")
            assessed: dict[str, tuple[ProbeOutcome, ProbeOutcome]] = {}

            def validate_proxy(text: str) -> str | None:
                error = _proxy_format_error(text)
                if error is not None:
                    return error
                pair = self._assess(url, {"https": text, "http": text})
                if pair[0].kind not in ("ok", "ssl"):
                    return f"{text} did not work: {pair[0].detail}"
                assessed[text] = pair
                return None

            proxy = console.ask("Proxy address, like http://proxy.company.com:8080", validate=validate_proxy)
            proxies = {"https": proxy, "http": proxy}
            system, plain = assessed[proxy]
            lines.append(f"Proxy: {proxy}")
            if console.confirm(
                "Store HTTPS_PROXY for your Windows user so AI_OIL always uses it?", default=True
            ):
                store_proxy = proxy
            else:
                open_items.append(f"Network: set HTTPS_PROXY={proxy} for AI_OIL")

        bundle_targets: list[registry.ConnectorRow] = []
        if system.kind == "ssl":
            bundle = self._ask_bundle(ctx, url, proxies)
            ctx.state["ca_bundle"] = bundle
            lines.append(f"CA bundle: {bundle}")
            if atlassian:
                console.say("The CA bundle can be used by your Jira and Confluence connectors.")
            bundle_targets = [row for row in atlassian if console.confirm(f"Use it for {row.name}?")]
        elif plain.kind == "ssl":
            lines.append("SSL inspection detected — handled by the Windows certificate store, nothing to do")
        elif plain.kind == "ok":
            lines.append("No SSL inspection")
        else:
            lines.append(f"{url} is reachable")

        if store_proxy is not None:
            ctx.probe.set_user_env("HTTPS_PROXY", store_proxy)
            lines.append("HTTPS_PROXY stored for your Windows user")
        for row in bundle_targets:
            self._apply_bundle(ctx, row, str(ctx.state["ca_bundle"]))
            lines.append(f"CA bundle set for {row.name}")
        return StageResult("done", lines, open_items)

    def _assess(self, url: str, proxies: dict[str, str] | None) -> tuple[ProbeOutcome, ProbeOutcome]:
        """The outcome with the Windows certificate store, then with the bundled roots only."""
        return (
            self._probe(self._system_session, url, proxies=proxies),
            self._probe(self._plain_session, url, proxies=proxies),
        )

    @staticmethod
    def _probe(
        factory: Callable[[], requests.Session],
        url: str,
        *,
        verify: bool | str = True,
        proxies: dict[str, str] | None = None,
    ) -> ProbeOutcome:
        session = factory()
        try:
            return probe_https(session, url, verify=verify, proxies=proxies)
        finally:
            session.close()

    @staticmethod
    def _describe_proxy(ctx: SetupContext, settings: InternetSettings, pac: bool) -> None:
        console = ctx.console
        if settings.proxy_server:
            console.say(f"Windows proxy {settings.proxy_server} is used automatically.")
        if settings.env_proxy:
            console.say(f"HTTPS_PROXY {settings.env_proxy} is used automatically.")
        if pac:
            console.say(
                f"Windows uses an automatic configuration script ({settings.auto_config_url}), "
                "which Python cannot read."
            )
        if not (settings.proxy_server or settings.env_proxy or pac):
            console.say("No proxy is configured.")

    def _ask_bundle(self, ctx: SetupContext, url: str, proxies: dict[str, str] | None) -> str:
        console = ctx.console
        console.say("The connection is inspected with a certificate that this computer does not trust.")
        console.say(
            "Ask IT for the company root certificate as a CA bundle file (PEM, often ending in .pem or .crt)."
        )

        def validate(text: str) -> str | None:
            if not text:
                return "Enter the path of the CA bundle file."
            path = _as_path(text)
            if not path.is_file():
                return f"{path} is not a file."
            outcome = self._probe(self._plain_session, url, verify=str(path), proxies=proxies)
            if outcome.kind != "ok":
                return f"The bundle does not work for {url}: {outcome.detail}"
            return None

        return str(_as_path(console.ask("CA bundle file", validate=validate)))

    @staticmethod
    def _apply_bundle(ctx: SetupContext, row: registry.ConnectorRow, bundle: str) -> None:
        with ctx.engine.begin() as conn:
            updated = registry.update_settings(conn, row.id, {**row.settings, "ca_bundle": bundle})
            registry.instantiate(updated)
            audit.log(
                conn,
                "connector.update",
                "B",
                actor_type="user",
                target=row.name,
                detail={"type": row.type, "settings": updated.settings, "credential": "unchanged"},
            )


def normalise_site(text: str) -> str | None:
    """`https://<host>` of a pasted Jira link, a bare host or an http address; None without a dotted host."""
    candidate = text.strip().strip('"').strip("'")
    if not candidate or any(char.isspace() for char in candidate):
        return None
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "." not in host.strip("."):
        return None
    return f"https://{host}"


@contextmanager
def _config_errors() -> Iterator[None]:
    """Show an Atlassian failure as a failed stage; the client's messages never contain the token."""
    try:
        yield
    except AtlassianError as exc:
        raise ConfigError(str(exc)) from None


def _validate_site(text: str) -> str | None:
    if normalise_site(text) is None:
        return "Enter a Jira link or the address of your site, like acme.atlassian.net."
    return None


def _validate_email(text: str) -> str | None:
    return None if "@" in text else "The e-mail address needs an @."


def _split_keys(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for part in text.replace(",", " ").split():
        seen.setdefault(part, None)
    return list(seen)


@dataclass
class _Access:
    """What is needed to talk to Atlassian; the token stays in memory until the write at the end."""

    site: str
    email: str
    ca_bundle: str | None
    token: str | None = None
    token_is_new: bool = False


class AtlassianStage:
    name = "atlassian"
    title = "Jira and Confluence"

    def __init__(self, *, session_factory: Callable[[], requests.Session] | None = None) -> None:
        self._session_factory = session_factory

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        with ctx.engine.connect() as conn:
            jira_rows = registry.list_connectors(conn, type_name="jira")
            confluence_rows = registry.list_connectors(conn, type_name="confluence")
            try:
                me = people.get_me(conn)
            except NotFoundError:
                me = None
            my_addresses = (
                [i.external_id for i in people.list_identities(conn, me.id) if i.system == "email"]
                if me is not None
                else []
            )
        jira = jira_rows[0] if jira_rows else None
        confluence = confluence_rows[0] if confluence_rows else None

        default_site = self._text(jira, "base_url")
        site = normalise_site(
            console.ask(
                "Paste any Jira link, or your site address", default=default_site, validate=_validate_site
            )
        )
        assert site is not None
        console.say(f"Jira site: {site}")

        bundle = ctx.state.get("ca_bundle") or self._text(jira, "ca_bundle")
        email = console.ask(
            "Your Atlassian e-mail address",
            default=self._text(jira, "email")
            or (my_addresses[0] if my_addresses else None)
            or ctx.probe.user_principal_name(),
            validate=_validate_email,
        )
        access = _Access(site, email, str(bundle) if bundle else None)
        self._choose_token(ctx, access, jira)

        (account,) = self._login(ctx, access, [(site, "/rest/api/3/myself")])
        projects = self._choose_projects(ctx, access, jira)

        wiki = f"{site}/wiki"
        spaces: list[str] | None = None
        open_items: list[str] = []
        if console.confirm(f"Also set up Confluence on {wiki} with the same account?", default=True):
            wiki_ok = True
            try:
                self._client(access, wiki).get_json("/rest/api/user/current")
            except AtlassianError as exc:
                console.say(str(exc))
                if console.confirm("Leave Confluence out for now?", default=True):
                    wiki_ok = False
                    open_items.append(f"Confluence: {exc} — run `oil setup --only atlassian` later")
                else:
                    account, _ = self._login(
                        ctx, access, [(site, "/rest/api/3/myself"), (wiki, "/rest/api/user/current")]
                    )
            if wiki_ok:
                spaces = self._choose_spaces(ctx, access, wiki, confluence)

        account_id = str(account.get("accountId") or "") if isinstance(account, dict) else ""
        display_name = (str(account.get("displayName") or "") if isinstance(account, dict) else "") or email
        link_identity = self._check_identity(ctx, me, account_id, display_name, open_items)

        shared = {"ca_bundle": access.ca_bundle} if access.ca_bundle else {}
        with ctx.engine.begin() as conn:
            stored: list[tuple[str, str]] = []
            stored += self._write(
                conn,
                access,
                jira,
                "jira",
                _JIRA_CONNECTOR_NAME,
                {"base_url": site, "email": access.email, "projects": projects, **shared},
            )
            if spaces is not None:
                stored += self._write(
                    conn,
                    access,
                    confluence,
                    "confluence",
                    _CONFLUENCE_CONNECTOR_NAME,
                    {"base_url": wiki, "email": access.email, "spaces": spaces, **shared},
                )
            if link_identity and me is not None:
                people.add_identity(conn, me.id, "atlassian", account_id)
            for ref, token in stored:
                secrets.set_secret(ref, token)

        lines = [f"Jira: {site} — projects {', '.join(projects)}"]
        if spaces is not None:
            lines.append(f"Confluence: {wiki} — spaces {', '.join(spaces)}")
        lines.append(f"Atlassian account: {display_name}")
        return StageResult("done", lines, open_items)

    @staticmethod
    def _text(row: registry.ConnectorRow | None, key: str) -> str | None:
        value = row.settings.get(key) if row is not None else None
        return value if isinstance(value, str) and value.strip() else None

    def _client(self, access: _Access, base_url: str) -> AtlassianClient:
        session = self._session_factory() if self._session_factory is not None else None
        return AtlassianClient(
            base_url, access.email, None, token=access.token, ca_bundle=access.ca_bundle, session=session
        )

    def _choose_token(self, ctx: SetupContext, access: _Access, jira: registry.ConnectorRow | None) -> None:
        console = ctx.console
        stored = secrets.get_secret(jira.credential_ref) if jira and jira.credential_ref else None
        if stored and console.confirm("Keep the stored API token?", default=True):
            access.token, access.token_is_new = stored, False

    def _read_token(self, ctx: SetupContext, access: _Access) -> None:
        console = ctx.console
        console.say(f"An API token lets AI_OIL read Jira and Confluence as you. Create one at {_TOKEN_PAGE}")
        console.say("It goes into the Windows Credential Manager only. Type s to skip this stage.")
        while True:
            token = console.secret("API token (input hidden)").strip()
            if token.lower() == "s":
                raise SkipStage
            if token:
                access.token, access.token_is_new = token, True
                return
            console.say("The API token must not be empty.")

    def _login(self, ctx: SetupContext, access: _Access, probes: list[tuple[str, str]]) -> list[object]:
        """Fetch every probe path with the current credentials; on a rejection ask again, storing nothing."""
        console = ctx.console
        while True:
            if access.token is None:
                self._read_token(ctx, access)
            try:
                return [self._client(access, base).get_json(path) for base, path in probes]
            except AtlassianError as exc:
                if _AUTH_FAILED not in str(exc):
                    raise ConfigError(str(exc)) from None
                console.say(str(exc))
                if not console.confirm(f"Is {access.email} right?", default=True):
                    access.email = console.ask(
                        "Your Atlassian e-mail address", default=access.email, validate=_validate_email
                    )
                access.token = None

    def _choose_projects(
        self, ctx: SetupContext, access: _Access, jira: registry.ConnectorRow | None
    ) -> list[str]:
        client = self._client(access, access.site)
        with _config_errors():
            found = client.get_paged("/rest/api/3/project/search", None, values_key="values")
        available = [
            (str(item["key"]).upper(), str(item.get("name") or ""))
            for item in found
            if isinstance(item, dict) and item.get("key")
        ]
        current = jira.settings.get("projects") if jira is not None else None
        return self._pick(
            ctx,
            "Jira projects",
            "projects",
            available,
            [str(key) for key in current] if isinstance(current, list) else [],
            upper=True,
        )

    def _choose_spaces(
        self, ctx: SetupContext, access: _Access, wiki: str, confluence: registry.ConnectorRow | None
    ) -> list[str]:
        client = self._client(access, wiki)
        found: list[object] = []
        with _config_errors():
            start = 0
            while True:
                page = client.get_json(
                    "/rest/api/space",
                    {"type": "global", "status": "current", "start": start, "limit": _SPACE_PAGE},
                )
                results = page.get("results") if isinstance(page, dict) else None
                if not isinstance(results, list):
                    raise AtlassianError("unexpected response from /rest/api/space: no 'results' list")
                found.extend(results)
                if len(results) < _SPACE_PAGE:
                    break
                start += _SPACE_PAGE
        available = [
            (str(item["key"]), str(item.get("name") or ""))
            for item in found
            if isinstance(item, dict) and item.get("key")
        ]
        current = confluence.settings.get("spaces") if confluence is not None else None
        return self._pick(
            ctx,
            "Confluence spaces",
            "spaces",
            available,
            [str(key) for key in current] if isinstance(current, list) else [],
            upper=False,
        )

    @staticmethod
    def _pick(
        ctx: SetupContext,
        label: str,
        noun: str,
        available: list[tuple[str, str]],
        current: list[str],
        *,
        upper: bool,
    ) -> list[str]:
        """Choose from a numbered list, or type the keys when there are many; at least one is required."""
        console = ctx.console
        if not available:
            raise ConfigError(f"this account sees no {noun}")
        wanted = {key.upper() for key in current}
        if len(available) <= _LIST_LIMIT:
            options = [f"{key}  {name}".rstrip() for key, name in available]
            defaults = [index for index, (key, _) in enumerate(available) if key.upper() in wanted]
            while True:
                chosen = console.choose_many(f"{label} to read:", options, defaults=defaults)
                if chosen:
                    keys = [available[index][0] for index in chosen]
                    return [key.upper() for key in keys] if upper else keys
                console.say("Select at least one.")

        console.say(f"You can see {len(available)} {noun}; too many to list.")
        by_key = {key.upper(): key for key, _ in available}

        def validate(text: str) -> str | None:
            keys = _split_keys(text)
            if not keys:
                return "Enter at least one key."
            for key in keys:
                if key.upper() not in by_key:
                    return f"{key} is not one of your {noun}."
            return None

        answer = console.ask(
            f"{label} to read, keys separated by commas",
            default=", ".join(current) or None,
            validate=validate,
        )
        resolved = dict.fromkeys(by_key[key.upper()] for key in _split_keys(answer))
        return [key.upper() for key in resolved] if upper else list(resolved)

    @staticmethod
    def _check_identity(
        ctx: SetupContext,
        me: people.Person | None,
        account_id: str,
        display_name: str,
        open_items: list[str],
    ) -> bool:
        """Whether the Atlassian account should be added to me; says why not when it should not."""
        if me is None:
            open_items.append("About you: run `oil setup --only me`")
            return False
        if not account_id:
            return False
        with ctx.engine.connect() as conn:
            owner = people.find_person_by_identity(conn, "atlassian", account_id)
        if owner is None:
            return True
        if owner.id != me.id:
            ctx.console.say(
                f"The Atlassian account {display_name} belongs to {owner.display_name} — "
                "merge that person into you on the People page"
            )
        return False

    @staticmethod
    def _write(
        conn: sqlalchemy.Connection,
        access: _Access,
        row: registry.ConnectorRow | None,
        type_name: str,
        default_name: str,
        settings: dict[str, object],
    ) -> list[tuple[str, str]]:
        """Create or update one connector and audit it; returns the (reference, token) to store afterwards."""
        token = access.token
        assert token is not None
        if row is None:
            name = default_name
            ref: str | None = f"ai_oil/{name}"
            row = registry.create_connector(conn, type_name, name, settings=settings, credential_ref=ref)
            action, store = "connector.create", True
        else:
            ref = row.credential_ref or f"ai_oil/{row.name}"
            store = access.token_is_new or not secrets.get_secret(ref)
            if row.credential_ref is None:
                registry.set_credential_ref(conn, row.id, ref)
            row = registry.update_settings(conn, row.id, {**row.settings, **settings})
            action = "connector.update"
        registry.instantiate(row)
        audit.log(
            conn,
            action,
            "B",
            actor_type="user",
            target=row.name,
            detail={
                "type": type_name,
                "settings": row.settings,
                "credential": "stored" if store else "unchanged",
            },
        )
        return [(ref, token)] if store and ref is not None else []


class SharePointStage:
    name = "sharepoint"
    title = "SharePoint and OneDrive files"

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        libraries: dict[str, SyncedLibrary] = {}
        for library in ctx.probe.sharepoint_libraries():
            libraries.setdefault(library.url.lower(), library)
        found = list(libraries.values())
        if not found:
            return StageResult(
                "skipped",
                ["No synced SharePoint or OneDrive libraries found"],
                [f"SharePoint: {_SYNC_ACTION}"],
            )

        with ctx.engine.connect() as conn:
            rows = registry.list_connectors(conn, type_name="sharepoint")
        row = rows[0] if rows else None
        current = self._roots(row)
        configured = {str(root["url"]).lower() for root in current}

        options = [
            f"{library.name} — {library.url}" + ("" if Path(library.path).is_dir() else " (folder missing)")
            for library in found
        ]
        defaults = [index for index, library in enumerate(found) if library.url.lower() in configured]
        console.say(
            "Missing a library? Open it in the browser, click Sync, then run `oil setup --only sharepoint`."
        )
        while True:
            chosen = console.choose_many("Which libraries should AI_OIL read?", options, defaults=defaults)
            if not chosen:
                console.say("Select at least one library.")
                continue
            missing = [found[index] for index in chosen if not Path(found[index].path).is_dir()]
            if missing:
                for library in missing:
                    console.say(
                        f"The folder of {library.name} does not exist yet ({library.path}) — "
                        "let OneDrive finish syncing it, or leave it out."
                    )
                continue
            break

        roots = [{"url": found[index].url, "path": found[index].path} for index in chosen]
        roots += [root for root in current if str(root["url"]).lower() not in libraries]

        with ctx.engine.begin() as conn:
            if row is None:
                row = registry.create_connector(
                    conn, "sharepoint", _SHAREPOINT_CONNECTOR_NAME, settings={"sync_roots": roots}
                )
                action, credential = "connector.create", "none"
            else:
                row = registry.update_settings(conn, row.id, {**row.settings, "sync_roots": roots})
                action, credential = "connector.update", "unchanged"
            registry.instantiate(row)
            audit.log(
                conn,
                action,
                "B",
                actor_type="user",
                target=row.name,
                detail={"type": "sharepoint", "settings": row.settings, "credential": credential},
            )
        return StageResult("done", [f"Sync root: {root['url']} — {root['path']}" for root in roots])

    @staticmethod
    def _roots(row: registry.ConnectorRow | None) -> list[dict[str, str]]:
        """The well-formed sync roots of the existing connector."""
        value = row.settings.get("sync_roots") if row is not None else None
        if not isinstance(value, list):
            return []
        return [
            {"url": item["url"], "path": item["path"]}
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and isinstance(item.get("path"), str)
        ]


class TeamsStage:
    name = "teams"
    title = "Teams chats"

    def __init__(
        self, *, connector_factory: Callable[[ConnectorConfig], TeamsConnector] = TeamsConnector
    ) -> None:
        self._connector_factory = connector_factory

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        with ctx.engine.connect() as conn:
            rows = registry.list_connectors(conn, type_name="teams")
        row = rows[0] if rows else None
        found, default = self._candidates(ctx, row)
        if not found:
            return StageResult("skipped", ["No Teams bridge folder found"], [_TEAMS_OPEN_ITEM])

        if len(found) == 1:
            folder = _as_path(
                console.ask("Teams bridge folder", default=str(found[0]), validate=self._validate_folder)
            )
        else:
            folder = found[
                console.choose(
                    "Which folder holds the Teams bridge files?",
                    [str(item) for item in found],
                    default=default,
                )
            ]

        settings = {**(row.settings if row is not None else {}), "folder": str(folder)}
        name = row.name if row is not None else _TEAMS_CONNECTOR_NAME
        checks = (
            self._connector_factory(
                ConnectorConfig(row.id if row is not None else 0, "teams", name, True, settings, None)
            )
            .check()
            .checks
        )
        for check in checks:
            console.say(f"[{'ok' if check.ok else '!!'}] {check.name}: {check.detail}")
        file_checks = [check for check in checks if check.name.startswith("file ")]
        if not any(check.ok for check in file_checks):
            console.say(f"No Teams bridge file with a valid table was found in {folder}.")
            console.say("Section 1 of docs/teams_bridge_setup.md describes the Excel file and its table.")
            if not console.confirm("Create the connector anyway?", default=False):
                return StageResult("skipped", [], [_TEAMS_OPEN_ITEM])

        with ctx.engine.begin() as conn:
            if row is None:
                row = registry.create_connector(conn, "teams", name, settings=settings)
                action, credential = "connector.create", "none"
            else:
                row = registry.update_settings(conn, row.id, settings)
                action, credential = "connector.update", "unchanged"
            registry.instantiate(row)
            audit.log(
                conn,
                action,
                "B",
                actor_type="user",
                target=row.name,
                detail={"type": "teams", "settings": row.settings, "credential": credential},
            )
        newest = next(
            (check.detail for check in checks if check.name == "newest message"), "none captured yet"
        )
        count = len(file_checks)
        return StageResult(
            "done",
            [f"Teams bridge: {folder} — {count} file{'' if count == 1 else 's'}, newest message {newest}"],
        )

    @staticmethod
    def _validate_folder(text: str) -> str | None:
        if not text:
            return "Enter a folder."
        folder = _as_path(text)
        return None if folder.is_dir() else f"{folder} is not an existing folder."

    @staticmethod
    def _candidates(ctx: SetupContext, row: registry.ConnectorRow | None) -> tuple[list[Path], int]:
        """The existing `AI_OIL-Teams` folders worth offering, and the index of the configured one (or 0)."""
        folders: list[Path] = []
        documents = ctx.probe.documents_folder()
        if documents is not None:
            folders.append(documents / _TEAMS_FOLDER_NAME)
        for root in ctx.probe.onedrive_roots():
            folders += [root / _TEAMS_FOLDER_NAME, root / "Documents" / _TEAMS_FOLDER_NAME]
        configured = row.settings.get("folder") if row is not None else None
        if isinstance(configured, str) and configured.strip():
            folders.append(_as_path(configured))

        def key(folder: Path) -> str:
            return os.path.normcase(os.path.normpath(str(folder)))

        found: dict[str, Path] = {}
        for folder in folders:
            if folder.is_dir():
                found.setdefault(key(folder), folder)
        wanted = key(_as_path(configured)) if isinstance(configured, str) and configured.strip() else None
        return list(found.values()), list(found).index(wanted) if wanted in found else 0
