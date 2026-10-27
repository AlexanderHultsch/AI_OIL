"""Working-day arithmetic: Monday to Friday, no holidays (spec 5.7, AST-9)."""

from datetime import date, timedelta


def is_working_day(day: date) -> bool:
    """True for Monday to Friday."""
    return day.weekday() < 5


def working_days_between(start: date, end: date) -> int:
    """Number of working days d with start < d <= end; 0 if end <= start."""
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if is_working_day(day):
            count += 1
    return count


def add_working_days(start: date, n: int) -> date:
    """The n-th working day after `start`; `start` itself for n == 0."""
    if n < 0:
        raise ValueError("n must not be negative")
    day = start
    remaining = n
    while remaining > 0:
        day += timedelta(days=1)
        if is_working_day(day):
            remaining -= 1
    return day
