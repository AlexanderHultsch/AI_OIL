from datetime import date

import pytest

from ai_oil.workdays import add_working_days, is_working_day, working_days_between

FRI = date(2026, 10, 23)
SAT = date(2026, 10, 24)
SUN = date(2026, 10, 25)
MON = date(2026, 10, 26)
WED = date(2026, 10, 21)


def test_is_working_day() -> None:
    assert is_working_day(FRI)
    assert is_working_day(MON)
    assert not is_working_day(SAT)
    assert not is_working_day(SUN)


def test_working_days_between_over_weekend() -> None:
    assert working_days_between(FRI, MON) == 1


def test_working_days_between_same_day_is_zero() -> None:
    assert working_days_between(MON, MON) == 0


def test_working_days_between_end_before_start_is_zero() -> None:
    assert working_days_between(MON, FRI) == 0


def test_working_days_between_excludes_start_includes_end() -> None:
    assert working_days_between(WED, FRI) == 2
    assert working_days_between(SAT, SUN) == 0
    assert working_days_between(SAT, MON) == 1


def test_working_days_between_full_week() -> None:
    assert working_days_between(WED, date(2026, 10, 28)) == 5


def test_add_working_days_from_friday() -> None:
    assert add_working_days(FRI, 1) == MON


def test_add_working_days_from_saturday() -> None:
    assert add_working_days(SAT, 1) == MON


def test_add_zero_returns_start() -> None:
    assert add_working_days(SAT, 0) == SAT
    assert add_working_days(WED, 0) == WED


def test_add_negative_raises() -> None:
    with pytest.raises(ValueError):
        add_working_days(WED, -1)


def test_add_five_from_wednesday() -> None:
    assert add_working_days(WED, 5) == date(2026, 10, 28)
