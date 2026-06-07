"""Orchestrates a full booking run: wait → poll → login → book → notify."""

from __future__ import annotations

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

        # The window has opened; date may still need selecting, and slots may
        # appear a beat late — poll briefly for slots.
        await booker.select_date()

        slots = []
        deadline = config.now() + dt.timedelta(minutes=config.booking_window_minutes)
        while config.now() < deadline:
            slots = await booker.collect_slots()
            if slots:
                break
            log.info("No slots visible yet — refreshing in %.1fs.",
                     config.poll_interval_seconds)
            try:
                await booker.page.reload(wait_until="domcontentloaded")  # type: ignore
                await booker.select_date()
            except Exception as exc:
                log.debug("Reload during slot poll failed: %s", exc)
            import asyncio
            await asyncio.sleep(config.poll_interval_seconds)

        if not slots:
            result.message = "No available tee times were found for the date."
            notifier.failure("No Tee Times", result.message)
            await _safe_shot(booker, "no-slots", result)
            return result

        slot, in_window = booker.choose_slot(slots)
        if slot is None:
            result.message = ("All slots in your preferred window are taken and "
                              "fallback booking is disabled.")
            notifier.failure("Preferred Window Full", result.message)
            await _safe_shot(booker, "window-full", result)
            return result

        if not in_window:
            notifier.warn(
                f"Preferred window ({config.window_start:%H:%M}–{config.window_end:%H:%M}) "
                f"was full. Booking next-best available: {slot.label}."
            )

        log.info("Attempting to book %s (%d players).", slot.label, config.players)
        booked = await booker.book_slot(slot)
        shot = await _safe_shot(booker, "booking", result)

        result.booked_time = slot.time
        result.booked_label = slot.label
        result.in_preferred_window = in_window
        result.screenshot_path = shot

        if booked:
            result.success = True
            window_note = ("within your preferred window" if in_window
                           else "next-best (preferred window was full)")
            result.message = (
                f"Booked {slot.label} for {config.players} player(s) on "
                f"{config.desired_date.strftime('%A, %B %d, %Y')} — {window_note}."
                + (f"\nScreenshot: {shot}" if shot else "")
            )
            notifier.success("Tee Time Booked!", result.message)
        else:
            result.message = (f"Attempted to book {slot.label} but could not confirm. "
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
