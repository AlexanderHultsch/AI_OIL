import getpass
import importlib.metadata
import io
import json
import re
from collections.abc import Iterator
from typing import ClassVar

import pytest
import sqlalchemy

from ai_oil import secrets
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import registry
from ai_oil.sources.model import (
    CandidateQuery,
    ConnectorConfig,
    FetchResult,
    HealthCheck,
    HealthReport,
    ObjectMeta,
    ObjectState,
    SourceRef,
)

SECRET = "s3cr3t-api-token"


class FakeConnector:
    """Validates `limit` in its constructor; `mode` decides what `check` does (ok, fail or raise)."""

    type: ClassVar[str] = "fakeconn"
    object_types: ClassVar[frozenset[str]] = frozenset({"note"})
    action_class: ClassVar[str] = "A"
    max_parallel: ClassVar[int] = 1
    checks_run: ClassVar[list[str]] = []

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        limit = config.settings.get("limit", 1)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError("Fake setting limit must be a whole number")
        self.config = config

    def check(self) -> HealthReport:
        self.checks_run.append(self.config.name)
        mode = self.config.settings.get("mode", "ok")
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "fail":
            return HealthReport(
                (HealthCheck("auth", True, "signed in as Alex"), HealthCheck("folder", False, "not found"))
            )
        return HealthReport((HealthCheck("reachable", True, "all good"), HealthCheck("auth", True, "fine")))

    def parse_reference(self, text: str) -> SourceRef | None:
        return None

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(ref.object_type, ref.external_id)

    def fetch(self, state: ObjectState) -> FetchResult:
        return FetchResult()

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


class BrokenConnector(FakeConnector):
    type: ClassVar[str] = "brokenconn"

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        raise RuntimeError("cannot start")


@pytest.fixture(autouse=True)
def fake_connectors_registered() -> Iterator[None]:
    FakeConnector.checks_run.clear()
    registry.register_connector_type(FakeConnector)
    registry.register_connector_type(BrokenConnector)
    yield
    registry.unregister_connector_type("fakeconn")
    registry.unregister_connector_type("brokenconn")


