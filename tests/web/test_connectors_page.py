import re
from collections.abc import Iterator
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil import secrets, timeutil
from ai_oil.domain import people
from ai_oil.governance import audit
from ai_oil.sources import linking, registry, store
from ai_oil.sources.model import ConnectorConfig, HealthCheck, HealthReport, ObjectMeta

SECRET = "s3cr3t-api-token-value"


class FakeConnector:
    """A connector whose live check is scripted through class attributes."""

    type: ClassVar[str] = "fakeconn"
    object_types: ClassVar[frozenset[str]] = frozenset({"jira_issue"})
    action_class: ClassVar[str] = "A"
    checks: ClassVar[tuple[HealthCheck, ...]] = (HealthCheck("reachable", True, "200 OK"),)
    raises: ClassVar[Exception | None] = None

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        if config.settings.get("broken"):
            raise RuntimeError("settings are broken")
        self.config = config

    def check(self) -> HealthReport:
        if self.raises is not None:
            raise self.raises
        return HealthReport(self.checks)


@pytest.fixture(autouse=True)
def fakeconn_registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FakeConnector, "checks", (HealthCheck("reachable", True, "200 OK"),))
    monkeypatch.setattr(FakeConnector, "raises", None)
    registry.register_connector_type(FakeConnector)
    yield
    registry.unregister_connector_type("fakeconn")


@pytest.fixture
def add_connector(app: FastAPI):
    def add(name: str = "Jira main", *, credential_ref: str | None = None, **settings: object) -> int:
        with app.state.engine.begin() as conn:
            return registry.create_connector(
                conn, "fakeconn", name, settings=settings, credential_ref=credential_ref
            ).id

    return add


@pytest.fixture
def post(client: TestClient, csrf_token: str):
    def send(path: str, *, csrf: bool = True, htmx: bool = False):
        headers = {"X-CSRF-Token": csrf_token} if csrf else {}
        if htmx:
            headers["HX-Request"] = "true"
        return client.post(path, headers=headers, follow_redirects=False)

    return send


def card_of(page: str, connector_id: int) -> str:
    match = re.search(
        rf'<section class="card connector" id="connector-{connector_id}">.*?</section>', page, re.DOTALL
    )
    assert match is not None, f"no card for connector {connector_id}"
    return match.group(0)


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def check_lines(card: str) -> list[str]:
    block = re.search(r'<ul class="check-lines">(.*?)</ul>', card, re.DOTALL)
    assert block is not None
    return [plain(item) for item in re.findall(r"<li[^>]*>(.*?)</li>", block.group(1), re.DOTALL)]


def seed_object(
    app: FastAPI,
    connector_id: int,
    external_id: str,
    *,
    status: str = "ok",
    link: bool = True,
) -> int:
    with app.state.engine.begin() as conn:
        meta = ObjectMeta("jira_issue", external_id, title=external_id, display_ref=external_id)
        object_id = store.upsert_object(conn, connector_id, meta)
        conn.execute(
            text("UPDATE source_object SET fetch_status = :status WHERE id = :id"),
            {"status": status, "id": object_id},
        )
        if link:
            linking.link_source(conn, "OIL-1", object_id, actor_id=people.get_me(conn).id)
    return object_id


def seed_run(app: FastAPI, started_at: str, object_ids: list[int]) -> int:
    with app.state.engine.begin() as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO sync_run (trigger, scope, started_at, status) VALUES ('cli', 'all', :at, 'ok')"
                " RETURNING id"
            ),
            {"at": started_at},
        ).scalar_one()
        for object_id in object_ids:
            conn.execute(
                text(
                    "INSERT INTO sync_result (sync_run_id, source_object_id, outcome)"
                    " VALUES (:run, :object, 'unchanged')"
                ),
                {"run": run_id, "object": object_id},
            )
    return run_id


