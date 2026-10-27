import dataclasses

import pytest

from ai_oil.sources.model import (
    ENTRY_TYPES,
    OBJECT_TYPES,
    EntryData,
    FetchResult,
    HealthCheck,
    HealthReport,
    IdentityRef,
    ObjectMeta,
    SourceRef,
)


def test_fetch_result_defaults_are_ok_and_empty() -> None:
    result = FetchResult()
    assert result.status == "ok"
    assert result.version is None
    assert result.entries == ()
    assert result.children == ()
    assert result.cursor is None
    assert result.title is None
    assert result.warnings == ()


def test_mapping_defaults_are_not_shared() -> None:
    first = ObjectMeta("note", "1")
    second = ObjectMeta("note", "2")
    assert first.extra == {}
    assert first.extra is not second.extra


def test_fetch_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="status"):
        FetchResult(status="error")
    assert FetchResult(status="gone").status == "gone"


@pytest.mark.parametrize("cls", [SourceRef, ObjectMeta])
def test_object_type_is_validated(cls: type) -> None:
    with pytest.raises(ValueError, match="object type"):
        cls("mailbox", "1")
    for object_type in OBJECT_TYPES:
        assert cls(object_type, "1").object_type == object_type


def test_entry_type_is_validated() -> None:
    with pytest.raises(ValueError, match="entry type"):
        EntryData("tweet", "1", "2026-01-01T00:00:00Z", "text")
    for entry_type in ENTRY_TYPES:
        assert EntryData(entry_type, "1", "2026-01-01T00:00:00Z", "text").entry_type == entry_type


def test_entry_direction_is_validated() -> None:
    with pytest.raises(ValueError, match="direction"):
        EntryData("email_message", "1", "2026-01-01T00:00:00Z", "text", direction="sideways")
    for direction in (None, "in", "out"):
        entry = EntryData("email_message", "1", "2026-01-01T00:00:00Z", "text", direction=direction)
        assert entry.direction == direction


def test_identity_system_is_validated() -> None:
    with pytest.raises(ValueError, match="system"):
        IdentityRef("slack", "u1")
    for system in ("email", "atlassian", "teams"):
        assert IdentityRef(system, "u1").display_name is None


def test_health_report_ok_semantics() -> None:
    assert HealthReport(()).ok is True
    assert HealthReport((HealthCheck("a", True, ""), HealthCheck("b", True, ""))).ok is True
    assert HealthReport((HealthCheck("a", True, ""), HealthCheck("b", False, "down"))).ok is False


def test_dataclasses_are_frozen() -> None:
    meta = ObjectMeta("note", "1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.title = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        FetchResult().status = "gone"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        HealthCheck("a", True, "").ok = False  # type: ignore[misc]
