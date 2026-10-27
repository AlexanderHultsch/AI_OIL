from collections.abc import Callable
from pathlib import Path

import pytest
import requests

from ai_oil import secrets
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import people
from ai_oil.errors import ConfigError
from ai_oil.governance import audit
from ai_oil.setup import wizard
from ai_oil.setup.console import SkipStage
from ai_oil.setup.probe import InternetSettings, SyncedLibrary
from ai_oil.setup.stages_basic import DataStage
from ai_oil.setup.stages_connectors import (
    AtlassianStage,
    NetworkStage,
    OutlookStage,
    ProbeOutcome,
    SharePointStage,
    normalise_site,
    probe_https,
)
from ai_oil.setup.wizard import run_wizard
from ai_oil.sources import registry
from ai_oil.sources.model import HealthCheck

# --- helpers ---------------------------------------------------------------------------------------


def _engine(home: Path):
    return create_db_engine(load_config(home).db_path)


def _connectors(home: Path, type_name: str | None = None) -> list[registry.ConnectorRow]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            return registry.list_connectors(conn, type_name=type_name)
    finally:
        engine.dispose()


def _audit(home: Path) -> list[audit.AuditEntry]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            return audit.list_entries(conn, action_prefix="connector.")
    finally:
        engine.dispose()


def _add_connector(
    home: Path, type_name: str, name: str, settings: dict | None = None, credential_ref: str | None = None
) -> None:
    engine = _engine(home)
    try:
        with engine.begin() as conn:
            registry.create_connector(conn, type_name, name, settings=settings, credential_ref=credential_ref)
    finally:
        engine.dispose()


def _add_person(home: Path, name: str, address: str) -> None:
    engine = _engine(home)
    try:
        with engine.begin() as conn:
            person = people.create_person(conn, name)
            people.add_identity(conn, person.id, "email", address)
    finally:
        engine.dispose()


def _my_addresses(home: Path) -> list[str]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            me = people.get_me(conn)
            return [i.external_id for i in people.list_identities(conn, me.id) if i.system == "email"]
    finally:
        engine.dispose()


# --- outlook ---------------------------------------------------------------------------------------

GOOD_CHECKS = [
    HealthCheck("com", True, "classic Outlook reachable through COM"),
    HealthCheck("mailbox", True, "alex@corp.com"),
    HealthCheck("inbox", True, "Inbox"),
    HealthCheck("sent", True, "Sent Items"),
]
BAD_CHECKS = [HealthCheck("com", False, "classic Outlook is not available: Invalid class string")]


class FakeOutlookSession:
    def __init__(self, checks: list[HealthCheck]) -> None:
        self.checks = checks

    def check(self) -> list[HealthCheck]:
        return self.checks


class SessionFactory:
    """Hands out one session per call, each answering the next list of checks (the last one repeats)."""

    def __init__(self, *attempts: list[HealthCheck]) -> None:
        self.attempts = list(attempts) or [GOOD_CHECKS]
        self.sessions: list[FakeOutlookSession] = []

    def __call__(self) -> FakeOutlookSession:
        index = min(len(self.sessions), len(self.attempts) - 1)
        session = FakeOutlookSession(self.attempts[index])
        self.sessions.append(session)
        return session


def test_outlook_is_skipped_off_windows(make_ctx, probe, initialised_home: Path) -> None:
    probe.windows = False
    factory = SessionFactory()
    ctx = make_ctx(initialised_home, probe=probe)

    result = OutlookStage(session_factory=factory).run(ctx)

    assert result.status == "skipped"
    assert result.lines == ["Outlook is only available on Windows"]
    assert result.open_items == ["Outlook: not available on this computer"]
    assert factory.sessions == [] and ctx.console.prompts == []


def test_outlook_not_installed_is_skipped_with_an_open_item(make_ctx, probe, initialised_home: Path) -> None:
    probe.outlook = False
    factory = SessionFactory()

    result = OutlookStage(session_factory=factory).run(make_ctx(initialised_home, probe=probe))

    assert result.status == "skipped"
    assert result.lines == ["Classic Outlook is not installed"]
    assert result.open_items == [
        "Outlook: install classic Outlook (not the new Outlook), then run `oil setup --only outlook`"
    ]
    assert factory.sessions == [] and _connectors(initialised_home) == []


