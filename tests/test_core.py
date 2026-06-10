"""Unit tests for the dependency-light core logic (no browser required).

Run with:  python -m pytest -q     (or)     python tests/test_core.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tenfore import timeparse as tp
from tenfore.booker import Slot, TenforeBooker, _extract_time
from tenfore.config import BookingConfig, load_config

_BASE_YAML = """
booking:
  desired_date: "2026-06-14"
  time_window: {{start: "07:00", end: "09:00"}}
  players: 3
  courses: {courses}
  priority: "{priority}"
scheduling:
  unlock_days_before: 8
  unlock_time: "00:00"
  timezone: "UTC"
"""


# --------------------------------------------------------------------- date parsing
@pytest.mark.parametrize("text,expected", [
    ("Sunday, June 14", dt.date(2026, 6, 14)),   # June 14 2026 IS a Sunday
    ("2026-06-14", dt.date(2026, 6, 14)),
    ("June 14", dt.date(2026, 6, 14)),
    ("6/14", dt.date(2026, 6, 14)),
    ("14 June 2026", dt.date(2026, 6, 14)),
])
def test_parse_date(text, expected):
    assert tp.parse_date(text, today=dt.date(2026, 6, 7)) == expected


def test_parse_date_infers_next_year_when_past():
    # Jan 1 is behind a June "today" -> should roll to next year.
    assert tp.parse_date("January 1", today=dt.date(2026, 6, 7)) == dt.date(2027, 1, 1)


def test_parse_date_weekday_mismatch_raises():
    # June 14, 2026 is a Sunday; naming Saturday is a typo and must error.
    with pytest.raises(tp.ParseError):
        tp.parse_date("Saturday, June 14", today=dt.date(2026, 6, 7))


@pytest.mark.parametrize("text,expected", [
    ("saturday", dt.date(2026, 6, 13)),       # today is Sun Jun 7 -> upcoming Sat
    ("next saturday", dt.date(2026, 6, 13)),
    ("this saturday", dt.date(2026, 6, 13)),
])
def test_parse_weekday_only(text, expected):
    assert tp.parse_date(text, today=dt.date(2026, 6, 7)) == expected


def test_parse_weekday_today_rolls_forward():
    # "sunday" when today is Sunday -> next week's Sunday, never same-day.
    assert tp.parse_date("sunday", today=dt.date(2026, 6, 7)) == dt.date(2026, 6, 14)


def test_parse_date_invalid():
    with pytest.raises(tp.ParseError):
        tp.parse_date("not a date")


# --------------------------------------------------------------------- time parsing
@pytest.mark.parametrize("text,expected", [
    ("7:00 AM", dt.time(7, 0)),
    ("07:00", dt.time(7, 0)),
    ("0730", dt.time(7, 30)),
    ("9pm", dt.time(21, 0)),
    ("12:00 AM", dt.time(0, 0)),
    ("12:00 PM", dt.time(12, 0)),
])
def test_parse_time(text, expected):
    assert tp.parse_time(text) == expected


@pytest.mark.parametrize("text", [
    "between 7:00 AM and 9:00 AM",
    "07:00-09:00",
    "7:00 to 9:00",
])
def test_parse_time_window(text):
    assert tp.parse_time_window(text) == (dt.time(7, 0), dt.time(9, 0))


# --------------------------------------------------------------- slot text extraction
@pytest.mark.parametrize("text,expected", [
    ("Book 07:20 AM 4 spots", dt.time(7, 20)),
    ("13:40 Available", dt.time(13, 40)),
    ("No time here", None),
    ("7:00am - Tee 1", dt.time(7, 0)),
])
def test_extract_time(text, expected):
    assert _extract_time(text) == expected


# --------------------------------------------------------------------- slot selection
def _config(**overrides) -> BookingConfig:
    base = dict(
        desired_date=dt.date(2026, 6, 14),
        window_start=dt.time(7, 0), window_end=dt.time(9, 0),
        players=2, courses=[], priority="time",
        unlock_days_before=8, unlock_time=dt.time(0, 0),
        timezone=ZoneInfo("UTC"), prefire_seconds=60, poll_interval_seconds=1.5,
        selection_budget_seconds=6,
        login_attempts=10, login_delay_seconds=2, booking_window_minutes=5,
        fallback_to_next_best=True, headless=True, timeout_ms=30000,
        macos_desktop=False, log_dir=Path("logs"), screenshot_dir=Path("screenshots"),
        base_url="https://x", raw={},
    )
    base.update(overrides)
    return BookingConfig(**base)


def _booker(config: BookingConfig) -> TenforeBooker:
    b = TenforeBooker.__new__(TenforeBooker)
    b.config = config
    return b


def test_choose_earliest_in_window():
    b = _booker(_config())
    slots = [Slot(dt.time(6, 40), "6:40", 0),
             Slot(dt.time(7, 20), "7:20", 1),
             Slot(dt.time(8, 0), "8:00", 2)]
    slot, in_window = b.choose_slot(slots)
    assert in_window and slot.time == dt.time(7, 20)


def test_fallback_after_window():
    b = _booker(_config())
    slots = [Slot(dt.time(9, 30), "9:30", 0), Slot(dt.time(10, 0), "10:00", 1)]
    slot, in_window = b.choose_slot(slots)
    assert not in_window and slot.time == dt.time(9, 30)


def test_fallback_disabled_returns_none():
    b = _booker(_config(fallback_to_next_best=False))
    slots = [Slot(dt.time(10, 0), "10:00", 0)]
    slot, in_window = b.choose_slot(slots)
    assert slot is None and not in_window


def test_course_priority_always_falls_back():
    # Time is flexible under course priority -> book any time even with
    # fallback_to_next_best disabled.
    b = _booker(_config(priority="course", fallback_to_next_best=False))
    slots = [Slot(dt.time(10, 0), "10:00", 0, course="Fox Hollow")]
    slot, in_window = b.choose_slot(slots)
    assert slot is not None and not in_window and slot.course == "Fox Hollow"


def test_time_priority_earliest_across_courses_wins():
    # With slots from two courses, the earliest in-window time wins regardless
    # of course; ties already resolved by gather() ordering.
    b = _booker(_config(priority="time"))
    slots = [Slot(dt.time(7, 40), "7:40", 0, course="Eagle Ridge"),
             Slot(dt.time(7, 10), "7:10", 1, course="Fox Hollow")]
    slots.sort(key=lambda s: s.time)
    slot, in_window = b.choose_slot(slots)
    assert in_window and slot.time == dt.time(7, 10) and slot.course == "Fox Hollow"


# --------------------------------------------------------------- courses_to_search
def test_courses_to_search_course_priority_uses_primary():
    cfg = _config(priority="course", courses=["Fox Hollow", "Eagle Ridge"])
    assert cfg.primary_course == "Fox Hollow"
    assert cfg.courses_to_search() == ["Fox Hollow"]
    assert cfg.course_flexible is False


def test_courses_to_search_time_priority_uses_all():
    cfg = _config(priority="time", courses=["Fox Hollow", "Eagle Ridge"])
    assert cfg.courses_to_search() == ["Fox Hollow", "Eagle Ridge"]
    assert cfg.course_flexible is True


def test_courses_to_search_empty_yields_single_none():
    cfg = _config(priority="time", courses=[])
    assert cfg.courses_to_search() == [None]
    assert cfg.primary_course is None


# --------------------------------------------------------------- unlock datetime math
def test_unlock_datetime():
    cfg = _config(desired_date=dt.date(2026, 6, 14), unlock_days_before=8,
                  unlock_time=dt.time(0, 0), timezone=ZoneInfo("America/New_York"))
    unlock = cfg.unlock_datetime
    assert unlock.date() == dt.date(2026, 6, 6)
    assert (unlock.hour, unlock.minute) == (0, 0)
    assert unlock.tzinfo is not None


# ----------------------------------------------------------------- load_config
def _write_yaml(tmp_path, courses="[]", priority="time"):
    p = tmp_path / "config.yaml"
    p.write_text(_BASE_YAML.format(courses=courses, priority=priority))
    return p


def test_load_config_reads_courses_and_priority(tmp_path):
    cfg = load_config(_write_yaml(tmp_path, '["Fox Hollow", "Eagle Ridge"]', "course"))
    assert cfg.courses == ["Fox Hollow", "Eagle Ridge"]
    assert cfg.priority == "course"
    assert cfg.players == 3


def test_load_config_cli_overrides_courses_and_priority(tmp_path):
    cfg = load_config(_write_yaml(tmp_path, '["Fox Hollow"]', "course"),
                      overrides={"courses": ["A", "B"], "priority": "time"})
    assert cfg.courses == ["A", "B"] and cfg.priority == "time"


def test_load_config_rejects_bad_priority(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write_yaml(tmp_path, "[]", "whatever"))


def test_load_config_honors_prefire_zero_override(tmp_path):
    # 0 is a legitimate override and must not be swallowed as falsy.
    cfg = load_config(_write_yaml(tmp_path), overrides={"prefire_seconds": 0})
    assert cfg.prefire_seconds == 0


# ----------------------------------------------------------------- setup wizard
def test_env_quote_roundtrips_special_chars(tmp_path):
    from dotenv import dotenv_values
    from tenfore.setup_wizard import _write_env

    email = "chip+golf@example.com"
    password = "p@ss #word$ with 'quotes' and \\slash"
    env_path = tmp_path / ".env"
    _write_env(env_path, email, password)
    values = dotenv_values(env_path)
    assert values["TENFORE_EMAIL"] == email
    assert values["TENFORE_PASSWORD"] == password


def test_merged_scheduling_preserves_unprompted_keys():
    from tenfore.setup_wizard import _merged_scheduling

    existing = {"selection_budget_seconds": 9, "poll_interval_seconds": 2.0,
                "prefire_seconds": 30}
    merged = _merged_scheduling(existing, {"unlock_days_before": 8,
                                           "prefire_seconds": 60})
    assert merged["selection_budget_seconds"] == 9      # carried through
    assert merged["poll_interval_seconds"] == 2.0       # carried through
    assert merged["prefire_seconds"] == 60              # answer wins
    assert merged["unlock_days_before"] == 8


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