def entries(app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def enabled_of(app: FastAPI, connector_id: int) -> bool:
    with app.state.engine.begin() as conn:
        return registry.get_connector(conn, connector_id).enabled


# --- the page ----------------------------------------------------------------------------------------------


def test_the_page_is_in_the_navigation_and_says_connectors_are_added_on_the_command_line(
    client: TestClient,
) -> None:
    page = client.get("/connectors").text

    assert '<a href="/connectors" class="active" aria-current="page">Connectors</a>' in page
    assert "No connectors configured yet." in page
    assert "<code>oil connector add TYPE NAME</code>" in page
    assert "Add connector" not in page


def test_a_card_shows_name_type_state_settings_and_the_configuration_check(
    client: TestClient, add_connector
) -> None:
    connector_id = add_connector("Jira main", site="https://example.atlassian.net", limit=5)

    card = card_of(client.get("/connectors").text, connector_id)

    assert "<h2>Jira main</h2>" in card
    assert '<span class="muted">fakeconn</span>' in card
    assert ">enabled</span>" in card
    assert (
        '<pre class="settings">{\n  &#34;limit&#34;: 5,\n  &#34;site&#34;: &#34;https://example.atlassian.net&#34;\n}</pre>'
        in (card)
    )
    assert check_lines(card) == ["[ok] plugin loaded"]
    assert "Last sync" in card
    assert plain(card).count("never") == 1
    assert "Disable" in card
    assert "Live check" in card
    assert "Enable<" not in card


def test_credential_states_are_stored_missing_and_a_dash(client: TestClient, add_connector) -> None:
    secrets.set_secret("ai_oil/Has", SECRET)
    stored = add_connector("Has", credential_ref="ai_oil/Has")
    missing = add_connector("Lacks", credential_ref="ai_oil/Lacks")
    none = add_connector("Without")

    page = client.get("/connectors").text

    assert "<dt>Credential</dt><dd>stored</dd>" in card_of(page, stored)
    assert check_lines(card_of(page, stored)) == ["[ok] plugin loaded", "[ok] credential stored"]
    assert "<dt>Credential</dt><dd>missing</dd>" in card_of(page, missing)
    assert check_lines(card_of(page, missing)) == [
        "[ok] plugin loaded",
        "[FAIL] credential missing for ai_oil/Lacks",
    ]
    assert "<dt>Credential</dt><dd>—</dd>" in card_of(page, none)


def test_the_secret_appears_nowhere_in_the_page_or_the_live_check(
    client: TestClient, add_connector, post
) -> None:
    secrets.set_secret("ai_oil/Has", SECRET)
    connector_id = add_connector("Has", credential_ref="ai_oil/Has")

    page = client.get("/connectors").text
    checked = post(f"/connectors/{connector_id}/check", htmx=True).text

    assert SECRET not in page
    assert SECRET not in checked
    assert "ai_oil/Has" not in page


def test_a_plugin_that_cannot_start_is_a_fail_line(client: TestClient, add_connector) -> None:
    connector_id = add_connector("Broken", broken=True)

    card = card_of(client.get("/connectors").text, connector_id)

    assert check_lines(card) == ["[FAIL] plugin: RuntimeError: settings are broken"]


def test_a_disabled_connector_shows_disabled_and_offers_enable_only(
    app: FastAPI, client: TestClient, add_connector
) -> None:
    connector_id = add_connector("Off")
    with app.state.engine.begin() as conn:
        registry.set_enabled(conn, connector_id, False)

    card = card_of(client.get("/connectors").text, connector_id)

    assert ">disabled</span>" in card
    assert check_lines(card) == ["[--] disabled"]
    assert f'action="/connectors/{connector_id}/enable"' in card
    assert "Live check" not in card
    assert "/disable" not in card


def test_last_sync_is_the_newest_run_with_a_result_for_the_connector(
    app: FastAPI, client: TestClient, add_connector
) -> None:
    first = add_connector("First")
    second = add_connector("Second")
    third = add_connector("Third")
    first_object = seed_object(app, first, "A-1")
    second_object = seed_object(app, second, "B-1")
    seed_run(app, "2026-10-20T08:00:00.000Z", [first_object])
    seed_run(app, "2026-10-22T09:30:00.000Z", [first_object])
    seed_run(app, "2026-10-25T07:15:00.000Z", [second_object])
    seed_run(app, "2026-10-26T12:00:00.000Z", [])  # a run without any result belongs to nobody
    page = client.get("/connectors").text

    def local(stamp: str) -> str:
        return timeutil.parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M")

    assert f"<dt>Last sync</dt><dd>{local('2026-10-22T09:30:00.000Z')}</dd>" in card_of(page, first)
    assert f"<dt>Last sync</dt><dd>{local('2026-10-25T07:15:00.000Z')}</dd>" in card_of(page, second)
    assert "<dt>Last sync</dt><dd>never</dd>" in card_of(page, third)


def test_the_error_count_is_the_active_linked_objects_in_error(
    app: FastAPI, client: TestClient, add_connector
) -> None:
    connector_id = add_connector("Jira main")
    other = add_connector("Other")
    seed_object(app, connector_id, "A-1", status="error")
    seed_object(app, connector_id, "A-2", status="error")
    seed_object(app, connector_id, "A-3", status="ok")
    seed_object(app, connector_id, "A-4", status="error", link=False)
    removed = seed_object(app, connector_id, "A-5", status="error")
    with app.state.engine.begin() as conn:
        linking.unlink_source(conn, "OIL-1", removed, actor_id=people.get_me(conn).id)

    page = client.get("/connectors").text

    assert '<dt>Fetch errors</dt><dd><span class="fetch-error">2 linked objects</span></dd>' in card_of(
        page, connector_id
    )
    assert "<dt>Fetch errors</dt><dd>0</dd>" in card_of(page, other)


def test_plugins_that_failed_to_load_are_listed_in_a_warning_box(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenEntryPoint:
        name = "brokenplugin"

        def load(self) -> type:
            raise ImportError("no module named vendor")

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda group: [BrokenEntryPoint()])

    page = client.get("/connectors").text

    assert '<div class="warning" role="alert">' in page
    assert "<code>brokenplugin</code>: ImportError: no module named vendor" in page


def test_there_is_no_warning_box_when_every_plugin_loads(client: TestClient, add_connector) -> None:
    add_connector()

    assert 'class="warning"' not in client.get("/connectors").text


# --- the live check ----------------------------------------------------------------------------------------


def test_the_live_check_returns_the_card_with_the_check_lines(
    client: TestClient, add_connector, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FakeConnector,
        "checks",
        (HealthCheck("reachable", True, "200 OK"), HealthCheck("authenticated", True, "as Alex")),
    )
    connector_id = add_connector()
    page = client.get("/connectors").text
    assert "authenticated" not in page
    assert f'hx-post="/connectors/{connector_id}/check"' in card_of(page, connector_id)

    response = post(f"/connectors/{connector_id}/check", htmx=True)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert response.text.lstrip().startswith(
        f'<section class="card connector" id="connector-{connector_id}">'
    )
    assert check_lines(response.text) == [
        "[ok] plugin loaded",
        "[ok] reachable: 200 OK",
        "[ok] authenticated: as Alex",
    ]


def test_a_failed_live_check_is_a_fail_line(add_connector, post, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        FakeConnector,
        "checks",
        (HealthCheck("reachable", True, "200 OK"), HealthCheck("authenticated", False, "401 Unauthorized")),
    )
    connector_id = add_connector()

    lines = check_lines(post(f"/connectors/{connector_id}/check", htmx=True).text)

    assert lines == ["[ok] plugin loaded", "[ok] reachable: 200 OK", "[FAIL] authenticated: 401 Unauthorized"]


def test_an_exception_of_the_live_check_is_shown_as_a_fail_line(
    add_connector, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeConnector, "raises", TimeoutError("no answer from the server"))
    connector_id = add_connector()

    response = post(f"/connectors/{connector_id}/check", htmx=True)

    assert response.status_code == 200
    assert check_lines(response.text) == [
        "[ok] plugin loaded",
        "[FAIL] live check: TimeoutError: no answer from the server",
    ]


def test_the_live_check_is_read_only_and_writes_no_audit_entry(app: FastAPI, add_connector, post) -> None:
    connector_id = add_connector()
    before = entries(app, "")

    post(f"/connectors/{connector_id}/check", htmx=True)

    assert entries(app, "") == before


def test_the_live_check_of_a_disabled_connector_runs_nothing(
    app: FastAPI, add_connector, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeConnector, "raises", AssertionError("must not be called"))
    connector_id = add_connector()
    with app.state.engine.begin() as conn:
        registry.set_enabled(conn, connector_id, False)

    response = post(f"/connectors/{connector_id}/check", htmx=True)

    assert check_lines(response.text) == ["[--] disabled"]


def test_the_live_check_of_an_unknown_connector_is_a_404(post) -> None:
    assert post("/connectors/99/check", htmx=True).status_code == 404


def test_the_live_check_needs_the_csrf_token(add_connector, post) -> None:
    connector_id = add_connector()

    assert post(f"/connectors/{connector_id}/check", csrf=False, htmx=True).status_code == 403


# --- enable and disable ------------------------------------------------------------------------------------


def test_disable_then_enable_redirects_changes_the_row_and_audits_with_via_web(
    app: FastAPI, add_connector, post
) -> None:
    connector_id = add_connector("Jira main")

    disabled = post(f"/connectors/{connector_id}/disable")

    assert (disabled.status_code, disabled.headers["location"]) == (303, "/connectors")
    assert enabled_of(app, connector_id) is False
    enabled = post(f"/connectors/{connector_id}/enable")
    assert (enabled.status_code, enabled.headers["location"]) == (303, "/connectors")
    assert enabled_of(app, connector_id) is True
    recorded = entries(app, "connector.")
    assert [(e.action, e.action_class, e.actor_type, e.target) for e in reversed(recorded)] == [
        ("connector.disable", "B", "user", "Jira main"),
        ("connector.enable", "B", "user", "Jira main"),
    ]
    assert all(e.detail == {"type": "fakeconn", "via": "web"} for e in recorded)


def test_enable_and_disable_of_an_unknown_connector_are_404s(app: FastAPI, post) -> None:
    assert post("/connectors/99/enable").status_code == 404
    assert post("/connectors/99/disable").status_code == 404
    assert entries(app, "connector.") == []


def test_enable_and_disable_need_the_csrf_token(app: FastAPI, add_connector, post) -> None:
    connector_id = add_connector()

    assert post(f"/connectors/{connector_id}/disable", csrf=False).status_code == 403
    assert enabled_of(app, connector_id) is True
    assert post(f"/connectors/{connector_id}/enable", csrf=False).status_code == 403
    assert entries(app, "connector.") == []


def test_the_page_forms_carry_the_csrf_token(client: TestClient, add_connector, csrf_token: str) -> None:
    connector_id = add_connector()

    card = card_of(client.get("/connectors").text, connector_id)

    assert f'<input type="hidden" name="csrf_token" value="{csrf_token}">' in card