def test_outlook_new_outlook_is_explained_and_rechecked_until_it_is_off(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.new_outlook_script = [True, True, False]
    factory = SessionFactory()
    ctx = make_ctx(initialised_home, ["", ""], probe=probe)

    result = OutlookStage(session_factory=factory).run(ctx)

    assert ctx.console.prompts == ["Check again? [Y/n]: "] * 2
    assert ctx.console.output.count("It has no interface AI_OIL can use") == 2
    assert '"New Outlook" toggle at the top right' in ctx.console.output
    assert result.status == "done"
    assert len(factory.sessions) == 1


def test_outlook_new_outlook_declined_skips_the_stage_and_writes_nothing(
    make_ctx, probe, initialised_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe.new_outlook = True
    factory = SessionFactory()
    ctx = make_ctx(initialised_home, ["n"], probe=probe)

    with pytest.raises(SkipStage):
        OutlookStage(session_factory=factory).run(ctx)
    assert factory.sessions == [] and _connectors(initialised_home) == []

    monkeypatch.setattr(wizard, "STAGES", [OutlookStage(session_factory=factory)])
    ctx = make_ctx(initialised_home, ["1", "n"], probe=probe)  # the re-run menu, then the prompt
    assert run_wizard(ctx) == 0
    assert "Outlook  skipped" in ctx.console.output
    assert "- Outlook: skipped — run `oil setup --only outlook` later" in ctx.console.output


def test_outlook_com_failure_is_shown_and_retried_with_a_new_session(
    make_ctx, probe, initialised_home: Path
) -> None:
    factory = SessionFactory(BAD_CHECKS, GOOD_CHECKS)
    ctx = make_ctx(initialised_home, [""], probe=probe)

    result = OutlookStage(session_factory=factory).run(ctx)

    output = ctx.console.output
    assert "[!!] com: classic Outlook is not available: Invalid class string" in output
    assert "Start classic Outlook and sign in" in output
    assert "[ok] com: classic Outlook reachable through COM" in output
    assert "[ok] inbox: Inbox" in output
    assert ctx.console.prompts == ["Check again? [Y/n]: "]
    assert len(factory.sessions) == 2 and factory.sessions[0] is not factory.sessions[1]
    assert result.status == "done"


def test_outlook_com_failure_declined_skips_the_stage(make_ctx, probe, initialised_home: Path) -> None:
    ctx = make_ctx(initialised_home, ["n"], probe=probe)
    with pytest.raises(SkipStage):
        OutlookStage(session_factory=SessionFactory(BAD_CHECKS)).run(ctx)
    assert _connectors(initialised_home) == [] and _audit(initialised_home) == []


def test_outlook_creates_the_connector_once_and_audits_it(make_ctx, probe, initialised_home: Path) -> None:
    ctx = make_ctx(initialised_home, probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert result.status == "done"
    assert result.lines == [
        "Outlook: classic, reachable",
        "Connector: Outlook",
        "Mailbox: alex@corp.com",
        "Inbox: Inbox",
        "Sent: Sent Items",
    ]
    assert result.open_items == []
    (row,) = _connectors(initialised_home, "outlook")
    assert (row.name, row.settings, row.credential_ref, row.enabled) == ("Outlook", {}, None, True)
    (entry,) = _audit(initialised_home)
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "connector.create",
        "B",
        "user",
        "Outlook",
    )
    assert entry.detail == {"type": "outlook", "settings": {}, "credential": "none"}


def test_outlook_rerun_reuses_the_connector_without_a_new_audit_row(
    make_ctx, probe, initialised_home: Path
) -> None:
    OutlookStage(session_factory=SessionFactory()).run(make_ctx(initialised_home, probe=probe))
    ctx = make_ctx(initialised_home, probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert "Using the existing connector Outlook" in ctx.console.output
    assert "Connector: Outlook" in result.lines
    assert len(_connectors(initialised_home, "outlook")) == 1
    assert len(_audit(initialised_home)) == 1


def test_outlook_uses_an_existing_connector_with_another_name(
    make_ctx, probe, initialised_home: Path
) -> None:
    _add_connector(initialised_home, "outlook", "Mail")
    ctx = make_ctx(initialised_home, probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert "Using the existing connector Mail" in ctx.console.output
    assert "Connector: Mail" in result.lines
    assert [row.name for row in _connectors(initialised_home)] == ["Mail"]
    assert _audit(initialised_home) == []


def test_outlook_offers_a_new_account_address_and_adds_it(make_ctx, probe, initialised_home: Path) -> None:
    probe.accounts = ["alex@example.com", "alex.h@corp.com"]
    ctx = make_ctx(initialised_home, [""], probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert ctx.console.prompts == ["Add alex.h@corp.com to your e-mail addresses? [Y/n]: "]
    assert "Added e-mail address: alex.h@corp.com" in result.lines
    assert _my_addresses(initialised_home) == ["alex.h@corp.com", "alex@example.com"]


def test_outlook_declined_account_address_is_not_added(make_ctx, probe, initialised_home: Path) -> None:
    probe.accounts = ["alex.h@corp.com"]
    ctx = make_ctx(initialised_home, ["n"], probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert not [line for line in result.lines if line.startswith("Added")]
    assert _my_addresses(initialised_home) == ["alex@example.com"]


def test_outlook_address_of_another_person_is_reported_and_skipped(
    make_ctx, probe, initialised_home: Path
) -> None:
    _add_person(initialised_home, "Pat", "pat@corp.com")
    probe.accounts = ["pat@corp.com"]
    ctx = make_ctx(initialised_home, probe=probe)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert "pat@corp.com belongs to Pat — merge that person into you on the People page" in ctx.console.output
    assert ctx.console.prompts == []
    assert result.status == "done" and result.open_items == []
    assert _my_addresses(initialised_home) == ["alex@example.com"]


def test_outlook_without_a_me_person_leaves_an_open_item(make_ctx, probe, fresh_home: Path) -> None:
    probe.accounts = ["alex@corp.com"]
    ctx = make_ctx(fresh_home, probe=probe)
    DataStage().run(ctx)

    result = OutlookStage(session_factory=SessionFactory()).run(ctx)

    assert result.status == "done"
    assert result.open_items == ["About you: run `oil setup --only me`"]
    assert ctx.console.prompts == []
    assert [row.name for row in _connectors(fresh_home)] == ["Outlook"]


# --- network ---------------------------------------------------------------------------------------

DEFAULT_URL = "https://api.atlassian.com/"
Handler = Callable[[str, dict], "int | Exception"]


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """A requests.Session without a network: `handler(url, kwargs)` returns a status code or an error."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[FakeResponse] = []
        self.closed = False

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        outcome = self.handler(url, kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        response = FakeResponse(outcome)
        self.responses.append(response)
        return response

    def close(self) -> None:
        self.closed = True


class FakeNet:
    """System-store and plain session factories over two handlers; every session ever made is kept."""

    def __init__(self, system: Handler | int | Exception, plain: Handler | int | Exception) -> None:
        self.system_sessions: list[FakeSession] = []
        self.plain_sessions: list[FakeSession] = []
        self._system = self._handler(system)
        self._plain = self._handler(plain)

    @staticmethod
    def _handler(spec: Handler | int | Exception) -> Handler:
        return spec if callable(spec) else (lambda url, kwargs: spec)

    def _make(self, handler: Handler, sessions: list[FakeSession]) -> FakeSession:
        session = FakeSession(handler)
        sessions.append(session)
        return session

    def stage(self) -> NetworkStage:
        return NetworkStage(
            system_session=lambda: self._make(self._system, self.system_sessions),  # type: ignore[arg-type,return-value]
            plain_session=lambda: self._make(self._plain, self.plain_sessions),  # type: ignore[arg-type,return-value]
        )

    @property
    def calls(self) -> list[tuple[str, dict]]:
        return [call for session in self.system_sessions + self.plain_sessions for call in session.calls]


SSL_ERROR = requests.exceptions.SSLError("certificate verify failed: self-signed certificate in chain")
CONNECT_ERROR = requests.exceptions.ConnectionError("Failed to establish a new connection")
PROXY_ERROR = requests.exceptions.ProxyError("Cannot connect to proxy")
PROXY_URL = "http://proxy.corp:8080"
PAC = InternetSettings(auto_config_url="http://wpad.corp/wpad.dat")


def _only_via(proxy: str, then: "int | Exception") -> Handler:
    """Unreachable directly, `then` through `proxy`."""

    def handler(url: str, kwargs: dict) -> "int | Exception":
        proxies = kwargs.get("proxies")
        return then if proxies == {"https": proxy, "http": proxy} else CONNECT_ERROR

    return handler


def _bundle(tmp_path: Path) -> Path:
    path = tmp_path / "corp-ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    return path


def _trusting(bundle: Path) -> Handler:
    """SSL error unless the corporate bundle is the verification source."""

    def handler(url: str, kwargs: dict) -> "int | Exception":
        return 200 if kwargs.get("verify") == str(bundle) else SSL_ERROR

    return handler


def test_probe_https_reports_any_http_answer_as_ok_and_closes_the_response() -> None:
    session = FakeSession(lambda url, kwargs: 403)

    outcome = probe_https(session, DEFAULT_URL, proxies={"https": PROXY_URL}, timeout=3.0)  # type: ignore[arg-type]

    assert outcome == ProbeOutcome("ok", "HTTP 403")
    assert session.calls == [
        (
            DEFAULT_URL,
            {"allow_redirects": False, "timeout": 3.0, "verify": True, "proxies": {"https": PROXY_URL}},
        )
    ]
    assert [r.closed for r in session.responses] == [True]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (requests.exceptions.SSLError("bad certificate"), "ssl"),
        (requests.exceptions.ProxyError("no proxy"), "proxy"),
        (requests.exceptions.ConnectionError("refused"), "connect"),
        (requests.exceptions.ConnectTimeout("too slow"), "connect"),
        (requests.exceptions.ReadTimeout("too slow"), "connect"),
        (requests.exceptions.TooManyRedirects("loop"), "other"),
        (requests.exceptions.InvalidURL("bad"), "other"),
    ],
)
def test_probe_https_maps_each_exception_kind(error: Exception, kind: str) -> None:
    outcome = probe_https(FakeSession(lambda url, kwargs: error), DEFAULT_URL)  # type: ignore[arg-type]
    assert outcome.kind == kind
    assert str(error) in outcome.detail


def test_probe_https_shortens_long_details() -> None:
    error = requests.exceptions.ConnectionError("x " * 500)
    detail = probe_https(FakeSession(lambda url, kwargs: error), DEFAULT_URL).detail  # type: ignore[arg-type]
    assert len(detail) == 200 and detail.endswith("...")


def test_network_without_inspection(make_ctx, probe, initialised_home: Path) -> None:
    net = FakeNet(200, 200)
    ctx = make_ctx(initialised_home, probe=probe)

    result = net.stage().run(ctx)

    assert result.status == "done"
    assert result.lines == ["No SSL inspection"] and result.open_items == []
    assert f"Checking the connection to {DEFAULT_URL}" in ctx.console.output
    assert "No proxy is configured." in ctx.console.output
    assert [url for url, _ in net.calls] == [DEFAULT_URL, DEFAULT_URL]
    assert len(net.system_sessions) == 1 and len(net.plain_sessions) == 1
    assert all(s.closed for s in net.system_sessions + net.plain_sessions)
    assert ctx.console.prompts == [] and probe.user_env == {}


def test_network_inspection_handled_by_the_windows_store(make_ctx, probe, initialised_home: Path) -> None:
    net = FakeNet(404, SSL_ERROR)  # a 404 is still an answer

    result = net.stage().run(make_ctx(initialised_home, probe=probe))

    assert result.status == "done"
    assert result.lines == [
        "SSL inspection detected — handled by the Windows certificate store, nothing to do"
    ]
    assert result.open_items == []


def test_network_inspection_not_trusted_asks_for_a_bundle_and_reasks_with_the_reason(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    wrong = tmp_path / "wrong.pem"
    wrong.write_text("nope", encoding="utf-8")
    net = FakeNet(SSL_ERROR, _trusting(bundle))
    ctx = make_ctx(
        initialised_home, [str(tmp_path / "missing.pem"), "", str(wrong), f'"{bundle}"'], probe=probe
    )

    result = net.stage().run(ctx)

    output = ctx.console.output
    assert "does not trust" in output and "Ask IT" in output
    assert f"{tmp_path / 'missing.pem'} is not a file." in output
    assert "Enter the path of the CA bundle file." in output
    assert f"The bundle does not work for {DEFAULT_URL}: certificate verify failed" in output
    assert result.status == "done"
    assert result.lines == [f"CA bundle: {bundle}"]
    assert ctx.state["ca_bundle"] == str(bundle)
    validations = [kwargs["verify"] for url, kwargs in net.calls if kwargs["verify"] is not True]
    assert validations == [str(wrong), str(bundle)]


def test_network_bundle_is_applied_to_the_chosen_atlassian_connectors_and_audited(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    _add_connector(
        initialised_home,
        "jira",
        "Jira",
        {"base_url": "https://acme.atlassian.net", "email": "alex@corp.com"},
    )
    _add_connector(
        initialised_home,
        "confluence",
        "Wiki",
        {"base_url": "https://acme.atlassian.net/wiki", "email": "alex@corp.com"},
    )
    bundle = _bundle(tmp_path)
    net = FakeNet(SSL_ERROR, _trusting(bundle))
    ctx = make_ctx(initialised_home, [str(bundle), "", "n"], probe=probe)

    result = net.stage().run(ctx)

    assert ctx.console.prompts[1:] == ["Use it for Jira? [Y/n]: ", "Use it for Wiki? [Y/n]: "]
    assert "Checking the connection to https://acme.atlassian.net/" in ctx.console.output
    assert {url for url, _ in net.calls} == {"https://acme.atlassian.net/"}
    assert result.lines == [f"CA bundle: {bundle}", "CA bundle set for Jira"]
    jira = _connectors(initialised_home, "jira")[0]
    wiki = _connectors(initialised_home, "confluence")[0]
    assert jira.settings == {
        "base_url": "https://acme.atlassian.net",
        "email": "alex@corp.com",
        "ca_bundle": str(bundle),
    }
    assert wiki.settings == {"base_url": "https://acme.atlassian.net/wiki", "email": "alex@corp.com"}
    (entry,) = [e for e in _audit(initialised_home) if e.action == "connector.update"]
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", "Jira")
    assert entry.detail == {"type": "jira", "settings": jira.settings, "credential": "unchanged"}


def test_network_target_url_comes_from_an_existing_confluence_connector(
    make_ctx, probe, initialised_home: Path
) -> None:
    _add_connector(
        initialised_home,
        "confluence",
        "Wiki",
        {"base_url": "https://acme.atlassian.net/wiki", "email": "alex@corp.com"},
    )
    net = FakeNet(200, 200)

    net.stage().run(make_ctx(initialised_home, probe=probe))

    assert {url for url, _ in net.calls} == {"https://acme.atlassian.net/"}


def test_network_shows_a_static_and_an_environment_proxy_as_used_automatically(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.internet = InternetSettings(proxy_server="proxy.corp:8080", env_proxy="http://env.corp:3128")
    ctx = make_ctx(initialised_home, probe=probe)

    FakeNet(200, 200).stage().run(ctx)

    assert "Windows proxy proxy.corp:8080 is used automatically." in ctx.console.output
    assert "HTTPS_PROXY http://env.corp:3128 is used automatically." in ctx.console.output


def test_network_pac_script_is_named_as_unreadable_for_python(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.internet = PAC
    ctx = make_ctx(initialised_home, probe=probe)

    FakeNet(200, 200).stage().run(ctx)

    assert "uses an automatic configuration script" in ctx.console.output
    assert "which Python cannot read" in ctx.console.output
    assert "No proxy is configured." not in ctx.console.output


def test_network_pac_asks_for_a_proxy_validates_it_and_stores_it_for_the_user(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.internet = PAC
    net = FakeNet(_only_via(PROXY_URL, 200), _only_via(PROXY_URL, 200))
    answers = ["proxy.corp:8080", "http://proxy.corp", "http://dead.corp:8080", PROXY_URL, ""]
    ctx = make_ctx(initialised_home, answers, probe=probe)

    result = net.stage().run(ctx)

    output = ctx.console.output
    assert output.count("Enter the address as http://proxy.company.com:8080.") == 1
    assert "The address needs a port, like http://proxy.company.com:8080." in output
    assert "http://dead.corp:8080 did not work: Failed to establish a new connection" in output
    assert (
        ctx.console.prompts[-1] == "Store HTTPS_PROXY for your Windows user so AI_OIL always uses it? [Y/n]: "
    )
    assert probe.user_env == {"HTTPS_PROXY": PROXY_URL}
    assert result.status == "done"
    assert result.lines == [
        f"Proxy: {PROXY_URL}",
        "No SSL inspection",
        "HTTPS_PROXY stored for your Windows user",
    ]
    assert result.open_items == []


def test_network_proxy_not_stored_becomes_an_open_item(make_ctx, probe, initialised_home: Path) -> None:
    net = FakeNet(_only_via(PROXY_URL, 200), _only_via(PROXY_URL, 200))  # no proxy configured at all
    ctx = make_ctx(initialised_home, [PROXY_URL, "n"], probe=probe)

    result = net.stage().run(ctx)

    assert result.status == "done"
    assert result.open_items == [f"Network: set HTTPS_PROXY={PROXY_URL} for AI_OIL"]
    assert probe.user_env == {}


def test_network_proxy_that_ends_in_an_ssl_error_continues_with_the_bundle_through_the_proxy(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    probe.internet = PAC

    def trusting_via_proxy(url: str, kwargs: dict) -> "int | Exception":
        if kwargs.get("proxies") is None:
            return CONNECT_ERROR
        return 200 if kwargs.get("verify") == str(bundle) else SSL_ERROR

    net = FakeNet(_only_via(PROXY_URL, SSL_ERROR), trusting_via_proxy)
    ctx = make_ctx(initialised_home, [PROXY_URL, "y", str(bundle)], probe=probe)

    result = net.stage().run(ctx)

    assert result.lines == [
        f"Proxy: {PROXY_URL}",
        f"CA bundle: {bundle}",
        "HTTPS_PROXY stored for your Windows user",
    ]
    assert ctx.state["ca_bundle"] == str(bundle)
    bundle_probe = [kwargs for _, kwargs in net.calls if kwargs["verify"] == str(bundle)]
    assert [kwargs["proxies"] for kwargs in bundle_probe] == [{"https": PROXY_URL, "http": PROXY_URL}]
    assert probe.user_env == {"HTTPS_PROXY": PROXY_URL}


def test_network_skip_after_the_proxy_question_stores_nothing(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.internet = PAC
    net = FakeNet(_only_via(PROXY_URL, SSL_ERROR), _only_via(PROXY_URL, SSL_ERROR))
    ctx = make_ctx(initialised_home, [PROXY_URL, "y", "s"], probe=probe)

    with pytest.raises(SkipStage):
        net.stage().run(ctx)

    assert probe.user_env == {}


def test_network_skip_in_the_proxy_question(make_ctx, probe, initialised_home: Path) -> None:
    ctx = make_ctx(initialised_home, ["s"], probe=probe)
    with pytest.raises(SkipStage):
        FakeNet(CONNECT_ERROR, CONNECT_ERROR).stage().run(ctx)


@pytest.mark.parametrize(
    "internet",
    [
        InternetSettings(proxy_server="proxy.corp:8080"),
        InternetSettings(env_proxy="http://env.corp:3128"),
    ],
)
def test_network_unreachable_with_a_proxy_in_place_fails_with_the_reason(
    make_ctx, probe, initialised_home: Path, internet: InternetSettings
) -> None:
    probe.internet = internet
    ctx = make_ctx(initialised_home, probe=probe)

    result = FakeNet(PROXY_ERROR, PROXY_ERROR).stage().run(ctx)

    assert result.status == "failed"
    assert result.lines == ["Cannot connect to proxy"]
    assert result.open_items == [
        f"Network: {DEFAULT_URL} is not reachable (Cannot connect to proxy) — check the VPN or ask IT, "
        "then run `oil setup --only network`"
    ]
    assert ctx.console.prompts == []


def test_network_a_timeout_is_a_connection_failure(make_ctx, probe, initialised_home: Path) -> None:
    probe.internet = InternetSettings(proxy_server="proxy.corp:8080")
    timeout = requests.exceptions.ConnectTimeout("timed out after 15 s")

    result = FakeNet(timeout, timeout).stage().run(make_ctx(initialised_home, probe=probe))

    assert result.status == "failed" and result.lines == ["timed out after 15 s"]


def test_network_on_an_uninitialised_folder_fails_with_the_init_hint(make_ctx, fresh_home: Path) -> None:
    with pytest.raises(ConfigError, match="oil init"):
        FakeNet(200, 200).stage().run(make_ctx(fresh_home))


# --- jira and confluence ---------------------------------------------------------------------------

SITE = "https://acme.atlassian.net"
WIKI = f"{SITE}/wiki"
TOKEN = "atl-tok-4f9c2e7a1b"
WRONG_TOKEN = "atl-tok-WRONG-0000"
ME = {"accountId": "acc-alex-1", "displayName": "Alex Hultsch"}
PROJECTS = [
    {"key": "ABC", "name": "Gen6 DCDC"},
    {"key": "MB32", "name": "MB32829"},
    {"key": "ZED", "name": ""},
]
SPACES = [{"key": "DCDC", "name": "Gen6 DCDC"}, {"key": "Eng", "name": "Engineering"}]


class AtlResponse:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = text
        self.url = ""
        self._payload = payload

    def json(self) -> object:
        return self._payload


def ok(payload: object) -> AtlResponse:
    return AtlResponse(200, payload)


class FakeAtlassianSession:
    """A requests.Session without a network: one canned answer (or a callable, or a sequence) per URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs) -> AtlResponse:
        self.calls.append((url, kwargs))
        route = self.routes.get(url)
        if route is None:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            return route(kwargs.get("params"), kwargs["auth"])
        return route

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def _only_token(token: str, email: str | None = None, payload: object = ME) -> Callable[..., AtlResponse]:
    def route(params: dict | None, auth: tuple) -> AtlResponse:
        good = auth[1] == token and (email is None or auth[0] == email)
        return ok(payload) if good else AtlResponse(401)

    return route


def _routes(
    projects: list | None = None, spaces: list | None = None, **overrides: object
) -> dict[str, object]:
    routes: dict[str, object] = {
        f"{SITE}/rest/api/3/myself": ok(ME),
        f"{SITE}/rest/api/3/project/search": ok(
            {"values": PROJECTS if projects is None else projects, "isLast": True}
        ),
        f"{WIKI}/rest/api/user/current": ok({"accountId": ME["accountId"]}),
        f"{WIKI}/rest/api/space": ok({"results": SPACES if spaces is None else spaces}),
    }
    routes.update(
        {key.replace("SITE", SITE).replace("WIKI", WIKI): value for key, value in overrides.items()}
    )
    return routes


def _atlassian(make_ctx, home: Path, answers, tokens=(), *, routes=None, probe=None, state=None):
    """(context, stage, session) for one run of the Atlassian stage."""
    session = FakeAtlassianSession(routes if routes is not None else _routes())
    ctx = make_ctx(home, answers, probe=probe) if probe is not None else make_ctx(home, answers)
    ctx.console.secrets.extend(tokens)
    ctx.state.update(state or {})
    return ctx, AtlassianStage(session_factory=lambda: session), session  # type: ignore[arg-type,return-value]


def _audit_in_order(home: Path) -> list[audit.AuditEntry]:
    return list(reversed(_audit(home)))


def _identities(home: Path, system: str = "atlassian") -> list[str]:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            me = people.get_me(conn)
            return [i.external_id for i in people.list_identities(conn, me.id) if i.system == system]
    finally:
        engine.dispose()


def _everything_audited(home: Path) -> str:
    engine = _engine(home)
    try:
        with engine.connect() as conn:
            return repr(audit.list_entries(conn, limit=1000))
    finally:
        engine.dispose()


FRESH_ANSWERS = [f"{SITE}/jira/software/projects/ABC/boards/1?selectedIssue=ABC-4", "", "1,2", "", "2"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://acme.atlassian.net/browse/ABC-123", SITE),
        ("https://acme.atlassian.net/jira/software/c/projects/ABC/boards/4?selectedIssue=ABC-1", SITE),
        ("acme.atlassian.net", SITE),
        ("  ACME.atlassian.net/  ", SITE),
        ("acme.atlassian.net:443/browse/ABC-1", SITE),
        ("http://acme.atlassian.net/jira/your-work", SITE),
        ('"https://jira.corp.example/secure/Dashboard.jspa"', "https://jira.corp.example"),
        ("", None),
        ("acme", None),
        ("https://localhost/browse/ABC-1", None),
        ("not a link", None),
        ("ftp://acme.atlassian.net", None),
        ("https://", None),
    ],
)
def test_normalise_site(text: str, expected: str | None) -> None:
    assert normalise_site(text) == expected


def test_atlassian_fresh_setup_end_to_end(make_ctx, initialised_home: Path) -> None:
    ctx, stage, session = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS, [TOKEN])

    result = stage.run(ctx)

    assert result.status == "done" and result.open_items == []
    assert result.lines == [
        f"Jira: {SITE} — projects ABC, MB32",
        f"Confluence: {WIKI} — spaces Eng",
        "Atlassian account: Alex Hultsch",
    ]
    assert ctx.console.prompts[:3] == [
        "Paste any Jira link, or your site address: ",
        "Your Atlassian e-mail address [alex@example.com]: ",
        "API token (input hidden): ",
    ]
    assert "https://id.atlassian.com/manage-profile/security/api-tokens" in ctx.console.output
    assert f"Jira site: {SITE}" in ctx.console.output
    assert "ABC  Gen6 DCDC" in ctx.console.output and "Eng  Engineering" in ctx.console.output
    assert "Also set up Confluence on https://acme.atlassian.net/wiki with the same account? [Y/n]: " in (
        ctx.console.prompts
    )

    jira, wiki = _connectors(initialised_home, "jira")[0], _connectors(initialised_home, "confluence")[0]
    assert (jira.name, jira.credential_ref, jira.enabled) == ("Jira", "ai_oil/Jira", True)
    assert jira.settings == {"base_url": SITE, "email": "alex@example.com", "projects": ["ABC", "MB32"]}
    assert (wiki.name, wiki.credential_ref) == ("Confluence", "ai_oil/Confluence")
    assert wiki.settings == {"base_url": WIKI, "email": "alex@example.com", "spaces": ["Eng"]}
    assert secrets.get_secret("ai_oil/Jira") == TOKEN and secrets.get_secret("ai_oil/Confluence") == TOKEN

    entries = _audit_in_order(initialised_home)
    assert [(e.action, e.action_class, e.actor_type, e.target) for e in entries] == [
        ("connector.create", "B", "user", "Jira"),
        ("connector.create", "B", "user", "Confluence"),
    ]
    assert entries[0].detail == {"type": "jira", "settings": jira.settings, "credential": "stored"}
    assert entries[1].detail == {"type": "confluence", "settings": wiki.settings, "credential": "stored"}
    assert _identities(initialised_home) == [ME["accountId"]]

    assert set(session.urls()) == {
        f"{SITE}/rest/api/3/myself",
        f"{SITE}/rest/api/3/project/search",
        f"{WIKI}/rest/api/user/current",
        f"{WIKI}/rest/api/space",
    }
    assert all(call[1]["auth"] == ("alex@example.com", TOKEN) for call in session.calls)
    space_call = next(kwargs for url, kwargs in session.calls if url.endswith("/rest/api/space"))
    assert space_call["params"] == {"type": "global", "status": "current", "start": 0, "limit": 100}


def test_atlassian_invalid_site_and_email_are_reasked_with_the_reason(
    make_ctx, initialised_home: Path
) -> None:
    answers = ["acme", SITE, "nomail", "alex@corp.com", "1", "n"]
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, answers, [TOKEN])

    stage.run(ctx)

    assert "Enter a Jira link or the address of your site, like acme.atlassian.net." in ctx.console.output
    assert "The e-mail address needs an @." in ctx.console.output
    assert _connectors(initialised_home, "jira")[0].settings["email"] == "alex@corp.com"


def test_atlassian_a_rejected_token_is_explained_and_asked_again(make_ctx, initialised_home: Path) -> None:
    routes = _routes(**{"SITE/rest/api/3/myself": _only_token(TOKEN)})
    ctx, stage, session = _atlassian(
        make_ctx,
        initialised_home,
        [*FRESH_ANSWERS[:2], "", *FRESH_ANSWERS[2:]],
        [WRONG_TOKEN, TOKEN],
        routes=routes,
    )

    result = stage.run(ctx)

    assert result.status == "done"
    assert "authentication failed (401) — check the e-mail address and API token" in ctx.console.output
    assert "Is alex@example.com right? [Y/n]: " in ctx.console.prompts
    assert ctx.console.prompts.count("API token (input hidden): ") == 2
    assert secrets.get_secret("ai_oil/Jira") == TOKEN
    assert [call[1]["auth"][1] for call in session.calls[:2]] == [WRONG_TOKEN, TOKEN]


def test_atlassian_a_wrong_email_can_be_entered_again(make_ctx, initialised_home: Path) -> None:
    routes = _routes(**{"SITE/rest/api/3/myself": _only_token(TOKEN, "alex@corp.com")})
    answers = [FRESH_ANSWERS[0], "", "n", "nomail", "alex@corp.com", "1,2", "n"]
    ctx, stage, session = _atlassian(make_ctx, initialised_home, answers, [TOKEN, TOKEN], routes=routes)

    result = stage.run(ctx)

    assert result.status == "done"
    assert "Is alex@example.com right? [Y/n]: " in ctx.console.prompts
    assert "Your Atlassian e-mail address [alex@example.com]: " in ctx.console.prompts
    assert _connectors(initialised_home, "jira")[0].settings["email"] == "alex@corp.com"
    assert session.calls[-1][1]["auth"] == ("alex@corp.com", TOKEN)


def test_atlassian_an_empty_token_is_asked_again(make_ctx, initialised_home: Path) -> None:
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, [*FRESH_ANSWERS[:2], "1", "n"], ["", "  ", TOKEN])

    stage.run(ctx)

    assert ctx.console.output.count("The API token must not be empty.") == 2
    assert secrets.get_secret("ai_oil/Jira") == TOKEN


def test_atlassian_skip_at_the_token_prompt_writes_nothing(make_ctx, initialised_home: Path) -> None:
    ctx, stage, session = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS[:2], ["s"])

    with pytest.raises(SkipStage):
        stage.run(ctx)

    assert session.calls == []
    assert _connectors(initialised_home) == [] and _audit_in_order(initialised_home) == []
    assert secrets.get_secret("ai_oil/Jira") is None
    assert _identities(initialised_home) == []


