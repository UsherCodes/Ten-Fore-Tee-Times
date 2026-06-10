"""Lenient parsing of human-entered dates and times.

Kept dependency-free (no dateutil) so the tool stays lightweight. Handles the
phrasings a golfer would actually type: "Saturday, June 14", "June 14",
"2026-06-14", "next Saturday", "7:00 AM", "0700", "9", etc.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

_WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
_WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


class ParseError(ValueError):
    """Raised when a date or time string can't be understood."""


def parse_date(text: str, *, today: dt.date | None = None) -> dt.date:
    """Parse a human date string into a concrete future-leaning date.

    The year is inferred when omitted: the nearest occurrence that is not in
    the past relative to ``today``.
    """
    today = today or dt.date.today()
    raw = text.strip()
    if not raw:
        raise ParseError("empty date")

    lowered = raw.lower()

    # ISO / numeric: 2026-06-14, 2026/6/14, 06-14-2026, 6/14
    iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", lowered)
    if iso:
        y, m, d = (int(g) for g in iso.groups())
        return _make_date(y, m, d)

    mdy = re.match(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?$", lowered)
    if mdy:
        m, d, y = mdy.groups()
        m, d = int(m), int(d)
        if y is None:
            return _nearest_future(today, lambda yr: _make_date(yr, m, d))
        y = int(y)
        if y < 100:
            y += 2000
        return _make_date(y, m, d)

    # "next saturday" / "this friday" / bare weekday "saturday"
    wd_match = re.match(r"^(?:next\s+|this\s+|coming\s+)?([a-z]+)$", lowered)
    if wd_match and wd_match.group(1) in _WEEKDAYS:
        target = _WEEKDAYS[wd_match.group(1)]
        ahead = (target - today.weekday()) % 7
        # Bare/"this"/"next" all mean the upcoming occurrence; if that is today,
        # roll forward a week so we never return a same-day (already-open) date.
        if ahead == 0:
            ahead = 7
        return today + dt.timedelta(days=ahead)

    # "Saturday, June 14" / "June 14" / "14 June" / "Jun 14 2026"
    # Capture the leading weekday name (if any) so we can sanity-check it later,
    # then strip it before parsing the month/day.
    first_token = lowered.split()[0].rstrip(",")
    stated_weekday = _WEEKDAYS.get(first_token) if first_token in _WEEKDAYS else None
    cleaned = re.sub(r"^[a-z]+(?:,|\s)+\s*", "", lowered) if stated_weekday is not None else lowered
    tokens = re.findall(r"[a-z]+|\d+", cleaned)

    month = day = year = None
    for tok in tokens:
        if tok in _MONTHS and month is None:
            month = _MONTHS[tok]
        elif tok.isdigit():
            n = int(tok)
            if n > 31 and year is None:
                year = n
            elif day is None:
                day = n
            elif year is None:
                year = n if n > 31 else 2000 + n

    if month and day:
        if year:
            result = _make_date(year, month, day)
        else:
            result = _nearest_future(today, lambda yr: _make_date(yr, month, day))
        # If the user named a weekday, make sure it matches — a mismatch means a
        # typo in either the weekday or the day number, which would silently book
        # the wrong day.
        if stated_weekday is not None and result.weekday() != stated_weekday:
            raise ParseError(
                f"{result.strftime('%B %d, %Y')} is a {calendar.day_name[result.weekday()]}, "
                f"not a {calendar.day_name[stated_weekday]}. Check the date you entered."
            )
        return result

    raise ParseError(f"could not understand date: {text!r}")


def parse_time(text: str) -> dt.time:
    """Parse a clock time. Accepts '7:00 AM', '07:00', '0700', '9', '9pm'."""
    raw = text.strip().lower()
    if not raw:
        raise ParseError("empty time")

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", raw)
    if not m:
        # bare military like 0730
        digits = re.match(r"^(\d{2})(\d{2})$", raw)
        if digits:
            h, mi = int(digits.group(1)), int(digits.group(2))
            return _make_time(h, mi)
        raise ParseError(f"could not understand time: {text!r}")

    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return _make_time(hour, minute)


def parse_time_window(text: str) -> tuple[dt.time, dt.time]:
    """Parse 'between 7:00 AM and 9:00 AM' or '7:00-9:00' into (start, end)."""
    raw = text.strip().lower()
    raw = re.sub(r"^between\s+", "", raw)
    parts = re.split(r"\s*(?:-|to|and|–|—)\s*", raw, maxsplit=1)
    if len(parts) != 2:
        raise ParseError(f"could not split time window: {text!r}")
    return parse_time(parts[0]), parse_time(parts[1])


def _make_date(y: int, m: int, d: int) -> dt.date:
    try:
        return dt.date(y, m, d)
    except ValueError as exc:
        raise ParseError(str(exc)) from exc


def _make_time(h: int, mi: int) -> dt.time:
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ParseError(f"time out of range: {h:02d}:{mi:02d}")
    return dt.time(h, mi)


def _nearest_future(today: dt.date, builder) -> dt.date:
    """Return builder(year) for the smallest year giving a date >= today."""
    for year in (today.year, today.year + 1):
        try:
            candidate = builder(year)
        except ParseError:
            continue
        if candidate >= today:
            return candidate
    return builder(today.year + 1)
