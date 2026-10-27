"""The wizard stages that talk to the outside world: Outlook and the network (installer concept, stages 4-5).

Everything here is read-only towards Outlook, Jira, Confluence and SharePoint; the network stage sends
nothing but plain GET requests and uses no credentials.
"""

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from ai_oil.connectors.atlassian import system_certificate_session
from ai_oil.connectors.outlook_session import ComOutlookSession, OutlookSession
from ai_oil.domain import people
from ai_oil.errors import NotFoundError
from ai_oil.governance import audit
from ai_oil.setup.console import SkipStage
from ai_oil.setup.model import SetupContext, StageResult
from ai_oil.setup.probe import InternetSettings
from ai_oil.setup.stages_basic import _as_path
from ai_oil.sources import registry

_OUTLOOK_CONNECTOR_NAME = "Outlook"
_DEFAULT_PROBE_URL = "https://api.atlassian.com/"
_DETAIL_LIMIT = 200
_DETAIL_CHECKS = (("mailbox", "Mailbox"), ("inbox", "Inbox"), ("sent", "Sent"))


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