def test_atlassian_any_other_error_fails_the_stage_with_the_message(
    make_ctx, initialised_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = _routes(**{"SITE/rest/api/3/myself": AtlResponse(500, text="Service unavailable")})
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS[:2], [TOKEN], routes=routes)

    with pytest.raises(ConfigError, match="HTTP 500 for /rest/api/3/myself: Service unavailable"):
        stage.run(ctx)
    assert _connectors(initialised_home) == []

    monkeypatch.setattr(wizard, "STAGES", [stage])
    ctx = make_ctx(initialised_home, ["1", *FRESH_ANSWERS[:2]])  # the re-run menu first
    ctx.console.secrets.append(TOKEN)
    assert run_wizard(ctx) == 1
    assert "Failed: HTTP 500 for /rest/api/3/myself" in ctx.console.output
    assert "Jira and Confluence  failed" in ctx.console.output
    assert "oil setup --only atlassian" in ctx.console.output


def test_atlassian_the_token_is_never_shown_stored_in_a_file_or_put_in_an_error(
    make_ctx, initialised_home: Path
) -> None:
    echo = AtlResponse(500, text=f"upstream said {TOKEN}")
    routes = _routes(**{"SITE/rest/api/3/myself": [AtlResponse(401), echo]})
    ctx, stage, _ = _atlassian(
        make_ctx, initialised_home, [*FRESH_ANSWERS[:2], ""], [WRONG_TOKEN, TOKEN], routes=routes
    )
    with pytest.raises(ConfigError) as excinfo:
        stage.run(ctx)
    assert TOKEN not in str(excinfo.value) and "***" in str(excinfo.value)
    assert TOKEN not in ctx.console.output

    routes = _routes(**{"SITE/rest/api/3/myself": _only_token(TOKEN)})
    answers = [*FRESH_ANSWERS[:2], "", *FRESH_ANSWERS[2:]]
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, answers, [WRONG_TOKEN, TOKEN], routes=routes)
    stage.run(ctx)

    shown = ctx.console.output + "".join(ctx.console.prompts)
    stored = _everything_audited(initialised_home) + repr(_connectors(initialised_home))
    for secret in (TOKEN, WRONG_TOKEN):
        assert secret not in shown
        assert secret not in stored
    assert "connector.create" in stored
    assert secrets.get_secret("ai_oil/Jira") == TOKEN


