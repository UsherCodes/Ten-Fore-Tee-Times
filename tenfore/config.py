"""Configuration loading, CLI overrides, and booking-time math."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from . import timeparse

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")


@dataclass
class Credentials:
    email: str
    password: str


@dataclass
class BookingConfig:
    """Fully-resolved settings for a single booking run."""

    desired_date: dt.date
    window_start: dt.time
    window_end: dt.time
    players: int

    # Ordered list of courses to consider (first = most preferred). May be empty
    # for single-course platforms, in which case no course is selected.
    courses: list[str]
    # "course" -> lock the course, time is flexible.
    # "time"   -> lock the time window, course/location is flexible.
    priority: str

    unlock_days_before: int
    unlock_time: dt.time
    timezone: ZoneInfo
    prefire_seconds: int
    poll_interval_seconds: float
    # Wall-clock cap (seconds) for finding the date/course control, so a missing
    # or unmatched control can't burn polling time during the booking race.
    selection_budget_seconds: float

    login_attempts: int
    login_delay_seconds: float
    booking_window_minutes: float
    fallback_to_next_best: bool

    headless: bool
    timeout_ms: int

    macos_desktop: bool

    log_dir: Path
    screenshot_dir: Path
    base_url: str

    # Runtime-only: when True, do everything except click the final confirm.
    dry_run: bool = False

    raw: dict = field(default_factory=dict, repr=False)

    @property
    def unlock_datetime(self) -> dt.datetime:
        """The exact tz-aware moment the booking window opens.

        unlock_days_before days before the play date, at unlock_time, in the
        configured timezone.
        """
        unlock_date = self.desired_date - dt.timedelta(days=self.unlock_days_before)
        naive = dt.datetime.combine(unlock_date, self.unlock_time)
        return naive.replace(tzinfo=self.timezone)

    def now(self) -> dt.datetime:
        """Current time in the configured timezone."""
        return dt.datetime.now(tz=self.timezone)

    def window_contains(self, t: dt.time) -> bool:
        return self.window_start <= t <= self.window_end

    @property
    def primary_course(self) -> str | None:
        """The single most-preferred course, or None for single-course sites."""
        return self.courses[0] if self.courses else None

    @property
    def course_flexible(self) -> bool:
        """True when we'll search across multiple courses (time priority)."""
        return self.priority == "time"

    def courses_to_search(self) -> list[str | None]:
        """Which courses to scan for slots, given the priority mode.

        - course priority: just the primary course (time is flexible there).
        - time priority:   every listed course (location is flexible).
        Returns ``[None]`` when no courses are configured, so the booking flow
        still runs once against a single-course platform.
        """
        if self.priority == "course":
            target = [self.primary_course] if self.primary_course else []
        else:
            target = list(self.courses)
        return target or [None]


def load_credentials(env_path: Path = DEFAULT_ENV_PATH) -> Credentials:
    """Load credentials from .env / environment. Raises if missing."""
    if env_path.exists():
        load_dotenv(env_path)
    email = os.getenv("TENFORE_EMAIL")
    password = os.getenv("TENFORE_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Missing TENFORE_EMAIL / TENFORE_PASSWORD. "
            "Run `python main.py setup` or create a .env file (see .env.example)."
        )
    return Credentials(email=email, password=password)


def load_config(path: Path, overrides: dict | None = None) -> BookingConfig:
    """Load config.yaml and apply CLI overrides (which take precedence)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Run `python main.py setup` first."
        )
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    booking = raw.get("booking", {})
    scheduling = raw.get("scheduling", {})
    retry = raw.get("retry", {})
    browser = raw.get("browser", {})
    notifications = raw.get("notifications", {})
    paths = raw.get("paths", {})
    site = raw.get("site", {})

    # --- dates & window (with CLI overrides) ---
    tz_name = overrides.get("timezone") or scheduling.get("timezone") or ""
    tz = ZoneInfo(tz_name) if tz_name else _local_zoneinfo()

    today = dt.datetime.now(tz=tz).date()

    date_str = overrides.get("date") or booking.get("desired_date")
    if not date_str:
        raise ValueError("No desired_date in config and no --date provided.")
    desired_date = timeparse.parse_date(str(date_str), today=today)

    if overrides.get("time_window"):
        window_start, window_end = timeparse.parse_time_window(overrides["time_window"])
    else:
        tw = booking.get("time_window", {})
        window_start = timeparse.parse_time(str(tw.get("start", "07:00")))
        window_end = timeparse.parse_time(str(tw.get("end", "09:00")))
    if window_end < window_start:
        raise ValueError("Time window end is before its start.")

    players = int(overrides.get("players") or booking.get("players", 4))
    if not 1 <= players <= 4:
        raise ValueError("players must be between 1 and 4.")

    # --- courses & priority (with CLI overrides) ---
    raw_courses = overrides.get("courses")
    if raw_courses is None:
        raw_courses = booking.get("courses", []) or []
    if isinstance(raw_courses, str):
        raw_courses = [raw_courses]
    courses = [str(c).strip() for c in raw_courses if str(c).strip()]

    priority = str(overrides.get("priority") or booking.get("priority", "time")).lower()
    if priority not in ("course", "time"):
        raise ValueError("priority must be 'course' (time flexible) or "
                         "'time' (location flexible).")
    # Empty courses is allowed for single-course sites: courses_to_search()
    # returns [None] and the booking flow runs once against the default sheet.

    unlock_time = timeparse.parse_time(str(scheduling.get("unlock_time", "00:00")))

    return BookingConfig(
        desired_date=desired_date,
        window_start=window_start,
        window_end=window_end,
        players=players,
        courses=courses,
        priority=priority,
        unlock_days_before=int(scheduling.get("unlock_days_before", 8)),
        unlock_time=unlock_time,
        timezone=tz,
        prefire_seconds=int(overrides["prefire_seconds"]
                            if "prefire_seconds" in overrides
                            else scheduling.get("prefire_seconds", 60)),
        poll_interval_seconds=float(scheduling.get("poll_interval_seconds", 1.5)),
        selection_budget_seconds=float(scheduling.get("selection_budget_seconds", 6)),
        login_attempts=int(retry.get("login_attempts", 10)),
        login_delay_seconds=float(retry.get("login_delay_seconds", 2)),
        booking_window_minutes=float(retry.get("booking_window_minutes", 5)),
        fallback_to_next_best=bool(retry.get("fallback_to_next_best", True)),
        headless=_coerce_bool(overrides.get("headless"), browser.get("headless", True)),
        timeout_ms=int(browser.get("timeout_ms", 30000)),
        macos_desktop=bool(notifications.get("macos_desktop", True)),
        log_dir=Path(paths.get("log_dir", "logs")),
        screenshot_dir=Path(paths.get("screenshot_dir", "screenshots")),
        base_url=str(site.get("base_url", "https://fox.tenfore.golf/mcggolf")).rstrip("/"),
        dry_run=bool(overrides.get("dry_run", False)),
        raw=raw,
    )


def _coerce_bool(override, default):
    if override is None:
        return bool(default)
    return bool(override)


def _local_zoneinfo() -> ZoneInfo:
    """Best-effort local timezone, falling back to UTC."""
    try:
        local = dt.datetime.now().astimezone().tzinfo
        key = getattr(local, "key", None)
        if key:
            return ZoneInfo(key)
    except Exception:
        pass
    return ZoneInfo("UTC")
