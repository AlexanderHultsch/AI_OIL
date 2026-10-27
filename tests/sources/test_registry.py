import importlib.metadata
from collections.abc import Iterator
from typing import ClassVar

import pytest
import sqlalchemy

from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.governance.classes import action_class_of
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


class FakeConnector:
    type: ClassVar[str] = "fake"
    object_types: ClassVar[frozenset[str]] = frozenset({"note"})
    action_class: ClassVar[str] = "A"

    def __init__(self, config: ConnectorConfig, **backends: object) -> None:
        self.config = config
        self.backends = backends

    def check(self) -> HealthReport:
        return HealthReport((HealthCheck("reachable", True, "ok"),))

    def parse_reference(self, text: str) -> SourceRef | None:
        if text.startswith("fake:"):
            return SourceRef("note", text.removeprefix("fake:"))
        return None

    def resolve(self, ref: SourceRef) -> ObjectMeta:
        return ObjectMeta(ref.object_type, ref.external_id)

    def fetch(self, state: ObjectState) -> FetchResult:
        return FetchResult()

    def search_candidates(self, query: CandidateQuery) -> list[ObjectMeta]:
        return []


@pytest.fixture
def fake_registered() -> Iterator[type]:
    registry.register_connector_type(FakeConnector)
    yield FakeConnector
    registry.unregister_connector_type("fake")


@pytest.fixture
def no_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **kwargs: [])


def test_register_returns_class_and_is_available(fake_registered: type) -> None:
    assert registry.available_connector_types()["fake"] is FakeConnector
    assert registry.get_connector_class("fake") is FakeConnector


def test_register_works_as_decorator() -> None:
    try:

        @registry.register_connector_type
        class Decorated(FakeConnector):
            type = "decorated"

        assert registry.get_connector_class("decorated") is Decorated
    finally:
        registry.unregister_connector_type("decorated")


def test_unregister_removes_and_tolerates_unknown(fake_registered: type) -> None:
    registry.unregister_connector_type("fake")
    assert "fake" not in registry.available_connector_types()
    registry.unregister_connector_type("fake")


def test_unknown_type_lists_available_types(fake_registered: type, no_entry_points: None) -> None:
    with pytest.raises(NotFoundError, match="unknown connector type: nope.*fake"):
        registry.get_connector_class("nope")


def test_registered_overrides_entry_point(monkeypatch: pytest.MonkeyPatch, fake_registered: type) -> None:
    class Other:
        type = "fake"

    class EntryPoint:
        name = "fake"

        def load(self) -> type:
            return Other

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **kwargs: [EntryPoint()])
    assert registry.available_connector_types()["fake"] is FakeConnector


def test_entry_point_is_loaded_by_group_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class EntryPoint:
        name = "viaentry"

        def load(self) -> type:
            return FakeConnector

    seen: list[dict] = []

    def entry_points(**kwargs: object) -> list[EntryPoint]:
        seen.append(kwargs)
        return [EntryPoint()]

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", entry_points)
    assert registry.available_connector_types() == {"viaentry": FakeConnector}
    assert seen == [{"group": "ai_oil.connectors"}]


def test_failing_entry_point_is_skipped_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        name = "broken"

        def load(self) -> type:
            raise ImportError("no module named pywin32")

    class Working:
        name = "working"

        def load(self) -> type:
            return FakeConnector

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **kwargs: [Broken(), Working()])
    available = registry.available_connector_types()
    assert set(available) == {"working"}
    assert "pywin32" in registry.load_errors["broken"]

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **kwargs: [Working()])
    registry.available_connector_types()
    assert registry.load_errors == {}


def test_real_entry_point_lookup_does_not_fail() -> None:
    assert isinstance(registry.available_connector_types(), dict)
    assert importlib.metadata.entry_points(group=registry.ENTRY_POINT_GROUP) is not None


