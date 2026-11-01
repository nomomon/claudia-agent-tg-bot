"""Tests for the minimal cron parser and next-run calculation."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from claudia_channels.cron import parse_cron

TZ = ZoneInfo("UTC")


def _at(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


@pytest.mark.parametrize(
    ("expression", "now", "expected"),
    [
        # Daily at 09:00, now past it -> tomorrow.
        ("0 9 * * *", _at(2026, 9, 9, 14, 3), _at(2026, 9, 10, 9, 0)),
        # Daily at 09:00, now before it -> today.
        ("0 9 * * *", _at(2026, 9, 9, 8, 59), _at(2026, 9, 9, 9, 0)),
        # Steps.
        ("*/15 * * * *", _at(2026, 9, 9, 14, 3), _at(2026, 9, 9, 14, 15)),
        # Ranges: weekdays only, Friday evening -> Monday.
        ("0 8 * * 1-5", _at(2026, 9, 11, 9, 0), _at(2026, 9, 14, 8, 0)),
        # Day of month rollover.
        ("30 8 1 * *", _at(2026, 9, 9, 14, 3), _at(2026, 10, 1, 8, 30)),
        # Lists.
        ("0 9,18 * * *", _at(2026, 9, 9, 14, 3), _at(2026, 9, 9, 18, 0)),
        # Sunday as 0 and as 7.
        ("0 12 * * 0", _at(2026, 9, 9, 14, 3), _at(2026, 9, 13, 12, 0)),
        ("0 12 * * 7", _at(2026, 9, 9, 14, 3), _at(2026, 9, 13, 12, 0)),
        # A match exactly one minute ahead is still in the future.
        ("* * * * *", _at(2026, 9, 9, 14, 3), _at(2026, 9, 9, 14, 4)),
    ],
)
def test_next_after(expression: str, now: datetime, expected: datetime) -> None:
    assert parse_cron(expression).next_after(now) == expected


def test_day_of_month_and_day_of_week_are_or_ed() -> None:
    """Standard cron: when both day fields are restricted they are OR-ed."""
    schedule = parse_cron("0 12 13 * 5")  # the 13th, or any Friday
    # 2026-09-11 is a Friday, 2026-09-13 is the 13th.
    assert schedule.next_after(_at(2026, 9, 9, 14, 3)) == _at(2026, 9, 11, 12, 0)
    assert schedule.next_after(_at(2026, 9, 11, 12, 0)) == _at(2026, 9, 13, 12, 0)


def test_seconds_are_truncated() -> None:
    assert parse_cron("*/5 * * * *").next_after(
        datetime(2026, 9, 9, 14, 4, 59, tzinfo=TZ)
    ) == _at(2026, 9, 9, 14, 5)


@pytest.mark.parametrize(
    "expression",
    [
        "bad",
        "0 9 * *",
        "0 9 * * * *",
        "60 9 * * *",
        "0 24 * * *",
        "0 9 0 * *",
        "0 9 * 13 *",
        "0 9 * * 8",
        "*/0 * * * *",
        "9-5 * * * *",
        "a * * * *",
    ],
)
def test_invalid_expressions(expression: str) -> None:
    with pytest.raises(ValueError):
        parse_cron(expression)


def test_expression_is_normalized() -> None:
    assert parse_cron("  0   9  *  *  * ").expression == "0 9 * * *"