def test_atlassian_keeping_the_stored_token_stores_nothing_again(
    make_ctx, initialised_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS, [TOKEN])
    stage.run(ctx)

    def refuse(ref: str, value: str) -> None:
        raise AssertionError("the token must not be stored again")

    monkeypatch.setattr(secrets, "set_secret", refuse)
    ctx, stage, session = _atlassian(make_ctx, initialised_home, ["", "", "", "", "", ""])

    result = stage.run(ctx)

    assert result.status == "done"
    assert "Keep the stored API token? [Y/n]: " in ctx.console.prompts
    assert "API token (input hidden): " not in ctx.console.prompts
    assert all(call[1]["auth"][1] == TOKEN for call in session.calls)
    updates = [e for e in _audit_in_order(initialised_home) if e.action == "connector.update"]
    assert [(e.target, e.detail["credential"]) for e in updates] == [
        ("Jira", "unchanged"),
        ("Confluence", "unchanged"),
    ]
    assert _identities(initialised_home) == [ME["accountId"]]


def test_atlassian_a_stored_token_that_is_rejected_is_replaced(make_ctx, initialised_home: Path) -> None:
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS, [WRONG_TOKEN])
    stage.run(ctx)
    routes = _routes(**{"SITE/rest/api/3/myself": _only_token(TOKEN)})
    ctx, stage, _ = _atlassian(
        make_ctx, initialised_home, ["", "", "", "", "", "", ""], [TOKEN], routes=routes
    )

    stage.run(ctx)

    assert secrets.get_secret("ai_oil/Jira") == TOKEN and secrets.get_secret("ai_oil/Confluence") == TOKEN
    updates = [e for e in _audit_in_order(initialised_home) if e.action == "connector.update"]
    assert [e.detail["credential"] for e in updates] == ["stored", "stored"]


