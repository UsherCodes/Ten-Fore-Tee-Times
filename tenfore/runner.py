"""Orchestrates a full booking run: wait → poll → login → book → notify."""

from __future__ import annotations

import asyncio
import datetime as dt

from rich.console import Console

from .booker import BookingResult, TenforeBooker
from .config import BookingConfig, Credentials
from .logsetup import get_logger
from .notifier import Notifier
from .scheduler import wait_until_prefire


async def run_booking(config: BookingConfig,
                      credentials: Credentials,
                      console: Console,
                      *,
                      skip_wait: bool = False) -> BookingResult:
    """Execute the end-to-end booking flow and return the result."""
    log = get_logger()
    notifier = Notifier(console, macos_desktop=config.macos_desktop)

    if not skip_wait:
        await wait_until_prefire(config, console)

    booker = TenforeBooker(config, credentials)
    result = BookingResult(success=False)
    try:
        await booker.start()

        if not await booker.login():
            result.message = "Login failed after all retry attempts."
            notifier.failure("Login Failed", result.message)
            return result

        if not await booker.open_booking_page():
            result.message = ("Booking page never opened within the retry window. "
                              "It may not have unlocked yet, or the site is down.")
            notifier.failure("Booking Unavailable", result.message)
            return result

        # Announce the strategy we're running with.
        courses = config.courses_to_search()
        named = [c for c in courses if c]
        if config.priority == "course":
            notifier.info(
                f"Priority: COURSE — locking onto "
                f"{config.primary_course or 'this course'} (time is flexible)."
            )
        else:
            scope = ", ".join(named) if named else "all available courses"
            notifier.info(
                f"Priority: TIME — earliest slot in your window wins across "
                f"{scope} (location is flexible)."
            )

        # The window has opened; date/course may still need selecting, and slots
        # may appear a beat late — poll briefly across the target courses.
        slots = []
        deadline = config.now() + dt.timedelta(minutes=config.booking_window_minutes)
        while config.now() < deadline:
            slots = await booker.gather_slots(courses)
            if slots:
                break
            log.info("No slots visible yet — refreshing in %.1fs.",
                     config.poll_interval_seconds)
            try:
                await booker.page.reload(wait_until="domcontentloaded")  # type: ignore
            except Exception as exc:
                log.debug("Reload during slot poll failed: %s", exc)
            await asyncio.sleep(config.poll_interval_seconds)

        if not slots:
            where = "any of your courses" if len(named) > 1 else "the date"
            result.message = f"No available tee times were found for {where}."
            notifier.failure("No Tee Times", result.message)
            await _safe_shot(booker, "no-slots", result)
            return result

        # Try the best slot; if it's snatched between gather and confirm, drop it
        # and try the next-best, a few times (booking is a race).
        candidates = list(slots)
        booked = False
        slot = None
        in_window = False
        for _ in range(min(5, len(candidates) + 1)):
            slot, in_window = booker.choose_slot(candidates)
            if slot is None:
                result.message = (
                    "All slots in your preferred window are taken and fallback "
                    "booking is disabled."
                )
                notifier.failure("Preferred Window Full", result.message)
                await _safe_shot(booker, "window-full", result)
                return result

            if not in_window:
                at = f" at {slot.course}" if slot.course else ""
                if config.priority == "course":
                    notifier.warn(
                        f"No slot in your window; time is flexible, so booking the "
                        f"next-best available{at}: {slot.label}."
                    )
                else:
                    notifier.warn(
                        f"Preferred window ({config.window_start:%H:%M}–"
                        f"{config.window_end:%H:%M}) was full everywhere. "
                        f"Booking next-best{at}: {slot.label}."
                    )

            at = f" at {slot.course}" if slot.course else ""
            log.info("Attempting to book %s%s (%d players).",
                     slot.label, at, config.players)
            if await booker.book_slot(slot):
                booked = True
                break
            log.warning("Could not secure %s%s — trying the next-best slot.",
                        slot.label, at)
            candidates.remove(slot)

        shot = await _safe_shot(booker, "booking", result)
        result.booked_time = slot.time if slot else None
        result.booked_label = slot.label if slot else None
        result.booked_course = slot.course if slot else None
        result.in_preferred_window = in_window
        result.screenshot_path = shot

        if booked and slot:
            result.success = True
            window_note = ("within your preferred window" if in_window
                           else "next-best slot")
            where = f" at {slot.course}" if slot.course else ""
            result.message = (
                f"Booked {slot.label}{where} for {config.players} player(s) on "
                f"{config.desired_date.strftime('%A, %B %d, %Y')} — {window_note}."
                + (f"\nScreenshot: {shot}" if shot else "")
            )
            notifier.success("Tee Time Booked!", result.message)
        else:
            result.message = ("Could not confirm a booking after several attempts. "
                              "Check the screenshot and your Tenfore account.")
            notifier.failure("Booking Unconfirmed", result.message)

        return result

    except Exception as exc:  # last-resort guard
        log.exception("Unexpected error during booking run.")
        result.message = f"Unexpected error: {exc}"
        notifier.failure("Run Error", result.message)
        await _safe_shot(booker, "error", result)
        return result
    finally:
        await booker.close()


async def _safe_shot(booker: TenforeBooker, name: str, result: BookingResult):
    try:
        path = await booker.screenshot(name)
        result.screenshot_path = result.screenshot_path or path
        return path
    except Exception:
        return None
