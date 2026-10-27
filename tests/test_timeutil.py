from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ai_oil import timeutil


def test_utcnow_iso_truncates_to_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 10, 26, 9, 15, 0, 123999, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "_now", lambda: frozen)
    assert timeutil.utcnow_iso() == "2026-10-26T09:15:00.123Z"


def test_utcnow_iso_pads_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 10, 26, 9, 15, 0, 5000, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "_now", lambda: frozen)
    assert timeutil.utcnow_iso() == "2026-10-26T09:15:00.005Z"


def test_utcnow_iso_converts_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 10, 26, 11, 15, 0, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(timeutil, "_now", lambda: frozen)
    assert timeutil.utcnow_iso() == "2026-10-26T09:15:00.000Z"


def test_parse_iso_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 10, 26, 9, 15, 0, 123000, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "_now", lambda: frozen)
    assert timeutil.parse_iso(timeutil.utcnow_iso()) == frozen


def test_parse_iso_without_milliseconds() -> None:
    assert timeutil.parse_iso("2026-10-26T09:15:00Z") == datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value", ["", "garbage", "2026-10-26", "2026-10-26T09:15:00", "2026-10-26 09:15:00Z"]
)
def test_parse_iso_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        timeutil.parse_iso(value)


def test_today_uses_now(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "_now", lambda: frozen)
    assert timeutil.today() == frozen.astimezone().date()
    assert isinstance(timeutil.today(), date)
