"""Minimal 5-field cron parser and next-run calculator. No external dependencies.

Supports the standard `minute hour day-of-month month day-of-week` syntax with `*`,
`*/step`, `a-b`, `a-b/step` and comma-separated lists. Day-of-week accepts 0-7
(both 0 and 7 mean Sunday).

All calculations are done in the timezone of the datetime passed in, at minute
resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

# Give up rather than loop forever on expressions that never match (e.g. "0 0 30 2 *").
MAX_LOOKAHEAD_DAYS = 366 * 4


def _parse_field(raw: str, low: int, high: int, name: str) -> set[int]:
    """Expand one cron field into the set of values it matches."""
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty {name} field in cron expression")

        step = 1
        if "/" in part:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) == 0:
                raise ValueError(f"Invalid step '{step_raw}' in {name} field")
            step = int(step_raw)

        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start_raw, _, end_raw = part.partition("-")
            start, end = _to_int(start_raw, name), _to_int(end_raw, name)
        else:
            start = end = _to_int(part, name)

        if start < low or end > high or start > end:
            raise ValueError(f"Value out of range in {name} field: '{raw}'")

        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"No values matched in {name} field: '{raw}'")
    return values


def _to_int(raw: str, name: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError(f"Invalid value '{raw}' in {name} field")
    return int(raw)


@dataclass(frozen=True)
class CronSchedule:
    """A parsed cron expression."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def _day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        # cron weekday: 0 = Sunday; datetime.weekday(): 0 = Monday
        dow = (moment.weekday() + 1) % 7
        dom_ok = moment.day in self.days_of_month
        dow_ok = dow in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            # Standard cron semantics: restricted day fields are OR-ed.
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, moment: datetime) -> bool:
        """True if *moment* (minute resolution) satisfies the expression."""
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and self._day_matches(moment)
        )

    def next_after(self, moment: datetime) -> datetime:
        """Return the first matching moment strictly after *moment*."""
        candidate = (moment + timedelta(minutes=1)).replace(second=0, microsecond=0)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)

        for day_offset in range(MAX_LOOKAHEAD_DAYS):
            day_start = candidate if day_offset == 0 else None
            day = (candidate + timedelta(days=day_offset)).date()
            probe = datetime.combine(day, time(0, 0), tzinfo=candidate.tzinfo)
            if not self._day_matches(probe):
                continue
            for hour in hours:
                if day_start is not None and hour < day_start.hour:
                    continue
                for minute in minutes:
                    if (
                        day_start is not None
                        and hour == day_start.hour
                        and minute < day_start.minute
                    ):
                        continue
                    return datetime.combine(day, time(hour, minute), tzinfo=candidate.tzinfo)

        raise ValueError(f"Cron expression never matches: '{self.expression}'")


def parse_cron(expression: str) -> CronSchedule:
    """Parse a 5-field cron expression. Raises ValueError if malformed."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            "Cron expression must have 5 fields "
            "(minute hour day-of-month month day-of-week), "
            f"got {len(fields)}: '{expression}'"
        )

    parsed = [
        _parse_field(raw, low, high, name)
        for raw, (low, high), name in zip(fields, FIELD_RANGES, FIELD_NAMES, strict=True)
    ]
    minutes, hours, days_of_month, months, days_of_week = parsed
    # 7 and 0 both mean Sunday.
    if 7 in days_of_week:
        days_of_week.discard(7)
        days_of_week.add(0)

    return CronSchedule(
        expression=" ".join(fields),
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days_of_month=frozenset(days_of_month),
        months=frozenset(months),
        days_of_week=frozenset(days_of_week),
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
    )
