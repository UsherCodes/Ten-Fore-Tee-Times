"""Playwright (async) browser automation for booking a Tenfore tee time.

IMPORTANT — SELECTORS
---------------------
The exact DOM of www.fox.tenfore.golf can vary by club and changes over time,
so this module never relies on a single brittle selector. Each step tries an
ordered list of *candidate* locators (role-based, text-based, and CSS) and uses
the first that resolves. You can override or extend every candidate list from
config.yaml under `site.selectors:` without touching code — see README.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
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
        "input[type='email']", "input[name='email']", "input[name='username']",
        "input[id*='email']", "input[id*='user']",
    ],
    "password_input": [
        "input[type='password']", "input[name='password']", "input[id*='pass']",
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
    # A row/card representing a single bookable slot. Each match should contain
    # a time and a book button somewhere inside it.
    "slot_row": [
        "[class*='teetime']", "[class*='tee-time']", "[class*='slot']",
        "tr[class*='booking']", "[data-time]", ".available",
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


@dataclass
class BookingResult:
    success: bool
    booked_time: dt.time | None = None
    booked_label: str | None = None
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
        self.browser = await self._pw.chromium.launch(headless=self.config.headless)
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self.context.set_default_timeout(self.config.timeout_ms)
        self.page = await self.context.new_page()
        self.log.debug("Browser started (headless=%s)", self.config.headless)

    async def close(self) -> None:
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
        """Return the first visible locator from a candidate list, or None."""
        scope = root or self.page
        for sel in self.selectors.get(key, []):
            try:
                loc = scope.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout)
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
        """Authenticate, retrying up to ``login_attempts`` times."""
        assert self.page
        for attempt in range(1, self.config.login_attempts + 1):
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
                password = await self._first_visible("password_input", timeout=5000)
                if not email or not password:
                    raise RuntimeError("Could not locate login form fields.")

                await email.fill(self.creds.email)
                await password.fill(self.creds.password)

                submit = await self._first_visible("login_submit", timeout=3000)
                if submit:
                    await submit.click()
                else:
                    await password.press("Enter")

                await self.page.wait_for_load_state("networkidle", timeout=self.config.timeout_ms)

                if await self._first_visible("logged_in_marker", timeout=4000):
                    self.log.info("Login successful.")
                    return True

                # Some sites land directly on the dashboard with no logout link
                # visible above the fold — treat absence of a password field as
                # a soft success signal.
                if not await self._first_visible("password_input", timeout=1500):
                    self.log.info("Login appears successful (no login form present).")
                    return True

                self.log.warning("Login attempt %d did not confirm success.", attempt)
            except (PWTimeout, RuntimeError, Exception) as exc:
                self.log.warning("Login attempt %d failed: %s", attempt, exc)

            if attempt < self.config.login_attempts:
                await asyncio.sleep(self.config.login_delay_seconds)

        self.log.error("All %d login attempts failed.", self.config.login_attempts)
        return False

    # ------------------------------------------------------ booking page access
    async def open_booking_page(self) -> bool:
        """Navigate to the tee-time booking page.

        Retries until the booking window opens or ``booking_window_minutes``
        elapses (handles the "too early / not open yet" case).
        """
        assert self.page
        deadline = self.config.now() + dt.timedelta(minutes=self.config.booking_window_minutes)
        attempt = 0
        while self.config.now() < deadline:
            attempt += 1
            try:
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

                self.log.info("Booking page reached (attempt %d).", attempt)
                return True
            except (PWTimeout, Exception) as exc:
                self.log.warning("Booking page attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(self.config.poll_interval_seconds)

        self.log.error("Booking page never became available within the window.")
        return False

    # ------------------------------------------------------------- date selection
    async def select_date(self) -> bool:
        """Select the desired play date in whatever date control the page uses."""
        assert self.page
        target = self.config.desired_date
        candidates = [
            target.strftime("%Y-%m-%d"),
            target.strftime("%d/%m/%Y"),
            target.strftime("%m/%d/%Y"),
            target.strftime("%d %B %Y"),
            target.strftime("%A %d %B"),
            target.strftime("%B %d"),
            str(target.day),
        ]

        # 1) A real <input type=date> is the most reliable.
        try:
            date_input = self.page.locator("input[type='date']").first
            await date_input.wait_for(state="visible", timeout=2000)
            await date_input.fill(target.strftime("%Y-%m-%d"))
            await date_input.dispatch_event("change")
            self.log.info("Selected date via date input: %s", target.isoformat())
            await self.page.wait_for_load_state("networkidle", timeout=8000)
            return True
        except Exception:
            pass

        # 2) Click a day cell / link matching one of the textual forms.
        for text in candidates:
            for sel in (f"[data-date='{target.isoformat()}']",
                        f"a:has-text('{text}')",
                        f"button:has-text('{text}')",
                        f"td:has-text('{text}')"):
                try:
                    loc = self.page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=1200)
                    await loc.click()
                    self.log.info("Selected date via %r", sel)
                    await self.page.wait_for_load_state("networkidle", timeout=8000)
                    return True
                except Exception:
                    continue

        self.log.warning(
            "Could not explicitly select date %s — proceeding with whatever the "
            "page currently shows (it may already default to it).", target.isoformat()
        )
        return False

    # ------------------------------------------------------------- slot scraping
    async def collect_slots(self) -> list[Slot]:
        """Scrape available tee times currently shown on the page."""
        assert self.page
        rows = None
        for sel in self.selectors["slot_row"]:
            loc = self.page.locator(sel)
            try:
                count = await loc.count()
            except Exception:
                count = 0
            if count:
                rows = loc
                self.log.debug("Using slot selector %r (%d rows).", sel, count)
                break

        if not rows:
            self.log.warning("No tee-time rows found on the page.")
            return []

        slots: list[Slot] = []
        count = await rows.count()
        for i in range(count):
            row = rows.nth(i)
            try:
                text = (await row.inner_text()).strip()
            except Exception:
                continue
            t = _extract_time(text)
            if t is None:
                continue
            # Only treat it as bookable if it appears to offer a book action and
            # is not marked unavailable/full.
            lowered = text.lower()
            if any(w in lowered for w in ("full", "unavailable", "booked", "sold out")):
                continue
            slots.append(Slot(time=t, label=_first_time_label(text), handle_index=i))

        slots.sort(key=lambda s: s.time)
        self.log.info("Found %d available slot(s): %s",
                      len(slots), ", ".join(s.label for s in slots[:12]) or "none")
        return slots

    def choose_slot(self, slots: list[Slot]) -> tuple[Slot | None, bool]:
        """Pick the slot to book.

        Returns (slot, in_preferred_window). Earliest slot inside the window
        wins; otherwise the next-best (closest after window start) if fallback
        is enabled.
        """
        in_window = [s for s in slots if self.config.window_contains(s.time)]
        if in_window:
            return in_window[0], True

        if not self.config.fallback_to_next_best or not slots:
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
        """Click into a slot, set players, and confirm the reservation."""
        assert self.page
        for sel in self.selectors["slot_row"]:
            loc = self.page.locator(sel)
            if await loc.count() > slot.handle_index:
                row = loc.nth(slot.handle_index)
                break
        else:
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