def run(oil, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = oil(*argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def connectors(db) -> list[registry.ConnectorRow]:
    with db() as conn:
        return registry.list_connectors(conn)


def audit_entries(db, prefix: str = "connector.") -> list[audit.AuditEntry]:
    with db() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def create_directly(home, type_name: str, name: str, **kwargs) -> registry.ConnectorRow:
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.begin() as conn:
            return registry.create_connector(conn, type_name, name, **kwargs)
    finally:
        engine.dispose()


# --- oil connector add -----------------------------------------------------------------------------------


def test_add_parses_settings_as_json_when_possible_and_as_text_otherwise(oil, db, capsys) -> None:
    code, out, err = run(
        oil,
        capsys,
        "connector",
        "add",
        "fakeconn",
        "Fake",
        "--setting",
        "limit=5",
        "--setting",
        "hydrate=false",
        "--setting",
        'projects=["HW","SUP"]',
        "--setting",
        'roots=[{"url":"https://a","path":"C:/x"}]',
        "--setting",
        "base_url=https://acme.atlassian.net",
        "--setting",
        "email=alex@example.com",
        "--setting",
        "note=a=b",
    )

    assert (code, out, err) == (0, "Added connector Fake (fakeconn)\n", "")
    [row] = connectors(db)
    assert (row.type, row.name, row.enabled, row.credential_ref) == ("fakeconn", "Fake", True, None)
    assert row.settings == {
        "limit": 5,
        "hydrate": False,
        "projects": ["HW", "SUP"],
        "roots": [{"url": "https://a", "path": "C:/x"}],
        "base_url": "https://acme.atlassian.net",
        "email": "alex@example.com",
        "note": "a=b",
    }
    [entry] = audit_entries(db)
    assert (entry.action, entry.action_class, entry.actor_type, entry.target) == (
        "connector.create",
        "B",
        "user",
        "Fake",
    )
    assert entry.detail == {"type": "fakeconn", "settings": row.settings, "credential": "none"}


@pytest.mark.parametrize("item", ["limit", "=5", " =5"])
def test_add_rejects_a_setting_without_key_or_equals_sign(oil, db, capsys, item: str) -> None:
    code, out, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--setting", item)

    assert code == 1
    assert out == ""
    assert "invalid --setting" in err and "KEY=VALUE" in err
    assert connectors(db) == []


def test_add_with_invalid_settings_stores_neither_row_nor_secret(oil, db, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"{SECRET}\n")

    code, out, err = run(
        oil, capsys, "connector", "add", "fakeconn", "Fake", "--setting", "limit=many", "--token-stdin"
    )

    assert code == 1
    assert out == ""
    assert "limit must be a whole number" in err
    assert connectors(db) == []
    assert audit_entries(db) == []
    assert secrets.get_secret("ai_oil/Fake") is None
    assert SECRET not in out + err


def test_add_rejects_a_setting_that_looks_like_a_secret(oil, db, capsys) -> None:
    code, _, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--setting", "api_token=abc123")

    assert code == 1
    assert "GOV-8" in err
    assert "abc123" not in err
    assert connectors(db) == []


def test_add_of_an_unknown_type_is_rejected(oil, db, capsys) -> None:
    code, _, err = run(oil, capsys, "connector", "add", "nosuchtype", "X")

    assert code == 1
    assert "unknown connector type: nosuchtype" in err
    assert connectors(db) == []


def test_add_of_a_type_that_cannot_start_stores_nothing(oil, db) -> None:
    with pytest.raises(RuntimeError, match="cannot start"):
        oil("connector", "add", "brokenconn", "Broken")

    assert connectors(db) == []
    assert audit_entries(db) == []


def test_add_with_token_prompts_and_stores_the_secret_under_the_connector_name(
    oil, db, capsys, monkeypatch
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return SECRET

    monkeypatch.setattr(getpass, "getpass", fake_getpass)

    code, out, err = run(oil, capsys, "connector", "add", "fakeconn", "My Fake", "--token")

    assert (code, out, err) == (0, "Added connector My Fake (fakeconn)\n", "")
    assert prompts == ["API token: "]
    [row] = connectors(db)
    assert row.credential_ref == "ai_oil/My Fake"
    assert secrets.get_secret("ai_oil/My Fake") == SECRET
    [entry] = audit_entries(db)
    assert entry.detail["credential"] == "stored"
    assert SECRET not in json.dumps(entry.detail)
    assert SECRET not in json.dumps(row.settings)


def test_add_with_token_stdin_reads_one_stripped_line(oil, db, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"  {SECRET}  \nignored second line\n")

    code, out, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token-stdin")

    assert (code, err) == (0, "")
    assert SECRET not in out
    [row] = connectors(db)
    assert row.credential_ref == "ai_oil/Fake"
    assert secrets.get_secret("ai_oil/Fake") == SECRET


def test_the_token_never_reaches_stdout_stderr_audit_or_the_database(
    oil, db, home, capsys, monkeypatch
) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    code, out, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token-stdin")
    assert code == 0
    for command in (("connector", "list"), ("connector", "check", "--live")):
        _, listed, listed_err = run(oil, capsys, *command)
        out += listed
        err += listed_err
    stdin(monkeypatch, f"{SECRET}-2\n")
    _, out_set, err_set = run(
        oil, capsys, "connector", "set", "Fake", "--setting", "limit=3", "--token-stdin"
    )

    assert SECRET not in out + err + out_set + err_set
    with db() as conn:
        dumped = conn.execute(
            sqlalchemy.text("SELECT group_concat(settings_json) FROM connector")
        ).scalar_one()
        details = conn.execute(
            sqlalchemy.text("SELECT group_concat(detail_json) FROM audit_log")
        ).scalar_one()
    assert SECRET not in dumped
    assert SECRET not in details
    db_path = load_config(home).db_path
    for path in db_path.parent.glob(f"{db_path.name}*"):
        assert SECRET.encode() not in path.read_bytes()


@pytest.mark.parametrize("line", ["", "   \n", "\n"])
def test_add_with_an_empty_token_is_rejected(oil, db, capsys, monkeypatch, line: str) -> None:
    stdin(monkeypatch, line)

    code, _, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token-stdin")

    assert code == 1
    assert "token must not be empty" in err
    assert connectors(db) == []
    assert secrets.get_secret("ai_oil/Fake") is None


def test_add_with_an_empty_prompted_token_is_rejected(oil, db, capsys, monkeypatch) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")

    code, _, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token")

    assert code == 1
    assert "token must not be empty" in err
    assert connectors(db) == []


def test_add_with_both_token_options_is_rejected(oil, db, capsys) -> None:
    code, _, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token", "--token-stdin")

    assert code == 1
    assert "--token or --token-stdin" in err
    assert connectors(db) == []


def test_a_duplicate_name_is_rejected_and_keeps_the_stored_secret(oil, db, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    assert oil("connector", "add", "fakeconn", "Fake", "--token-stdin") == 0
    capsys.readouterr()
    stdin(monkeypatch, "another-token\n")

    code, out, err = run(oil, capsys, "connector", "add", "fakeconn", "Fake", "--token-stdin")

    assert code == 1
    assert out == ""
    assert "already exists" in err
    assert "another-token" not in err
    assert secrets.get_secret("ai_oil/Fake") == SECRET
    assert len(connectors(db)) == 1
    assert len(audit_entries(db)) == 1


def test_add_with_a_blank_name_is_rejected(oil, db, capsys) -> None:
    code, _, err = run(oil, capsys, "connector", "add", "fakeconn", "  ")

    assert code == 1
    assert "name" in err
    assert connectors(db) == []


# --- oil connector list ----------------------------------------------------------------------------------


def test_list_shows_state_credential_and_sorted_settings(oil, db, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    assert oil("connector", "add", "fakeconn", "Alpha", "--setting", "mode=ok", "--setting", "limit=2") == 0
    assert oil("connector", "add", "fakeconn", "Beta") == 0
    assert oil("connector", "add", "fakeconn", "Gamma", "--token-stdin") == 0
    assert oil("connector", "disable", "Beta") == 0
    secrets.delete_secret("ai_oil/Gamma")
    capsys.readouterr()

    code, out, err = run(oil, capsys, "connector", "list")

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "1  Alpha  fakeconn  enabled  credential: -",
        '    settings: {"limit": 2, "mode": "ok"}',
        "2  Beta  fakeconn  disabled  credential: -",
        "    settings: {}",
        "3  Gamma  fakeconn  enabled  credential: missing",
        "    settings: {}",
    ]


def test_list_shows_a_stored_credential(oil, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    assert oil("connector", "add", "fakeconn", "Alpha", "--token-stdin") == 0
    capsys.readouterr()

    _, out, _ = run(oil, capsys, "connector", "list")

    assert out.splitlines()[0] == "1  Alpha  fakeconn  enabled  credential: stored"
    assert SECRET not in out


def test_list_without_connectors_prints_nothing(oil, capsys) -> None:
    assert run(oil, capsys, "connector", "list") == (0, "", "")


def test_list_warns_about_plugins_that_failed_to_load(oil, capsys, monkeypatch) -> None:
    assert oil("connector", "add", "fakeconn", "Alpha") == 0
    capsys.readouterr()

    class BrokenEntryPoint:
        name = "winplug"

        def load(self) -> type:
            raise ImportError("no module named pywin32")

    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **kwargs: [BrokenEntryPoint()])

    code, out, err = run(oil, capsys, "connector", "list")

    assert code == 0
    assert err == "warning: connector plugin winplug failed to load: ImportError: no module named pywin32\n"
    assert out.splitlines()[0] == "1  Alpha  fakeconn  enabled  credential: -"


# --- oil connector set -----------------------------------------------------------------------------------


def test_set_merges_settings_and_unsets_keys(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake", "--setting", "limit=1", "--setting", "keep=yes") == 0
    assert oil("connector", "set", "Fake", "--setting", "limit=7", "--setting", "extra=[1,2]") == 0
    capsys.readouterr()

    code, out, err = run(oil, capsys, "connector", "set", "Fake", "--unset", "keep", "--unset", "absent")

    assert (code, out, err) == (0, "Updated connector Fake\n", "")
    [row] = connectors(db)
    assert row.settings == {"limit": 7, "extra": [1, 2]}
    assert row.credential_ref is None
    entries = audit_entries(db, "connector.update")
    assert [entry.target for entry in entries] == ["Fake", "Fake"]
    assert entries[0].action_class == "B"
    assert entries[0].detail["settings"] == {"limit": 7, "extra": [1, 2]}
    assert entries[0].detail["credential"] == "unchanged"


def test_set_finds_the_connector_by_id(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "set", "1", "--setting", "limit=3")

    assert (code, out) == (0, "Updated connector Fake\n")
    assert connectors(db)[0].settings == {"limit": 3}


def test_set_with_invalid_settings_changes_nothing(oil, db, capsys, monkeypatch) -> None:
    assert oil("connector", "add", "fakeconn", "Fake", "--setting", "limit=2") == 0
    capsys.readouterr()
    stdin(monkeypatch, f"{SECRET}\n")

    code, _, err = run(oil, capsys, "connector", "set", "Fake", "--setting", "limit=many", "--token-stdin")

    assert code == 1
    assert "limit must be a whole number" in err
    [row] = connectors(db)
    assert row.settings == {"limit": 2}
    assert row.credential_ref is None
    assert secrets.get_secret("ai_oil/Fake") is None
    assert len(audit_entries(db, "connector.update")) == 0


def test_set_with_a_token_creates_the_credential_reference(oil, db, capsys, monkeypatch) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()
    stdin(monkeypatch, f"{SECRET}\n")

    code, out, err = run(oil, capsys, "connector", "set", "Fake", "--token-stdin")

    assert (code, out, err) == (0, "Updated connector Fake\n", "")
    [row] = connectors(db)
    assert row.credential_ref == "ai_oil/Fake"
    assert secrets.get_secret("ai_oil/Fake") == SECRET
    [entry] = audit_entries(db, "connector.update")
    assert entry.detail["credential"] == "stored"
    assert SECRET not in json.dumps(entry.detail)


def test_set_with_a_token_replaces_the_secret_under_the_existing_reference(
    oil, db, home, capsys, monkeypatch
) -> None:
    create_directly(home, "fakeconn", "Fake", credential_ref="elsewhere/ref")
    secrets.set_secret("elsewhere/ref", "old-token")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": SECRET)

    code, _, _ = run(oil, capsys, "connector", "set", "Fake", "--token")

    assert code == 0
    assert connectors(db)[0].credential_ref == "elsewhere/ref"
    assert secrets.get_secret("elsewhere/ref") == SECRET
    assert secrets.get_secret("ai_oil/Fake") is None


def test_set_without_anything_to_change_is_rejected(oil, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()

    code, _, err = run(oil, capsys, "connector", "set", "Fake")

    assert code == 1
    assert "nothing to change" in err


def test_set_and_unset_of_the_same_key_is_rejected(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake", "--setting", "limit=2") == 0
    capsys.readouterr()

    code, _, err = run(oil, capsys, "connector", "set", "Fake", "--setting", "limit=3", "--unset", "limit")

    assert code == 1
    assert "limit is both set and unset" in err
    assert connectors(db)[0].settings == {"limit": 2}


def test_set_of_an_unknown_connector_is_rejected(oil, capsys) -> None:
    code, _, err = run(oil, capsys, "connector", "set", "Nope", "--setting", "limit=3")

    assert code == 1
    assert "connector Nope not found" in err


def test_set_rejects_a_setting_that_looks_like_a_secret(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()

    code, _, err = run(oil, capsys, "connector", "set", "Fake", "--setting", "password=hunter2")

    assert code == 1
    assert "GOV-8" in err
    assert "hunter2" not in err
    assert connectors(db)[0].settings == {}


# --- oil connector check ---------------------------------------------------------------------------------


def test_check_without_live_reports_the_configuration_and_never_calls_the_connector(
    oil, capsys, monkeypatch
) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    assert oil("connector", "add", "fakeconn", "Alpha", "--token-stdin") == 0
    assert oil("connector", "add", "fakeconn", "Beta") == 0
    capsys.readouterr()

    code, out, err = run(oil, capsys, "connector", "check")

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "Alpha (fakeconn)",
        "  [ok] plugin loaded",
        "  [ok] credential stored",
        "Beta (fakeconn)",
        "  [ok] plugin loaded",
    ]
    assert FakeConnector.checks_run == []


def test_check_fails_when_the_credential_is_missing(oil, capsys, monkeypatch) -> None:
    stdin(monkeypatch, f"{SECRET}\n")
    assert oil("connector", "add", "fakeconn", "Fake", "--token-stdin") == 0
    secrets.delete_secret("ai_oil/Fake")
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "check", "Fake")

    assert code == 1
    assert out.splitlines() == [
        "Fake (fakeconn)",
        "  [ok] plugin loaded",
        "  [FAIL] credential missing for ai_oil/Fake",
    ]


def test_check_reports_a_plugin_that_cannot_start_and_a_type_that_is_gone(oil, home, capsys) -> None:
    create_directly(home, "brokenconn", "Broken")
    create_directly(home, "fakeconn", "Gone")
    registry.unregister_connector_type("fakeconn")

    code, out, _ = run(oil, capsys, "connector", "check", "--live")

    assert code == 1
    lines = out.splitlines()
    assert lines[:2] == ["Broken (brokenconn)", "  [FAIL] plugin: RuntimeError: cannot start"]
    assert lines[2] == "Gone (fakeconn)"
    assert re.fullmatch(r"  \[FAIL\] plugin: NotFoundError: unknown connector type: fakeconn.*", lines[3])
    assert len(lines) == 4
    assert FakeConnector.checks_run == []


def test_check_live_prints_every_health_check(oil, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fine") == 0
    capsys.readouterr()

    code, out, err = run(oil, capsys, "connector", "check", "Fine", "--live")

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "Fine (fakeconn)",
        "  [ok] plugin loaded",
        "  [ok] reachable: all good",
        "  [ok] auth: fine",
    ]
    assert FakeConnector.checks_run == ["Fine"]


def test_check_live_with_a_failed_check_exits_with_one(oil, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Bad", "--setting", "mode=fail") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "check", "Bad", "--live")

    assert code == 1
    assert out.splitlines() == [
        "Bad (fakeconn)",
        "  [ok] plugin loaded",
        "  [ok] auth: signed in as Alex",
        "  [FAIL] folder: not found",
    ]


def test_check_live_reports_an_exception_of_the_connector(oil, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Boom", "--setting", "mode=raise") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "check", "Boom", "--live")

    assert code == 1
    assert out.splitlines() == [
        "Boom (fakeconn)",
        "  [ok] plugin loaded",
        "  [FAIL] live check: RuntimeError: boom",
    ]


def test_check_live_covers_all_connectors_and_one_failure_does_not_hide_the_others(oil, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "A-fine") == 0
    assert oil("connector", "add", "fakeconn", "B-boom", "--setting", "mode=raise") == 0
    assert oil("connector", "add", "fakeconn", "C-fine") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "check", "--live")

    assert code == 1
    assert FakeConnector.checks_run == ["A-fine", "B-boom", "C-fine"]
    assert "C-fine (fakeconn)" in out.splitlines()


def test_check_of_an_unknown_connector_is_rejected(oil, capsys) -> None:
    code, out, err = run(oil, capsys, "connector", "check", "Nope")

    assert code == 1
    assert out == ""
    assert "connector Nope not found" in err


def test_check_writes_no_audit_entry(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()

    assert oil("connector", "check", "--live") == 0

    assert [entry.action for entry in audit_entries(db)] == ["connector.create"]


# --- oil connector enable / disable ----------------------------------------------------------------------


def test_disable_and_enable_audit_and_change_what_check_and_list_show(oil, db, capsys) -> None:
    assert oil("connector", "add", "fakeconn", "Fake") == 0
    capsys.readouterr()

    code, out, _ = run(oil, capsys, "connector", "disable", "Fake")
    assert (code, out) == (0, "Disabled connector Fake\n")
    assert connectors(db)[0].enabled is False

    code, out, _ = run(oil, capsys, "connector", "check", "Fake", "--live")
    assert (code, out.splitlines()) == (0, ["Fake (fakeconn)", "  [--] disabled"])
    assert FakeConnector.checks_run == []
    _, listed, _ = run(oil, capsys, "connector", "list")
    assert listed.splitlines()[0] == "1  Fake  fakeconn  disabled  credential: -"

    code, out, _ = run(oil, capsys, "connector", "enable", "1")
    assert (code, out) == (0, "Enabled connector Fake\n")
    assert connectors(db)[0].enabled is True

    entries = audit_entries(db, "connector.")
    assert [(e.action, e.action_class, e.actor_type, e.target) for e in reversed(entries)] == [
        ("connector.create", "B", "user", "Fake"),
        ("connector.disable", "B", "user", "Fake"),
        ("connector.enable", "B", "user", "Fake"),
    ]


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_enable_and_disable_of_an_unknown_connector_are_rejected(oil, capsys, command: str) -> None:
    code, _, err = run(oil, capsys, "connector", command, "Nope")

    assert code == 1
    assert "connector Nope not found" in err