def test_atlassian_rerun_reuses_the_connector_names_other_settings_and_defaults(
    make_ctx, initialised_home: Path
) -> None:
    _add_connector(
        initialised_home,
        "jira",
        "Tracker",
        {
            "base_url": SITE,
            "email": "alex@corp.com",
            "projects": ["MB32"],
            "timeout": 10,
            "ca_bundle": "X:/ca.pem",
        },
        "ai_oil/Tracker",
    )
    _add_connector(
        initialised_home,
        "confluence",
        "Wiki",
        {"base_url": WIKI, "email": "alex@corp.com", "spaces": ["DCDC"]},
        "ai_oil/Wiki",
    )
    secrets.set_secret("ai_oil/Tracker", TOKEN)
    secrets.set_secret("ai_oil/Wiki", TOKEN)
    ctx, stage, session = _atlassian(make_ctx, initialised_home, ["", "", "", "", "", ""])

    result = stage.run(ctx)

    prompts = ctx.console.prompts
    assert prompts[0] == f"Paste any Jira link, or your site address [{SITE}]: "
    assert prompts[1] == "Your Atlassian e-mail address [alex@corp.com]: "
    assert (
        "Numbers, comma-separated, a = all [2]: " in prompts
        and "Numbers, comma-separated, a = all [1]: " in prompts
    )
    assert result.lines[0] == f"Jira: {SITE} — projects MB32"
    assert [row.name for row in _connectors(initialised_home)] == ["Tracker", "Wiki"]
    tracker = _connectors(initialised_home, "jira")[0]
    assert tracker.settings == {
        "base_url": SITE,
        "email": "alex@corp.com",
        "projects": ["MB32"],
        "timeout": 10,
        "ca_bundle": "X:/ca.pem",
    }
    assert _connectors(initialised_home, "confluence")[0].settings["spaces"] == ["DCDC"]
    assert all(call[1]["verify"] == "X:/ca.pem" for call in session.calls)
    assert [(e.action, e.target) for e in _audit_in_order(initialised_home)] == [
        ("connector.update", "Tracker"),
        ("connector.update", "Wiki"),
    ]


