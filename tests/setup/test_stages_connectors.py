from collections.abc import Callable
from pathlib import Path

import pytest
import requests

from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import people
from ai_oil.errors import ConfigError
from ai_oil.governance import audit
from ai_oil.setup import wizard
from ai_oil.setup.console import SkipStage
from ai_oil.setup.probe import InternetSettings
from ai_oil.setup.stages_basic import DataStage
from ai_oil.setup.stages_connectors import NetworkStage, OutlookStage, ProbeOutcome, probe_https
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


def _add_connector(home: Path, type_name: str, name: str, settings: dict | None = None) -> None:
    engine = _engine(home)
    try:
        with engine.begin() as conn:
            registry.create_connector(conn, type_name, name, settings=settings)
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
    ctx = make_ctx(initialised_home, ["n"], probe=probe)
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
