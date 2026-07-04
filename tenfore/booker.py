"""Playwright (async) browser automation for booking a Tenfore tee time.

IMPORTANT — SELECTORS
---------------------
The exact DOM of fox.tenfore.golf can vary by club and changes over time,
so this module never relies on a single brittle selector. Each step tries an
ordered list of *candidate* locators (role-based, text-based, and CSS) and uses
the first that resolves. You can override or extend every candidate list from
config.yaml under `site.selectors:` without touching code — see README.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

from .config import BookingConfig, Credentials
from .logsetup import get_logger

# Default candidate selectors. Overridable via config.yaml -> site.selectors.
DEFAULT_SELECTORS: dict[str, list[str]] = {
    "login_link": [
        "a:has-text('Login')", "a:has-text('Log in')", "a:has-text('Sign in')",
        "button:has-text('Login')", "[href*='login']",
    ],
    "email_input": [
        "input[type='email']", "input[name='email' i]", "input[name='username' i]",
        "input[id*='email' i]", "input[id*='user' i]",
        "input[type='text'][name*='user' i]", "input[type='text'][id*='name' i]",
    ],
    "password_input": [
        "input[type='password']", "input[name='password' i]", "input[id*='pass' i]",
    ],
    # Button that advances a two-step login (email screen -> password screen).
    "next_button": [
        "button:has-text('Next')", "button:has-text('Continue')",
        "input[type='submit'][value*='next' i]",
        "input[type='submit'][value*='continue' i]",
    ],
    "login_submit": [
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Login')", "button:has-text('Log in')",
        "button:has-text('Sign in')",
    ],
    "logged_in_marker": [
        "a:has-text('Logout')", "a:has-text('Log out')", "a:has-text('My Account')",
        "[href*='logout']", "text=Sign out",
    ],
    "booking_nav": [
        "a:has-text('Book a Tee Time')", "a:has-text('Tee Times')",
        "a:has-text('Book')", "a:has-text('Booking')", "[href*='booking']",
        "[href*='teetime']", "[href*='tee-time']",
    ],
    # Custom date pickers (Vue/React chip widgets): the chip/button that OPENS
    # the calendar popup, and the popup's next-month arrow.
    "date_opener": [
        "button:has([class*='date-button'])", "[class*='date-button']",
        "button:has(.fa-calendar-days)", "[class*='datepicker-toggle']",
    ],
    "date_next_month": [
        "[aria-label*='next' i]", "[class*='chevron-right']",
        "[class*='next-month']", "button:has-text('›')", "button:has-text('»')",
    ],
    # A dropdown/select used to pick the golf course. select_course() also tries
    # clicking a tab/link/button whose text matches the course name.
    "course_select": [
        "select[name*='course' i]", "select[id*='course' i]",
        "select[name*='club' i]", "[aria-label*='course' i]",
    ],
    # A row/card representing a single bookable slot. Each match should contain
    # a time and a book button somewhere inside it.
    "slot_row": [
        "[class*='teetime']", "[class*='tee-time']", "[class*='slot']",
        "tr[class*='booking']", "[data-time]", ".available",
        # TenFore SPA result cards (time + course + price in a clickable card).
        "div.bg-white.rounded-md.cursor-pointer",
        # Last resort for card-style SPAs: any div whose text STARTS with a
        # clock time (e.g. "12:36 PM Falls Road Golf Course $81.00").
        'div:text-matches("^\\s*\\d{1,2}:\\d{2}\\s*[AP]\\.?M", "i")',
    ],
    "book_button": [
        "button:has-text('Book')", "a:has-text('Book')",
        "button:has-text('Reserve')", "[class*='book-btn']",
    ],
    "players_select": [
        "select[name*='player']", "select[id*='player']",
        "select[name*='golfer']", "[aria-label*='player' i]",
    ],
    "confirm_button": [
        "button:has-text('Confirm')", "button:has-text('Complete')",
        "button:has-text('Confirm Booking')", "button:has-text('Reserve')",
        "button:has-text('Book Now')", "button[type='submit']",
    ],
    "booking_closed_marker": [
        "text=not yet open", "text=not available yet", "text=opens",
        "text=booking window", "text=check back",
    ],
}


@dataclass
class Slot:
    time: dt.time
    label: str  # original text, e.g. "07:20 AM"
    handle_index: int  # index into the located slot rows
    course: str | None = None  # which course this slot belongs to
    selector: str | None = None  # the slot_row selector these rows came from


@dataclass
class BookingResult:
    success: bool
    booked_time: dt.time | None = None
    booked_label: str | None = None
    booked_course: str | None = None
    in_preferred_window: bool = False
    screenshot_path: Path | None = None
    message: str = ""


class TenforeBooker:
    def __init__(self, config: BookingConfig, credentials: Credentials):
        self.config = config
        self.creds = credentials
        self.log = get_logger()
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

        overrides = (config.raw.get("site", {}) or {}).get("selectors", {}) or {}
        self.selectors = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
        for key, value in overrides.items():
            # User-provided selectors take priority but keep defaults as fallback.
            self.selectors[key] = list(value) + self.selectors.get(key, [])

    # ----------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # Escape hatches for locked-down machines / CI:
        #   TENFORE_CHROMIUM_PATH -> use a specific Chromium/Chrome binary instead
        #                            of Playwright's managed download.
        #   TENFORE_NO_SANDBOX=1  -> launch with --no-sandbox (needed when running
        #                            as root / inside some containers).
        launch_kwargs: dict = {"headless": self.config.headless}
        exe = os.getenv("TENFORE_CHROMIUM_PATH")
        if exe:
            launch_kwargs["executable_path"] = exe
            self.log.info("Using Chromium binary from TENFORE_CHROMIUM_PATH: %s", exe)
        if os.getenv("TENFORE_NO_SANDBOX") == "1":
            launch_kwargs["args"] = ["--no-sandbox"]
        self.browser = await self._pw.chromium.launch(**launch_kwargs)
        context_kwargs: dict = dict(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Reuse a persisted session (cookies/localStorage) from a prior run so
        # login can be skipped during the time-critical booking window.
        state_file = self.config.session_file
        if state_file and state_file.exists():
            context_kwargs["storage_state"] = str(state_file)
            self.log.info("Loaded persisted session from %s", state_file)
        try:
            self.context = await self.browser.new_context(**context_kwargs)
        except Exception as exc:
            self.log.warning("Persisted session unusable (%s) — starting fresh.", exc)
            context_kwargs.pop("storage_state", None)
            self.context = await self.browser.new_context(**context_kwargs)
        self.context.set_default_timeout(self.config.timeout_ms)
        self.page = await self.context.new_page()
        self.log.debug("Browser started (headless=%s)", self.config.headless)

    async def close(self) -> None:
        # Persist the session so the next run starts pre-authenticated.
        try:
            if self.context and self.config.session_file:
                await self.context.storage_state(path=str(self.config.session_file))
                self.log.debug("Session state saved to %s", self.config.session_file)
        except Exception as exc:
            self.log.debug("Could not save session state: %s", exc)
        for closer in (self.context, self.browser):
            try:
                if closer:
                    await closer.close()
            except Exception as exc:
                self.log.debug("Error during close: %s", exc)
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as exc:
            self.log.debug("Error stopping Playwright: %s", exc)

    # ------------------------------------------------------------------- helpers
    async def _first_visible(self, key: str, *, root=None, timeout: float = 2000):
        """Return the first visible locator from a candidate list, or None.

        ``timeout`` is the wall-clock budget for the WHOLE candidate list.
        Candidates are polled in preference order in short rounds until the
        budget expires, so an element that appears late is still caught by a
        later round, while a list of absent selectors costs at most ``timeout``
        total (previously each candidate waited the full timeout, multiplying
        worst-case dead time by the list length).
        """
        scope = root or self.page
        candidates = self.selectors.get(key, [])
        if not candidates:
            return None
        deadline = time.monotonic() + timeout / 1000
        slice_ms = 100  # per-candidate probe per polling round
        while time.monotonic() < deadline:
            for sel in candidates:
                remaining_ms = (deadline - time.monotonic()) * 1000
                if remaining_ms <= 0:
                    return None
                try:
                    loc = scope.locator(sel).first
                    await loc.wait_for(state="visible",
                                       timeout=min(slice_ms, remaining_ms))
                    return loc
                except PWTimeout:
                    continue
                except Exception as exc:
                    self.log.debug("Selector %r errored: %s", sel, exc)
        return None

    async def _goto(self, url: str) -> None:
        assert self.page
        await self.page.goto(url, wait_until="domcontentloaded",
                             timeout=self.config.timeout_ms)

    # --------------------------------------------------------------------- login
    async def login(self) -> bool:
        """Authenticate, retrying until success, ``login_attempts``, or the
        ``login_window_seconds`` wall-clock cap — whichever comes first.

        Handles both one-step (email + password on one screen) and two-step
        (email -> Next/Continue -> password) login flows.
        """
        assert self.page
        deadline = time.monotonic() + self.config.login_window_seconds
        connection_error = False
        for attempt in range(1, self.config.login_attempts + 1):
            if time.monotonic() >= deadline:
                self.log.error(
                    "Login wall-clock budget (%.0fs) exhausted after %d attempt(s).",
                    self.config.login_window_seconds, attempt - 1)
                return False
            connection_error = False
            try:
                self.log.info("Login attempt %d/%d", attempt, self.config.login_attempts)
                await self._goto(self.config.base_url)

                # Already logged in (persisted session)?
                if await self._first_visible("logged_in_marker", timeout=1500):
                    self.log.info("Already authenticated.")
                    return True

                link = await self._first_visible("login_link", timeout=2500)
                if link:
                    await link.click()
                    await self.page.wait_for_load_state("domcontentloaded")

                email = await self._first_visible("email_input", timeout=5000)
                if not email:
                    raise RuntimeError("Could not locate the email/username field.")
                await email.fill(self.creds.email)

                # Two-step flows (typical ASP.NET) reveal the password field only
                # after the email screen is advanced with a Next/Continue button.
                password = await self._first_visible("password_input", timeout=1500)
                if not password:
                    advance = (await self._first_visible("next_button", timeout=2000)
                               or await self._first_visible("login_submit", timeout=1500))
                    if advance:
                        await advance.click()
                    else:
                        await email.press("Enter")
                    await self.page.wait_for_load_state("domcontentloaded")
                    password = await self._first_visible("password_input", timeout=5000)
                    if not password:
                        raise RuntimeError(
                            "No password field found, even after advancing a "
                            "possible two-step login.")
                    self.log.info("Two-step login detected; advanced to password screen.")
                    # Some second screens re-render an empty email field; refill it.
                    email2 = await self._first_visible("email_input", timeout=500)
                    if email2:
                        try:
                            if not await email2.input_value():
                                await email2.fill(self.creds.email)
                        except Exception:
                            pass

                await password.fill(self.creds.password)

                submit = await self._first_visible("login_submit", timeout=3000)
                if submit:
                    await submit.click()
                else:
                    await password.press("Enter")

                try:
                    await self.page.wait_for_load_state(
                        "networkidle", timeout=self.config.timeout_ms)
                except PWTimeout:
                    self.log.debug("networkidle wait timed out after submit (continuing).")

                if await self._first_visible("logged_in_marker", timeout=4000):
                    self.log.info("Login successful.")
                    return True

                # Some sites land directly on the dashboard with no logout link
                # visible above the fold. Absence of a password field is a soft
                # success signal, but it could also be an error/redirect page, so
                # we proceed with an explicit "unverified" warning rather than
                # claiming success.
                if not await self._first_visible("password_input", timeout=1500):
                    self.log.warning(
                        "Login attempt %d: no login form present but no logged-in "
                        "marker found either — proceeding with UNVERIFIED auth. "
                        "If booking fails, check the 'logged_in_marker' selector.",
                        attempt)
                    return True

                self.log.warning("Login attempt %d did not confirm success.", attempt)
            except Exception as exc:
                msg = str(exc)
                # Connection-level failures (DNS, TLS, refused) will not be fixed
                # by waiting out the normal delay — retry quickly instead.
                connection_error = "net::ERR" in msg or "ERR_NAME" in msg
                self.log.warning("Login attempt %d failed%s: %s", attempt,
                                 " (connection error)" if connection_error else "",
                                 msg.splitlines()[0][:200])

            if attempt < self.config.login_attempts:
                await asyncio.sleep(0.5 if connection_error
                                    else self.config.login_delay_seconds)

        self.log.error("All %d login attempts failed.", self.config.login_attempts)
        return False

    # ------------------------------------------------------ booking page access
    async def open_booking_page(self, minutes: float | None = None) -> bool:
        """Navigate to the tee-time booking page.

        Retries until the booking window opens or ``minutes`` elapses (defaults
        to ``booking_window_minutes``; ``check`` passes a short value for a quick
        look). Handles the "too early / not open yet" case.
        """
        assert self.page
        window = self.config.booking_window_minutes if minutes is None else minutes
        deadline = self.config.now() + dt.timedelta(minutes=window)
        attempt = 0
        while self.config.now() < deadline:
            attempt += 1
            try:
                # The page we're already on (e.g. the post-login landing page)
                # may itself be the booking page — check before navigating away.
                if await self._looks_like_booking_page():
                    self.log.info("Current page already shows booking content "
                                  "(attempt %d).", attempt)
                    return True

                nav = await self._first_visible("booking_nav", timeout=3000)
                if nav:
                    await nav.click()
                    await self.page.wait_for_load_state("domcontentloaded")
                else:
                    # Fall back to common direct paths.
                    for path in ("/booking", "/tee-times", "/teetimes", "/book"):
                        try:
                            await self._goto(self.config.base_url + path)
                            break
                        except Exception:
                            continue

                closed = await self._first_visible("booking_closed_marker", timeout=1500)
                if closed:
                    txt = (await closed.inner_text()).strip()
                    self.log.info("Booking not open yet (attempt %d): %r — retrying.",
                                  attempt, txt[:80])
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue

                # Require POSITIVE evidence of a booking page before declaring
                # success — a 404/error page has no closed-marker either, and
                # proceeding on one would mask the real failure as "no slots".
                if await self._looks_like_booking_page():
                    self.log.info("Booking page reached (attempt %d).", attempt)
                    return True
                self.log.info("Page has no booking content yet (attempt %d) — "
                              "retrying.", attempt)
                await asyncio.sleep(self.config.poll_interval_seconds)
            except Exception as exc:
                self.log.warning("Booking page attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(self.config.poll_interval_seconds)

        self.log.error("Booking page never became available within the window.")
        return False

    async def _looks_like_booking_page(self) -> bool:
        """Positive check that the current page shows booking content:
        slot rows, a date control, or a course selector."""
        assert self.page
        for sel in self.selectors["slot_row"]:
            try:
                if await self.page.locator(sel).count():
                    return True
            except Exception:
                continue
        try:
            if await self.page.locator("input[type='date']").count():
                return True
        except Exception:
            pass
        return bool(await self._first_visible("course_select", timeout=800))

    def _budget_deadline(self) -> float:
        """A monotonic deadline ``selection_budget_seconds`` from now."""
        return time.monotonic() + self.config.selection_budget_seconds

    async def _settle(self, timeout: int = 8000) -> None:
        """Best-effort wait for the network to go idle. Never raises.

        Used after a successful action (date set, course chosen) so a slow or
        never-idle page (long-polling, websockets) can't make the caller think
        the action failed and retry a different element.
        """
        assert self.page
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            self.log.debug("networkidle wait timed out (continuing).")

    # ------------------------------------------------------------- date selection
    async def select_date(self) -> bool:
        """Select the desired play date in whatever date control the page uses.

        Bounded by ``selection_budget_seconds`` so a missing/unmatched control
        can't eat polling time during the booking race.
        """
        assert self.page
        target = self.config.desired_date
        deadline = self._budget_deadline()
        candidates = [
            target.strftime("%Y-%m-%d"),
            target.strftime("%d/%m/%Y"),
            target.strftime("%m/%d/%Y"),
            target.strftime("%d %B %Y"),
            target.strftime("%A %d %B"),
            target.strftime("%B %d"),
        ]

        # 0) Direct ISO day-cell: some tee sheets/pickers expose the ISO date
        # as an attribute or id on an always-present element — instant win.
        try:
            iso = target.isoformat()
            loc = self.page.locator(f"[data-date='{iso}'], [id='{iso}']").first
            await loc.wait_for(state="visible",
                               timeout=_remaining_ms(deadline, cap=500) or 1)
            await loc.click()
            await self._settle(3000)
            self.log.info("Selected date via direct ISO cell: %s", iso)
            return True
        except Exception:
            pass

        # 1) A real <input type=date> is the most reliable.
        wait = _remaining_ms(deadline, cap=1000)
        if wait:
            try:
                date_input = self.page.locator("input[type='date']").first
                await date_input.wait_for(state="visible", timeout=wait)
                await date_input.fill(target.strftime("%Y-%m-%d"))
                await date_input.dispatch_event("change")
                self.log.info("Selected date via date input: %s", target.isoformat())
                # The fill succeeded — the date IS set. Don't let a slow/never-idle
                # network make us fall through and click a different date.
                await self._settle()
                return True
            except Exception:
                pass

        # 1.5) Chip-style picker (Vue/React SPA): click the chip that opens the
        # calendar popup, click the EXACT day number, then verify the chip text
        # actually changed to the target date. Exact-text matching (:text-is)
        # is essential: has-text('11') would also match an "11:59 PM" chip.
        opener = await self._first_visible(
            "date_opener", timeout=_remaining_ms(deadline, cap=2000) or 1)
        if opener:
            try:
                await opener.click()
                await asyncio.sleep(0.7)  # let the popup render (SPA animation)
                self.log.info("Date picker: opener clicked, searching for day cell %s",
                              target.isoformat())
                for month_hop in range(2):  # retry once after next-month nav
                    day_cell = None
                    for sel in (f"[id='{target.isoformat()}'] [class*='cell_inner']",
                                f"[id='{target.isoformat()}']",
                                f"[class*='calendar_item']:has(:text-is('{target.day}'))",
                                f"[class*='calendar'] :text-is('{target.day}')",
                                f"[class*='picker'] :text-is('{target.day}')",
                                f"[class*='popup'] :text-is('{target.day}')",
                                f"[class*='menu'] :text-is('{target.day}')",
                                f"td:text-is('{target.day}')",
                                f"button:text-is('{target.day}')",
                                f"span:text-is('{target.day}')"):
                        wait = _remaining_ms(deadline, cap=800)
                        if not wait:
                            break
                        try:
                            loc = self.page.locator(sel).first
                            await loc.wait_for(state="visible", timeout=wait)
                            day_cell = loc
                            break
                        except Exception:
                            continue
                    if day_cell:
                        self.log.info("Date picker: clicking day cell (%r matched).",
                                      sel)
                        await day_cell.click()
                        await self._settle(3000)
                        if await self._date_chip_matches(target):
                            self.log.info("Selected date via picker popup: %s",
                                          target.isoformat())
                            return True
                        self.log.info("Date picker: chip did not verify after "
                                      "day-cell click.")
                    else:
                        self.log.info("Date picker: no day cell matched "
                                      "(month hop %d).", month_hop)
                    if month_hop == 0 and _remaining_ms(deadline, cap=1) > 0:
                        nxt = await self._first_visible(
                            "date_next_month",
                            timeout=_remaining_ms(deadline, cap=800) or 1)
                        if not nxt:
                            break
                        await nxt.click()
                        await asyncio.sleep(0.25)
                self.log.warning("Date picker flow could not verify %s — saving "
                                 "diagnostic screenshot.", target.isoformat())
                try:
                    await self.screenshot("date-picker-fail")
                except Exception:
                    pass
                await self.page.keyboard.press("Escape")  # close a stray popup
            except Exception as exc:
                self.log.debug("Picker popup flow failed: %s", exc)
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

        # 2) Click a day cell / link matching one of the textual forms.
        for text in candidates:
            for sel in (f"[data-date='{target.isoformat()}']",
                        f"a:has-text('{text}')",
                        f"button:has-text('{text}')",
                        f"td:has-text('{text}')"):
                wait = _remaining_ms(deadline, cap=1000)
                if not wait:
                    self.log.warning(
                        "Date-selection budget (%.0fs) exhausted before matching %s.",
                        self.config.selection_budget_seconds, target.isoformat())
                    return False
                try:
                    loc = self.page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=wait)
                    await loc.click()
                except Exception:
                    continue
                self.log.info("Selected date via %r", sel)
                await self._settle()
                return True

        self.log.warning(
            "Could not explicitly select date %s — proceeding with whatever the "
            "page currently shows (it may already default to it).", target.isoformat()
        )
        return False

    async def _date_chip_matches(self, target: dt.date) -> bool:
        """True if the date chip's visible text now shows the target date
        (e.g. 'Jul 11, 2026'). Month is accepted as short or full name."""
        chip = await self._first_visible("date_opener", timeout=1200)
        if not chip:
            return False
        try:
            text = (await chip.inner_text()).strip()
        except Exception:
            return False
        return (str(target.day) in text
                and (target.strftime("%b") in text or target.strftime("%B") in text)
                and str(target.year) in text)

    # ----------------------------------------------------------- course selection
    async def select_course(self, course: str) -> bool:
        """Select a golf course by name via a dropdown, tab, link, or button.

        Bounded by ``selection_budget_seconds`` (see select_date).
        """
        assert self.page
        deadline = self._budget_deadline()

        # 1) A <select> course dropdown is the most reliable.
        wait = _remaining_ms(deadline, cap=2000)
        dropdown = await self._first_visible("course_select", timeout=wait) if wait else None
        if dropdown:
            for kwargs in ({"label": course},
                           {"value": course},
                           {"label": _loose(course)}):
                try:
                    await dropdown.select_option(**kwargs)
                except Exception:
                    continue
                self.log.info("Selected course %r via dropdown.", course)
                await self._settle()
                return True

        # 2) Click a tab/link/button/option whose visible text matches the name.
        for sel in (f"[role='tab']:has-text('{course}')",
                    f"button:text-is('{course}')",
                    f"text=\"{course}\"",
                    f"a:has-text('{course}')",
                    f"button:has-text('{course}')",
                    f"li:has-text('{course}')",
                    f"option:has-text('{course}')"):
            wait = _remaining_ms(deadline, cap=1000)
            if not wait:
                self.log.warning(
                    "Course-selection budget (%.0fs) exhausted before matching %r.",
                    self.config.selection_budget_seconds, course)
                return False
            try:
                loc = self.page.locator(sel).first
                await loc.wait_for(state="visible", timeout=wait)
                await loc.click()
            except Exception:
                continue
            self.log.info("Selected course %r via %r", course, sel)
            await self._settle()
            return True

        self.log.warning(
            "Could not explicitly select course %r — proceeding with whatever the "
            "page currently shows. Add a 'course_select' override in config.yaml "
            "if this course is being missed.", course
        )
        return False

    async def gather_slots(self, courses: list[str | None]) -> list[Slot]:
        """Select the date for each course in turn and collect all open slots.

        Slots are tagged with their course and returned sorted by time; for
        equal times the earlier-listed (more preferred) course wins, since the
        sort is stable and courses are scanned in preference order.
        """
        combined: list[Slot] = []
        for course in courses:
            if course:
                await self.select_course(course)
            await self.select_date()
            combined.extend(await self.collect_slots(course=course))
        combined.sort(key=lambda s: s.time)
        return combined

    # ------------------------------------------------------------- slot scraping
    async def collect_slots(self, course: str | None = None) -> list[Slot]:
        """Scrape available tee times currently shown on the page."""
        assert self.page
        rows = None
        used_selector = None
        for sel in self.selectors["slot_row"]:
            loc = self.page.locator(sel)
            try:
                count = await loc.count()
            except Exception:
                count = 0
            if count:
                rows = loc
                used_selector = sel
                self.log.debug("Using slot selector %r (%d rows).", sel, count)
                break

        if not rows:
            self.log.warning("No tee-time rows found on the page.")
            return []

        slots: list[Slot] = []
        # One round-trip for all row texts (indexes align with the locator's
        # order) instead of a per-row inner_text() call — ~10x faster on big
        # tee sheets, which matters in the booking race.
        try:
            texts = await rows.all_inner_texts()
        except Exception as exc:
            self.log.debug("all_inner_texts failed (%s); no rows readable.", exc)
            texts = []
        # On multi-course tee sheets each card names its course. When a course
        # was requested AND at least one row mentions it, keep only that
        # course's rows — otherwise a stray toggled filter chip could surface
        # (and book) another course's time.
        course_filter = None
        if course and any(course.lower() in t.lower() for t in texts):
            course_filter = course.lower()

        for i, text in enumerate(texts):
            text = text.strip()
            t = _extract_time(text)
            if t is None:
                continue
            if course_filter and course_filter not in text.lower():
                continue
            # Only treat it as bookable if it appears to offer a book action and
            # is not marked unavailable/full.
            lowered = text.lower()
            if any(w in lowered for w in ("full", "unavailable", "booked", "sold out")):
                continue
            slots.append(Slot(time=t, label=_first_time_label(text),
                              handle_index=i, course=course,
                              selector=used_selector))

        slots.sort(key=lambda s: s.time)
        where = f" at {course}" if course else ""
        self.log.info("Found %d available slot(s)%s: %s",
                      len(slots), where,
                      ", ".join(s.label for s in slots[:12]) or "none")
        return slots

    def choose_slot(self, slots: list[Slot]) -> tuple[Slot | None, bool]:
        """Pick the slot to book, honouring the priority mode.

        Returns (slot, in_preferred_window).

        - Earliest slot inside the preferred window always wins. With time
          priority the candidates span every searched course, so this finds the
          best time regardless of location.
        - Otherwise the next-best slot is taken when allowed. Course priority
          treats time as flexible, so it always falls back; time priority only
          falls back when ``fallback_to_next_best`` is enabled.
        """
        in_window = [s for s in slots if self.config.window_contains(s.time)]
        if in_window:
            return in_window[0], True

        allow_fallback = self.config.priority == "course" or self.config.fallback_to_next_best
        if not allow_fallback or not slots:
            return None, False

        # Next-best: prefer the earliest slot AFTER the window start; if none,
        # the slot closest to the window start overall.
        after = [s for s in slots if s.time >= self.config.window_start]
        best = after[0] if after else min(
            slots, key=lambda s: abs(_to_minutes(s.time) - _to_minutes(self.config.window_start))
        )
        return best, False

    # ----------------------------------------------------------------- booking
    async def book_slot(self, slot: Slot) -> bool:
        """Click into a slot, set players, and confirm the reservation.

        Re-selects the slot's course/date and re-resolves the row by time so the
        booking is robust even when slots were gathered across several courses
        (the page may currently be showing a different course).

        When ``config.dry_run`` is set, every step runs up to (but not including)
        the final confirm click — so it validates the whole path without making
        a real reservation.
        """
        assert self.page
        if slot.course:
            if not await self.select_course(slot.course) and len(self.config.courses) > 1:
                # With multiple courses configured we must be sure we're on the
                # right one — booking a time-matched slot on the wrong course
                # would be worse than failing. Let the runner try the next slot.
                self.log.error(
                    "Could not select course %r for booking; aborting this slot so "
                    "we don't book on the wrong course.", slot.course)
                return False
        await self.select_date()

        fresh = await self.collect_slots(course=slot.course)
        match = next((s for s in fresh if s.time == slot.time), None)
        if match is None:
            self.log.warning("Slot %s%s no longer available at booking time.",
                             slot.label, f" at {slot.course}" if slot.course else "")
            return False

        # Reuse the exact selector collect_slots matched, so the row index can't
        # point into a different (broader) selector's element list.
        row = None
        selectors = ([match.selector] if match.selector else []) + self.selectors["slot_row"]
        for sel in selectors:
            loc = self.page.locator(sel)
            if await loc.count() > match.handle_index:
                row = loc.nth(match.handle_index)
                break
        if row is None:
            self.log.error("Slot row vanished before booking.")
            return False

        book_btn = await self._first_visible("book_button", root=row, timeout=3000)
        if not book_btn:
            # Some layouts make the whole row clickable.
            book_btn = row
        await book_btn.click()
        await self.page.wait_for_load_state("domcontentloaded")

        # Set number of players if a control exists.
        players_sel = await self._first_visible("players_select", timeout=2500)
        if players_sel:
            try:
                await players_sel.select_option(str(self.config.players))
                self.log.debug("Set players to %d.", self.config.players)
            except Exception:
                try:
                    await players_sel.select_option(label=str(self.config.players))
                except Exception as exc:
                    self.log.debug("Could not set players: %s", exc)

        confirm = await self._first_visible("confirm_button", timeout=5000)
        if not confirm:
            self.log.error("Could not find a confirm/book button.")
            return False

        if self.config.dry_run:
            self.log.info("DRY RUN — confirm button located but NOT clicked. "
                          "No reservation was made for %s%s.",
                          slot.label, f" at {slot.course}" if slot.course else "")
            return True

        await confirm.click()

        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.config.timeout_ms)
        except PWTimeout:
            pass

        # Heuristic success check.
        body = (await self.page.inner_text("body")).lower()
        if any(w in body for w in ("confirmed", "booking reference", "you're booked",
                                   "reservation complete", "success")):
            self.log.info("Confirmation text detected on page.")
            return True

        self.log.warning("No explicit confirmation text found; assuming booked "
                         "(verify via screenshot).")
        return True

    async def screenshot(self, name: str) -> Path:
        assert self.page
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.config.screenshot_dir / f"{name}-{stamp}.png"
        await self.page.screenshot(path=str(path), full_page=True)
        self.log.info("Saved screenshot -> %s", path)
        return path


# --------------------------------------------------------------------- utilities
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?\b", re.IGNORECASE)


def _extract_time(text: str) -> dt.time | None:
    m = _TIME_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    mer = (m.group(3) or "").replace(".", "").lower()
    if mer == "pm" and hour != 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def _first_time_label(text: str) -> str:
    m = _TIME_RE.search(text)
    return m.group(0).strip() if m else text.strip()[:20]


def _to_minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _loose(name: str) -> str:
    """A relaxed form of a course name for fuzzy <select> matching."""
    return re.sub(r"\s+", " ", name).strip()


def _remaining_ms(deadline: float, *, cap: int) -> int:
    """Milliseconds left until ``deadline`` (monotonic), clamped to [0, cap]."""
    rem = (deadline - time.monotonic()) * 1000
    if rem <= 0:
        return 0
    return int(min(cap, rem))