def test_atlassian_with_a_kept_token_a_new_confluence_connector_gets_its_own_copy(
    make_ctx, initialised_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, [*FRESH_ANSWERS[:3], "n"], [TOKEN])
    stage.run(ctx)
    stored: list[str] = []
    real = secrets.set_secret
    monkeypatch.setattr(secrets, "set_secret", lambda ref, value: (stored.append(ref), real(ref, value)))
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, ["", "", "", "", "", "2"])

    stage.run(ctx)

    assert stored == ["ai_oil/Confluence"]
    assert secrets.get_secret("ai_oil/Confluence") == TOKEN
    created = [e for e in _audit_in_order(initialised_home) if e.action == "connector.create"]
    assert [(e.target, e.detail["credential"]) for e in created] == [
        ("Jira", "stored"),
        ("Confluence", "stored"),
    ]


def test_atlassian_with_many_projects_and_spaces_the_keys_are_typed_and_checked(
    make_ctx, initialised_home: Path
) -> None:
    projects = [{"key": f"P{i}", "name": f"Project {i}"} for i in range(120)]
    spaces = [{"key": f"Sp{i}", "name": f"Space {i}"} for i in range(105)]

    def project_pages(params: dict | None, auth: tuple) -> AtlResponse:
        assert params is not None and params["maxResults"] == 100
        start = params["startAt"]
        return ok({"values": projects[start : start + 100], "total": len(projects)})

    def space_pages(params: dict | None, auth: tuple) -> AtlResponse:
        assert params is not None
        return ok({"results": spaces[params["start"] : params["start"] + 100]})

    routes = _routes(**{"SITE/rest/api/3/project/search": project_pages, "WIKI/rest/api/space": space_pages})
    answers = [FRESH_ANSWERS[0], "", "ZZZ", "", "p3, P101 p3", "", "SP7, nope", "sp7, SP104"]
    ctx, stage, session = _atlassian(make_ctx, initialised_home, answers, [TOKEN], routes=routes)

    result = stage.run(ctx)

    output = ctx.console.output
    assert "You can see 120 projects; too many to list." in output
    assert "ZZZ is not one of your projects." in output and "Enter at least one key." in output
    assert "You can see 105 spaces; too many to list." in output
    assert "nope is not one of your spaces." in output
    assert result.lines[:2] == [
        f"Jira: {SITE} — projects P3, P101",
        f"Confluence: {WIKI} — spaces Sp7, Sp104",
    ]
    space_starts = [kwargs["params"]["start"] for url, kwargs in session.calls if url.endswith("/space")]
    assert space_starts == [0, 100]
    assert [kwargs["params"]["startAt"] for url, kwargs in session.calls if url.endswith("/search")] == [
        0,
        100,
    ]


def test_atlassian_typed_keys_default_to_the_configured_ones(make_ctx, initialised_home: Path) -> None:
    projects = [{"key": f"P{i}", "name": ""} for i in range(31)]
    _add_connector(
        initialised_home,
        "jira",
        "Jira",
        {"base_url": SITE, "email": "alex@corp.com", "projects": ["P2", "P5"]},
    )
    ctx, stage, _ = _atlassian(
        make_ctx, initialised_home, ["", "", "", "n"], [TOKEN], routes=_routes(projects=projects)
    )

    result = stage.run(ctx)

    assert "Jira projects to read, keys separated by commas [P2, P5]: " in ctx.console.prompts
    assert result.lines[0] == f"Jira: {SITE} — projects P2, P5"