def test_create_and_get_by_id_and_name(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(
        conn, "fake", " Fake one ", settings={"b": 2, "a": 1}, credential_ref="ai_oil/fake"
    )
    assert row == registry.ConnectorRow(
        id=row.id,
        type="fake",
        name="Fake one",
        enabled=True,
        settings={"a": 1, "b": 2},
        credential_ref="ai_oil/fake",
        created_at=row.created_at,
    )
    assert registry.get_connector(conn, row.id) == row
    assert registry.get_connector(conn, str(row.id)) == row
    assert registry.get_connector(conn, "Fake one") == row
    with pytest.raises(NotFoundError):
        registry.get_connector(conn, "fake one")
    with pytest.raises(NotFoundError):
        registry.get_connector(conn, 999)
    with pytest.raises(NotFoundError):
        registry.get_connector(conn, "999")


def test_settings_are_stored_sorted(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake", settings={"b": 2, "a": 1})
    stored = conn.execute(
        sqlalchemy.text("SELECT settings_json FROM connector WHERE id = :id"), {"id": row.id}
    ).scalar_one()
    assert stored == '{"a": 1, "b": 2}'


def test_create_defaults_to_empty_settings(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake")
    assert row.settings == {}
    assert row.credential_ref is None


def test_create_unknown_type_rejected(conn: sqlalchemy.Connection, no_entry_points: None) -> None:
    with pytest.raises(NotFoundError, match="unknown connector type"):
        registry.create_connector(conn, "nope", "X")


def test_blank_name_rejected(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    with pytest.raises(ValidationError, match="name"):
        registry.create_connector(conn, "fake", "   ")


def test_duplicate_name_rejected(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    registry.create_connector(conn, "fake", "Fake")
    with pytest.raises(ConflictError):
        registry.create_connector(conn, "fake", "Fake")


def test_several_instances_of_one_type_allowed(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    registry.create_connector(conn, "fake", "Zeta")
    registry.create_connector(conn, "fake", "Alpha")
    assert [r.name for r in registry.list_connectors(conn)] == ["Alpha", "Zeta"]
    assert [r.name for r in registry.list_connectors(conn, type_name="fake")] == ["Alpha", "Zeta"]
    assert registry.list_connectors(conn, type_name="other") == []


@pytest.mark.parametrize("key", ["api_token", "Password", "client_SECRET", "tokenValue"])
def test_secret_looking_settings_rejected(
    conn: sqlalchemy.Connection, fake_registered: type, key: str
) -> None:
    with pytest.raises(ValidationError, match="GOV-8"):
        registry.create_connector(conn, "fake", "Fake", settings={key: "x"})
    row = registry.create_connector(conn, "fake", "Fake")
    with pytest.raises(ValidationError, match="GOV-8"):
        registry.update_settings(conn, row.id, {key: "x"})
    assert registry.get_connector(conn, row.id).settings == {}


def test_set_enabled(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake")
    assert registry.set_enabled(conn, "Fake", False).enabled is False
    assert registry.get_connector(conn, row.id).enabled is False
    assert registry.set_enabled(conn, row.id, True).enabled is True
    with pytest.raises(NotFoundError):
        registry.set_enabled(conn, "missing", True)


def test_update_settings_replaces(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake", settings={"a": 1})
    updated = registry.update_settings(conn, "Fake", {"b": 2})
    assert updated.settings == {"b": 2}
    assert updated.id == row.id
    with pytest.raises(NotFoundError):
        registry.update_settings(conn, "missing", {})


def test_instantiate_passes_config_and_backends(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake", settings={"a": 1}, credential_ref="ref")
    backend = object()
    instance = registry.instantiate(row, http=backend)
    assert isinstance(instance, FakeConnector)
    assert instance.config == ConnectorConfig(row.id, "fake", "Fake", True, {"a": 1}, "ref")
    assert instance.backends == {"http": backend}


def test_match_reference_skips_disabled_and_keeps_order(
    conn: sqlalchemy.Connection, fake_registered: type
) -> None:
    first = registry.create_connector(conn, "fake", "First")
    second = registry.create_connector(conn, "fake", "Second")
    third = registry.create_connector(conn, "fake", "Third")
    registry.set_enabled(conn, second.id, False)
    second = registry.get_connector(conn, second.id)
    instances = [(row, registry.instantiate(row)) for row in (third, second, first)]
    matches = registry.match_reference(instances, "fake:42")
    assert [(row.name, ref.external_id) for row, ref in matches] == [("Third", "42"), ("First", "42")]
    assert registry.match_reference(instances, "other:42") == []


def test_match_reference_propagates_connector_errors(
    conn: sqlalchemy.Connection, fake_registered: type
) -> None:
    class Exploding(FakeConnector):
        def parse_reference(self, text: str) -> SourceRef | None:
            raise RuntimeError("boom")

    row = registry.create_connector(conn, "fake", "Fake")
    with pytest.raises(RuntimeError, match="boom"):
        registry.match_reference([(row, Exploding(registry.to_config(row)))], "x")


def test_action_class_of_fake_connector(conn: sqlalchemy.Connection, fake_registered: type) -> None:
    row = registry.create_connector(conn, "fake", "Fake")
    assert action_class_of(registry.instantiate(row)) == "A"
