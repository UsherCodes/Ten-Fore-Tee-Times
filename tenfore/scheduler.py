"""Precision timing: sleep until just before the booking window opens."""

from __future__ import annotations

import asyncio
import datetime as dt

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .config import BookingConfig
from .logsetup import get_logger


def _fmt_delta(delta: dt.timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{sign}{days}d {hours:02d}h {mins:02d}m {secs:02d}s"
    return f"{sign}{hours:02d}h {mins:02d}m {secs:02d}s"


async def wait_until_prefire(config: BookingConfig, console: Console) -> None:
    """Block until ``prefire_seconds`` before the unlock moment.

    Displays a live countdown. If the unlock moment (minus prefire) has already
    passed, returns immediately so we go straight into polling.
    """
    log = get_logger()
    unlock = config.unlock_datetime
    fire_at = unlock - dt.timedelta(seconds=config.prefire_seconds)

    log.info("Play date .......... %s", config.desired_date.strftime("%A, %B %d, %Y"))
    log.info("Booking unlocks .... %s", unlock.strftime("%Y-%m-%d %H:%M:%S %Z"))
    log.info("Polling starts ..... %s (%ss early)",
             fire_at.strftime("%Y-%m-%d %H:%M:%S %Z"), config.prefire_seconds)

    now = config.now()
    if now >= fire_at:
        log.info("Pre-fire moment already passed — entering polling loop now.")
        return

    with Live(console=console, refresh_per_second=4, transient=True) as live:
        while True:
            now = config.now()
            remaining = fire_at - now
            if remaining.total_seconds() <= 0:
                break

            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold cyan", justify="right")
            table.add_column()
            table.add_row("Play date", config.desired_date.strftime("%A, %B %d, %Y"))
            table.add_row("Window", f"{config.window_start:%H:%M}–{config.window_end:%H:%M}"
                          f"  ({config.players} players)")
            table.add_row("Unlocks at", unlock.strftime("%Y-%m-%d %H:%M:%S %Z"))
            table.add_row("Polling in", f"[bold yellow]{_fmt_delta(remaining)}[/bold yellow]")
            live.update(table)

            # Sleep in small slices so the countdown stays smooth and we never
            # overshoot the pre-fire moment.
            await asyncio.sleep(min(0.25, max(0.01, remaining.total_seconds())))

    log.info("Pre-fire reached — beginning rapid polling loop.")