def test_atlassian_at_least_one_project_is_required(make_ctx, initialised_home: Path) -> None:
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, [*FRESH_ANSWERS[:2], "", "3", "n"], [TOKEN])

    result = stage.run(ctx)

    assert "Select at least one." in ctx.console.output
    assert result.lines[0] == f"Jira: {SITE} — projects ZED"
    assert "ZED" in ctx.console.output


def test_atlassian_an_account_without_projects_fails_the_stage(make_ctx, initialised_home: Path) -> None:
    ctx, stage, _ = _atlassian(
        make_ctx, initialised_home, FRESH_ANSWERS[:2], [TOKEN], routes=_routes(projects=[])
    )
    with pytest.raises(ConfigError, match="this account sees no projects"):
        stage.run(ctx)
    assert _connectors(initialised_home) == []


def test_atlassian_without_confluence_only_jira_is_configured(make_ctx, initialised_home: Path) -> None:
    ctx, stage, session = _atlassian(make_ctx, initialised_home, [*FRESH_ANSWERS[:3], "n"], [TOKEN])

    result = stage.run(ctx)

    assert result.lines == [f"Jira: {SITE} — projects ABC, MB32", "Atlassian account: Alex Hultsch"]
    assert [row.type for row in _connectors(initialised_home)] == ["jira"]
    assert [e.target for e in _audit_in_order(initialised_home)] == ["Jira"]
    assert not [url for url in session.urls() if "/wiki" in url]


def test_atlassian_a_confluence_rejection_can_leave_confluence_out_and_still_writes_jira(
    make_ctx, initialised_home: Path
) -> None:
    routes = _routes(**{"WIKI/rest/api/user/current": AtlResponse(403)})
    answers = [*FRESH_ANSWERS[:3], "", ""]
    ctx, stage, session = _atlassian(make_ctx, initialised_home, answers, [TOKEN], routes=routes)

    result = stage.run(ctx)

    assert result.status == "done"
    assert "authentication failed (403)" in ctx.console.output
    assert ctx.console.prompts[-1] == "Leave Confluence out for now? [Y/n]: "
    assert ctx.console.prompts.count("API token (input hidden): ") == 1
    assert result.open_items == [
        "Confluence: authentication failed (403) — check the e-mail address and API token"
        " — run `oil setup --only atlassian` later"
    ]
    assert result.lines == [f"Jira: {SITE} — projects ABC, MB32", "Atlassian account: Alex Hultsch"]
    assert [row.type for row in _connectors(initialised_home)] == ["jira"]
    assert [e.target for e in _audit_in_order(initialised_home)] == ["Jira"]
    assert secrets.get_secret("ai_oil/Confluence") is None
    assert f"{WIKI}/rest/api/space" not in session.urls()


def test_atlassian_a_confluence_rejection_that_is_not_left_out_asks_for_the_token_again(
    make_ctx, initialised_home: Path
) -> None:
    second = "atl-tok-second"
    routes = _routes(**{"WIKI/rest/api/user/current": _only_token(second, payload={"accountId": "x"})})
    answers = [*FRESH_ANSWERS[:3], "", "n", "", "1"]
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, answers, [TOKEN, second], routes=routes)

    result = stage.run(ctx)

    assert result.status == "done" and result.open_items == []
    assert ctx.console.output.count("authentication failed (401)") == 2
    assert "Is alex@example.com right? [Y/n]: " in ctx.console.prompts
    assert ctx.console.prompts.count("API token (input hidden): ") == 2
    assert result.lines[1] == f"Confluence: {WIKI} — spaces DCDC"
    assert secrets.get_secret("ai_oil/Jira") == second
    assert secrets.get_secret("ai_oil/Confluence") == second
    assert [e.target for e in _audit_in_order(initialised_home)] == ["Jira", "Confluence"]


def test_atlassian_a_new_confluence_connector_inherits_the_bundle_of_the_existing_jira_connector(
    make_ctx, initialised_home: Path
) -> None:
    _add_connector(
        initialised_home,
        "jira",
        "Jira",
        {"base_url": SITE, "email": "alex@corp.com", "projects": ["ABC"], "ca_bundle": "X:/ca.pem"},
    )
    ctx, stage, session = _atlassian(make_ctx, initialised_home, ["", "", "", "", "1"], [TOKEN])

    stage.run(ctx)

    assert all(call[1]["verify"] == "X:/ca.pem" for call in session.calls)
    assert _connectors(initialised_home, "jira")[0].settings["ca_bundle"] == "X:/ca.pem"
    assert _connectors(initialised_home, "confluence")[0].settings["ca_bundle"] == "X:/ca.pem"


def test_atlassian_the_ca_bundle_of_the_network_stage_reaches_both_connectors(
    make_ctx, initialised_home: Path
) -> None:
    ctx, stage, session = _atlassian(
        make_ctx, initialised_home, FRESH_ANSWERS, [TOKEN], state={"ca_bundle": "C:/certs/corp-ca.pem"}
    )

    stage.run(ctx)

    assert all(call[1]["verify"] == "C:/certs/corp-ca.pem" for call in session.calls)
    for type_name in ("jira", "confluence"):
        assert _connectors(initialised_home, type_name)[0].settings["ca_bundle"] == "C:/certs/corp-ca.pem"
    assert "ca_bundle" in _audit_in_order(initialised_home)[0].detail["settings"]


def test_atlassian_no_ca_bundle_means_no_ca_bundle_setting(make_ctx, initialised_home: Path) -> None:
    ctx, stage, session = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS, [TOKEN])
    stage.run(ctx)
    assert all(call[1]["verify"] is True for call in session.calls)
    assert "ca_bundle" not in _connectors(initialised_home, "jira")[0].settings


def test_atlassian_the_email_default_prefers_the_connector_then_me_then_the_windows_account(
    make_ctx, probe, initialised_home: Path, fresh_home: Path
) -> None:
    probe.upn = "alex@windows.corp"
    prompt = "Your Atlassian e-mail address [{}]: "

    ctx, stage, _ = _atlassian(make_ctx, initialised_home, [SITE, "s"], probe=probe)
    with pytest.raises(SkipStage):
        stage.run(ctx)
    assert ctx.console.prompts[1] == prompt.format("alex@example.com")

    _add_connector(initialised_home, "jira", "Jira", {"base_url": SITE, "email": "alex@jira.corp"})
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, ["", "s"], probe=probe)
    with pytest.raises(SkipStage):
        stage.run(ctx)
    assert ctx.console.prompts[1] == prompt.format("alex@jira.corp")

    ctx, stage, _ = _atlassian(make_ctx, fresh_home, [SITE, "s"], probe=probe)
    DataStage().run(ctx)
    with pytest.raises(SkipStage):
        stage.run(ctx)
    assert ctx.console.prompts[1] == prompt.format("alex@windows.corp")


def test_atlassian_the_account_of_another_person_is_reported_and_not_taken(
    make_ctx, initialised_home: Path
) -> None:
    engine = _engine(initialised_home)
    with engine.begin() as conn:
        pat = people.create_person(conn, "Pat")
        people.add_identity(conn, pat.id, "atlassian", ME["accountId"])
    engine.dispose()
    ctx, stage, _ = _atlassian(make_ctx, initialised_home, FRESH_ANSWERS, [TOKEN])

    result = stage.run(ctx)

    assert result.status == "done"
    assert (
        "The Atlassian account Alex Hultsch belongs to Pat — merge that person into you on the People page"
        in ctx.console.output
    )
    assert _identities(initialised_home) == []


