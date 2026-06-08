"""End-to-end Playwright smoke test against a local mock booking page.

No real network, no credentials, no Tenfore — it loads a self-contained HTML
fixture and drives the real TenforeBooker methods (collect_slots, choose_slot,
book_slot) to prove the browser automation actually works.

Run (after `python -m playwright install chromium`):
    python tests/smoke_browser.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tenfore.booker import TenforeBooker  # noqa: E402
from tenfore.config import BookingConfig, Credentials  # noqa: E402
from tenfore.logsetup import setup_logging  # noqa: E402
from rich.console import Console  # noqa: E402

MOCK_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Mock Tenfore Booking</title></head><body>
<h1>Tee Times — Saturday</h1>
<a href="#" id="logout">Logout</a>
<div class="teetime"><span>06:40 AM</span><button class="book-btn">Book</button></div>
<div class="teetime"><span>07:20 AM</span><button class="book-btn">Book</button></div>
<div class="teetime"><span>08:00 AM</span><button class="book-btn">Book</button></div>
<div class="teetime"><span>08:40 AM</span> FULL</div>
<div class="teetime"><span>10:00 AM</span><button class="book-btn">Book</button></div>
<script>
document.querySelectorAll('.book-btn').forEach(b => b.onclick = () => {
  document.body.innerHTML =
    '<select name="players"><option>1</option><option>2</option>'
    + '<option>3</option><option>4</option></select>'
    + '<button id="confirm">Confirm Booking</button>'
    + '<div id="done" style="display:none">Booking confirmed! Reference ABC123</div>';
  document.getElementById('confirm').onclick = () =>
    document.getElementById('done').style.display = 'block';
});
</script></body></html>"""


def _config(tmp: Path, *, dry_run: bool = False) -> BookingConfig:
    return BookingConfig(
        desired_date=dt.date(2026, 6, 14),
        window_start=dt.time(7, 0), window_end=dt.time(9, 0), players=2,
        courses=[], priority="time",
        unlock_days_before=8, unlock_time=dt.time(0, 0), timezone=ZoneInfo("UTC"),
        prefire_seconds=60, poll_interval_seconds=0.2, selection_budget_seconds=3,
        login_attempts=3,
        login_delay_seconds=0.2, booking_window_minutes=1, fallback_to_next_best=True,
        headless=True, timeout_ms=10000, macos_desktop=False,
        log_dir=tmp / "logs", screenshot_dir=tmp / "shots",
        base_url="https://example.invalid", dry_run=dry_run, raw={},
    )


async def main() -> int:
    console = Console()
    tmp = Path(tempfile.mkdtemp(prefix="tenfore-smoke-"))
    setup_logging(tmp / "logs", console)

    page_file = tmp / "booking.html"
    page_file.write_text(MOCK_PAGE, encoding="utf-8")

    # --- Part 1: DRY RUN — must NOT actually confirm ---
    dry = TenforeBooker(_config(tmp, dry_run=True), Credentials("a@b.com", "pw"))
    try:
        await dry.start()
        await dry.page.goto(page_file.as_uri())
        slot, _ = dry.choose_slot(await dry.collect_slots())
        assert await dry.book_slot(slot), "dry-run book_slot should report success"
        body = (await dry.page.inner_text("body")).lower()
        assert "confirmed" not in body, "DRY RUN must not reach the confirmation!"
        console.print("[green]✓ dry-run validated path without confirming[/green]")
    except AssertionError as exc:
        console.print(f"[bold red]SMOKE TEST FAILED (dry run):[/bold red] {exc}")
        return 1
    finally:
        await dry.close()

    # --- Part 2: real booking against a fresh page load ---
    booker = TenforeBooker(_config(tmp), Credentials("a@b.com", "pw"))
    try:
        await booker.start()
        await booker.page.goto(page_file.as_uri())

        slots = await booker.collect_slots()
        assert len(slots) == 4, f"expected 4 bookable slots (one FULL skipped), got {len(slots)}"
        assert [s.time for s in slots][:2] == [dt.time(6, 40), dt.time(7, 20)]

        slot, in_window = booker.choose_slot(slots)
        assert in_window and slot.time == dt.time(7, 20), "earliest in-window slot should win"

        booked = await booker.book_slot(slot)
        assert booked, "book_slot should report success"
        body = (await booker.page.inner_text("body")).lower()
        assert "confirmed" in body, "real booking should reach confirmation"

        shot = await booker.screenshot("smoke")
        assert shot.exists()

        console.print("[bold green]SMOKE TEST PASSED[/bold green] — "
                      f"booked {slot.label}, screenshot at {shot}")
        return 0
    except AssertionError as exc:
        console.print(f"[bold red]SMOKE TEST FAILED:[/bold red] {exc}")
        return 1
    finally:
        await booker.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
