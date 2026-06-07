#!/usr/bin/env python3
"""Tenfore Golf tee-time auto-booking CLI.

Subcommands:
  setup   Interactive wizard -> writes config.yaml and .env
  run     Wait for the booking window, then race to book your tee time
  info    Show the resolved config and the next booking-unlock moment

Run `python main.py <command> --help` for command-specific flags.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tenfore import __version__
from tenfore.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_ENV_PATH,
    load_config,
    load_credentials,
)
from tenfore.logsetup import setup_logging
from tenfore.runner import run_booking
from tenfore.setup_wizard import run_setup

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tenfore",
        description="Auto-book tee times on the Tenfore Golf platform.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH,
                        help="Path to .env credentials file (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose (debug-level) console output.")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Interactive setup wizard.")

    run_p = sub.add_parser("run", help="Wait for the window and book a tee time.")
    _add_overrides(run_p)
    run_p.add_argument("--now", action="store_true",
                       help="Skip the wait/countdown and try to book immediately.")
    run_p.add_argument("--headful", dest="headless", action="store_false", default=None,
                       help="Show the browser window (for debugging).")

    info_p = sub.add_parser("info", help="Show resolved config and unlock time.")
    _add_overrides(info_p)

    return parser


def _add_overrides(p: argparse.ArgumentParser) -> None:
    """CLI flags that override config.yaml at runtime."""
    p.add_argument("--date", help="Override desired play date (e.g. 'Saturday, June 14').")
    p.add_argument("--time-window", dest="time_window",
                   help="Override window (e.g. 'between 7:00 AM and 9:00 AM').")
    p.add_argument("--players", type=int, help="Override number of players (1-4).")
    p.add_argument("--timezone", help="Override IANA timezone.")
    p.add_argument("--prefire-seconds", dest="prefire_seconds", type=int,
                   help="Seconds before unlock to start polling.")


def _overrides_from_args(args: argparse.Namespace) -> dict:
    return {
        "date": getattr(args, "date", None),
        "time_window": getattr(args, "time_window", None),
        "players": getattr(args, "players", None),
        "timezone": getattr(args, "timezone", None),
        "prefire_seconds": getattr(args, "prefire_seconds", None),
        "headless": getattr(args, "headless", None),
    }


def _print_info(config) -> None:
    table = Table(title="Tenfore Booker — Resolved Configuration", show_header=False)
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    table.add_row("Play date", config.desired_date.strftime("%A, %B %d, %Y"))
    table.add_row("Time window", f"{config.window_start:%H:%M} – {config.window_end:%H:%M}")
    table.add_row("Players", str(config.players))
    table.add_row("Timezone", str(config.timezone))
    table.add_row("Unlocks", config.unlock_datetime.strftime("%A, %B %d, %Y at %H:%M:%S %Z"))
    table.add_row("Poll starts", f"{config.prefire_seconds}s before unlock")
    table.add_row("Now", config.now().strftime("%Y-%m-%d %H:%M:%S %Z"))
    remaining = config.unlock_datetime - config.now()
    table.add_row("Time to unlock", str(remaining).split(".")[0])
    table.add_row("Login retries", f"{config.login_attempts} × {config.login_delay_seconds}s")
    table.add_row("Fallback booking", "on" if config.fallback_to_next_best else "off")
    table.add_row("Headless", "yes" if config.headless else "no")
    table.add_row("Site", config.base_url)
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "setup":
        try:
            run_setup(console, args.config, args.env)
            return 0
        except KeyboardInterrupt:
            console.print("\n[yellow]Setup cancelled.[/yellow]")
            return 130

    # run / info both need a resolved config.
    try:
        config = load_config(args.config, _overrides_from_args(args))
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        return 2

    setup_logging(config.log_dir, console, verbose=args.verbose)

    if args.command == "info":
        _print_info(config)
        return 0

    if args.command == "run":
        try:
            credentials = load_credentials(args.env)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return 2

        console.print(Panel.fit(
            f"[bold]Target:[/bold] {config.desired_date.strftime('%A, %B %d, %Y')}  "
            f"[bold]Window:[/bold] {config.window_start:%H:%M}–{config.window_end:%H:%M}  "
            f"[bold]Players:[/bold] {config.players}",
            title="🏌️  Tenfore Tee-Time Booker", border_style="green",
        ))

        try:
            result = asyncio.run(
                run_booking(config, credentials, console, skip_wait=args.now)
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]Aborted by user.[/yellow]")
            return 130

        return 0 if result.success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
