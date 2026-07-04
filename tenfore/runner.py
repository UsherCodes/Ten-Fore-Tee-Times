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
        attempted = 0
        for _ in range(min(5, len(candidates))):
            slot, in_window = booker.choose_slot(candidates)
            if slot is None:
                # Nothing acceptable on the very first pass means the window is
                # full and fallback is off. (If we'd already booked-attempted a
                # slot, we'd have left the loop via the exhausted path below.)
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
            attempted += 1
            if await booker.book_slot(slot):
                booked = True
                break
            log.warning("Could not secure %s%s — trying the next-best slot.",
                        slot.label, at)
            candidates.remove(slot)

        # Exhausted the attempt budget / candidate list without booking.
        if not booked and attempted:
            result.message = (
                f"Tried {attempted} slot(s) but none could be confirmed — they may "
                f"be selling out faster than we can book, or the confirm step isn't "
                f"matching. Check logs/tenfore.log and the screenshot."
            )
            notifier.failure("Could Not Secure a Slot", result.message)
            await _safe_shot(booker, "exhausted", result)
            return result

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
            verb = "Would have booked" if config.dry_run else "Booked"
            result.message = (
                f"{verb} {slot.label}{where} for {config.players} player(s) on "
                f"{config.desired_date.strftime('%A, %B %d, %Y')} — {window_note}."
                + ("\nDRY RUN — no reservation was actually made."
                   if config.dry_run else "")
                + (f"\nScreenshot: {shot}" if shot else "")
            )
            notifier.success(
                "Dry Run OK (not booked)" if config.dry_run else "Tee Time Booked!",
                result.message)
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


async def run_check(config: BookingConfig,
                    credentials: Credentials,
                    console: Console) -> bool:
    """Dry validation of the whole path WITHOUT booking anything.

    Logs in, opens the booking page, and for each target course selects the
    course + date and counts the slots it can see — so you can confirm the
    selectors work against the real site any day, not just at unlock time.
    Returns True if every checkpoint passed.
    """
    from rich.table import Table  # local import keeps module load light

    log = get_logger()
    booker = TenforeBooker(config, credentials)
    rows: list[tuple[str, bool, str]] = []

    def record(step: str, ok: bool, detail: str = "") -> None:
        rows.append((step, ok, detail))
        log.info("CHECK %-22s %s %s", step, "OK" if ok else "FAIL", detail)

    all_ok = True
    try:
        await booker.start()

        login_ok = await booker.login()
        record("Login", login_ok,
               "" if login_ok else "check credentials / 'login_*' selectors")
        all_ok &= login_ok

        if login_ok:
            page_ok = await booker.open_booking_page(minutes=1.5)
            record("Booking page", page_ok,
                   "" if page_ok else "not open yet, or 'booking_nav' selector")
            all_ok &= page_ok

            if page_ok:
                for course in config.courses_to_search():
                    label = course or "(single course)"
                    if course:
                        c_ok = await booker.select_course(course)
                        record(f"Select course: {label}", c_ok,
                               "" if c_ok else "add a 'course_select' override")
                        all_ok &= c_ok
                    d_ok = await booker.select_date()
                    record(f"Select date @ {label}", d_ok,
                           "" if d_ok else "page may already default to it")
                    slots = await booker.collect_slots(course=course)
                    in_win = [s for s in slots if config.window_contains(s.time)]
                    slot_ok = len(slots) > 0
                    if slots:
                        detail = (f"{len(slots)} slot(s), {len(in_win)} in window "
                                  f"— e.g. {', '.join(s.label for s in slots[:5])}")
                    elif await booker._looks_like_booking_page():
                        detail = ("0 slots — page looks right; the booking window "
                                  "for this date may simply not be open yet")
                    else:
                        detail = "no slot rows found — check the 'slot_row' selector"
                    record(f"Slots @ {label}", slot_ok, detail)
                    all_ok &= slot_ok

        shot = await booker.screenshot("check")
        log.info("Saved check screenshot -> %s", shot)
    except Exception as exc:
        log.exception("Check run errored.")
        record("Unexpected error", False, str(exc))
        all_ok = False
    finally:
        await booker.close()

    table = Table(title="Tenfore Booker — Pre-flight Check", show_lines=False)
    table.add_column("Step", style="bold")
    table.add_column("Result", justify="center")
    table.add_column("Detail", overflow="fold")
    for step, ok, detail in rows:
        table.add_row(step, "[green]✓[/green]" if ok else "[red]✗[/red]", detail)
    console.print(table)
    console.print(
        f"[bold green]All checks passed — you're ready to arm `run`.[/bold green]"
        if all_ok else
        "[bold yellow]Some checks failed.[/bold yellow] Re-run with [cyan]--headful[/cyan] "
        "to watch, and add selector overrides under [cyan]site.selectors[/cyan] "
        "in config.yaml (see the README)."
    )
    return all_ok