def test_atlassian_without_a_me_person_leaves_an_open_item(make_ctx, probe, fresh_home: Path) -> None:
    probe.upn = "alex@corp.com"
    ctx, stage, _ = _atlassian(make_ctx, fresh_home, [SITE, "", "1", "n"], [TOKEN], probe=probe)
    DataStage().run(ctx)

    result = stage.run(ctx)

    assert result.status == "done"
    assert result.open_items == ["About you: run `oil setup --only me`"]
    assert [row.name for row in _connectors(fresh_home)] == ["Jira"]


def test_atlassian_on_an_uninitialised_folder_fails_with_the_init_hint(make_ctx, fresh_home: Path) -> None:
    ctx, stage, _ = _atlassian(make_ctx, fresh_home, [])
    with pytest.raises(ConfigError, match="oil init"):
        stage.run(ctx)


# --- sharepoint ------------------------------------------------------------------------------------

DOCS_URL = "https://acme.sharepoint.com/sites/mb32829/Shared Documents"
PERSONAL_URL = "https://acme-my.sharepoint.com/personal/alex_acme_com"
SYNC_HINT = "Open it in the browser, click Sync, then run `oil setup --only sharepoint`."


def _library(tmp_path: Path, name: str, url: str, *, present: bool = True) -> SyncedLibrary:
    folder = tmp_path / name
    if present:
        folder.mkdir()
    return SyncedLibrary(name, url, str(folder), "teamsite")


@pytest.fixture
def libraries(tmp_path: Path) -> list[SyncedLibrary]:
    return [
        _library(tmp_path, "MB32829 - Documents", DOCS_URL),
        _library(tmp_path, "OneDrive - Acme", PERSONAL_URL),
        _library(tmp_path, "Suppliers", "https://acme.sharepoint.com/sites/suppliers/Shared Documents"),
    ]


def test_sharepoint_without_synced_libraries_is_skipped_with_an_open_item(
    make_ctx, probe, initialised_home: Path
) -> None:
    ctx = make_ctx(initialised_home, probe=probe)

    result = SharePointStage().run(ctx)

    assert result.status == "skipped"
    assert result.lines == ["No synced SharePoint or OneDrive libraries found"]
    assert result.open_items == [
        "SharePoint: open the library in the browser, click Sync, then run `oil setup --only sharepoint`"
    ]
    assert ctx.console.prompts == [] and _connectors(initialised_home) == []


def test_sharepoint_fresh_selection_creates_the_connector_and_audits_it(
    make_ctx, probe, initialised_home: Path, libraries: list[SyncedLibrary]
) -> None:
    probe.libraries = libraries
    ctx = make_ctx(initialised_home, ["1,3"], probe=probe)

    result = SharePointStage().run(ctx)

    output = ctx.console.output
    assert f"  1) MB32829 - Documents — {DOCS_URL}" in output
    assert f"Missing a library? {SYNC_HINT}" in output
    assert ctx.console.prompts[-1] == "Numbers, comma-separated, a = all: "
    assert result.status == "done"
    assert result.lines == [
        f"Sync root: {libraries[0].url} — {libraries[0].path}",
        f"Sync root: {libraries[2].url} — {libraries[2].path}",
    ]
    (row,) = _connectors(initialised_home, "sharepoint")
    assert row.name == "SharePoint" and row.credential_ref is None
    assert row.settings == {
        "sync_roots": [
            {"url": libraries[0].url, "path": libraries[0].path},
            {"url": libraries[2].url, "path": libraries[2].path},
        ]
    }
    (entry,) = _audit_in_order(initialised_home)
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "connector.create",
        "B",
        "user",
        "SharePoint",
    )
    assert entry.detail == {"type": "sharepoint", "settings": row.settings, "credential": "none"}


def test_sharepoint_defaults_come_from_the_existing_connector_and_hand_added_roots_are_kept(
    make_ctx, probe, initialised_home: Path, libraries: list[SyncedLibrary]
) -> None:
    by_hand = {"url": "https://acme.sharepoint.com/sites/archive/Shared Documents", "path": "D:/archive"}
    _add_connector(
        initialised_home,
        "sharepoint",
        "Docs",
        {
            "hydrate": False,
            "sync_roots": [
                {"url": libraries[1].url.upper().replace("HTTPS", "https"), "path": libraries[1].path},
                by_hand,
            ],
        },
    )
    probe.libraries = libraries
    ctx = make_ctx(initialised_home, [""], probe=probe)

    result = SharePointStage().run(ctx)

    assert ctx.console.prompts[-1] == "Numbers, comma-separated, a = all [2]: "
    (row,) = _connectors(initialised_home, "sharepoint")
    assert row.name == "Docs"
    assert row.settings["hydrate"] is False
    assert row.settings["sync_roots"] == [{"url": libraries[1].url, "path": libraries[1].path}, by_hand]
    assert len(result.lines) == 2 and result.lines[1] == f"Sync root: {by_hand['url']} — D:/archive"
    (entry,) = _audit_in_order(initialised_home)
    assert (entry.action, entry.target, entry.detail["credential"]) == (
        "connector.update",
        "Docs",
        "unchanged",
    )


def test_sharepoint_a_library_that_is_deselected_is_dropped(
    make_ctx, probe, initialised_home: Path, libraries: list[SyncedLibrary]
) -> None:
    _add_connector(
        initialised_home,
        "sharepoint",
        "SharePoint",
        {"sync_roots": [{"url": libraries[1].url, "path": libraries[1].path}]},
    )
    probe.libraries = libraries

    SharePointStage().run(make_ctx(initialised_home, ["3"], probe=probe))

    (row,) = _connectors(initialised_home, "sharepoint")
    assert row.settings["sync_roots"] == [{"url": libraries[2].url, "path": libraries[2].path}]


def test_sharepoint_a_library_whose_folder_is_missing_is_marked_and_refused(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    present = _library(tmp_path, "Present", DOCS_URL)
    missing = _library(tmp_path, "Missing", PERSONAL_URL, present=False)
    probe.libraries = [present, missing]
    ctx = make_ctx(initialised_home, ["2", "a", "1"], probe=probe)

    result = SharePointStage().run(ctx)

    assert f"  2) Missing — {PERSONAL_URL} (folder missing)" in ctx.console.output
    assert ctx.console.output.count(f"The folder of Missing does not exist yet ({missing.path})") == 2
    assert result.lines == [f"Sync root: {present.url} — {present.path}"]


def test_sharepoint_at_least_one_library_is_required(
    make_ctx, probe, initialised_home: Path, libraries: list[SyncedLibrary]
) -> None:
    probe.libraries = libraries
    ctx = make_ctx(initialised_home, ["", "2"], probe=probe)

    result = SharePointStage().run(ctx)

    assert "Select at least one library." in ctx.console.output
    assert result.lines == [f"Sync root: {libraries[1].url} — {libraries[1].path}"]


def test_sharepoint_libraries_with_the_same_url_are_listed_once(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    first = _library(tmp_path, "First", DOCS_URL)
    probe.libraries = [
        first,
        SyncedLibrary("Again", DOCS_URL.upper().replace("HTTPS", "https"), first.path, ""),
    ]
    ctx = make_ctx(initialised_home, ["1"], probe=probe)

    SharePointStage().run(ctx)

    assert "  2)" not in ctx.console.output
    assert "Again" not in ctx.console.output


def test_sharepoint_on_an_uninitialised_folder_fails_with_the_init_hint(
    make_ctx, probe, fresh_home: Path, libraries: list[SyncedLibrary]
) -> None:
    probe.libraries = libraries
    with pytest.raises(ConfigError, match="oil init"):
        SharePointStage().run(make_ctx(fresh_home, probe=probe))
