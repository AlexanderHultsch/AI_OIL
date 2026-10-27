"""Time helpers. Tests freeze time by monkeypatching `_now`."""

from datetime import UTC, date, datetime


def _now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Current UTC time as `YYYY-MM-DDTHH:MM:SS.fffZ`, matching the schema defaults (ms truncated)."""
    now = _now().astimezone(UTC)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


def today() -> date:
    """Local calendar date of the current time."""
    return _now().astimezone().date()


def parse_iso(value: str) -> datetime:
    """Parse `utcnow_iso()` output (with or without milliseconds) into an aware UTC datetime."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"not an ISO UTC timestamp: {value!r}")
