"""Interactive setup: collect preferences + credentials, write config & .env."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from . import timeparse
from .config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH


def run_setup(console: Console,
              config_path: Path = DEFAULT_CONFIG_PATH,
              env_path: Path = DEFAULT_ENV_PATH) -> None:
    console.print(Panel.fit(
        "[bold green]Tenfore Tee-Time Booker — Setup[/bold green]\n"
        "Answer a few questions; I'll write [cyan]config.yaml[/cyan] and "
        "[cyan].env[/cyan] for you.",
        border_style="green",
    ))

    existing = {}
    if config_path.exists():
        with config_path.open() as fh:
            existing = yaml.safe_load(fh) or {}

    booking = existing.get("booking", {})
    scheduling = existing.get("scheduling", {})

    # ---- Date ----
    while True:
        raw_date = Prompt.ask(
            "[bold]Desired golf date[/bold] (e.g. 'Saturday, June 14' or 2026-06-14)",
            default=str(booking.get("desired_date", "")) or None,
        )
        try:
            parsed = timeparse.parse_date(raw_date)
            console.print(f"  → parsed as [green]{parsed.strftime('%A, %B %d, %Y')}[/green]")
            break
        except timeparse.ParseError as exc:
            console.print(f"  [red]Couldn't parse that ({exc}). Try again.[/red]")

    # ---- Time window ----
    while True:
        raw_window = Prompt.ask(
            "[bold]Preferred tee-time window[/bold] (e.g. 'between 7:00 AM and 9:00 AM')",
            default="between 07:00 and 09:00",
        )
        try:
            start, end = timeparse.parse_time_window(raw_window)
            if end < start:
                console.print("  [red]End time is before start time.[/red]")
                continue
            console.print(f"  → [green]{start:%H:%M} – {end:%H:%M}[/green]")
            break
        except timeparse.ParseError as exc:
            console.print(f"  [red]Couldn't parse that ({exc}). Try again.[/red]")

    # ---- Players ----
    players = IntPrompt.ask("[bold]Number of players[/bold] (1-4)",
                            default=int(booking.get("players", 4)))
    players = max(1, min(4, players))

    # ---- Courses ----
    default_courses = booking.get("courses", []) or []
    raw_courses = Prompt.ask(
        "[bold]Which course(s)?[/bold] Comma-separated, in order of preference "
        "(blank = single-course site)",
        default=", ".join(default_courses),
    )
    courses = [c.strip() for c in raw_courses.split(",") if c.strip()]
    if courses:
        console.print(f"  → {len(courses)} course(s): "
                      f"[green]{', '.join(courses)}[/green]")

    # ---- Priority ----
    console.print(
        "\n[bold]What matters more?[/bold]\n"
        "  [cyan]1[/cyan]) The COURSE — get a tee time at "
        f"[green]{courses[0] if courses else 'your course'}[/green], "
        "time is flexible\n"
        "  [cyan]2[/cyan]) The TIME — get a slot in your window, "
        "location is flexible (search all courses)"
    )
    default_choice = "1" if str(booking.get("priority", "time")) == "course" else "2"
    choice = Prompt.ask("Choose", choices=["1", "2"], default=default_choice)
    priority = "course" if choice == "1" else "time"
    if priority == "course" and not courses:
        console.print("  [yellow]Course priority needs a course — none given, so "
                      "this will just target the single-course site.[/yellow]")

    # ---- Scheduling ----
    unlock_days = IntPrompt.ask("[bold]Booking unlocks how many days before play?[/bold]",
                                default=int(scheduling.get("unlock_days_before", 8)))
    raw_unlock_time = Prompt.ask(
        "[bold]Time of day the booking window opens[/bold] (24h, e.g. 00:00 or 07:00)",
        default=str(scheduling.get("unlock_time", "00:00")),
    )
    try:
        unlock_time = timeparse.parse_time(raw_unlock_time)
        unlock_time_str = f"{unlock_time:%H:%M}"
    except timeparse.ParseError:
        unlock_time_str = "00:00"

    timezone = Prompt.ask("[bold]Timezone[/bold] (IANA name, blank = system local)",
                          default=str(scheduling.get("timezone", "America/New_York")))

    # ---- Build config ----
    config = {
        "booking": {
            "desired_date": raw_date,
            "time_window": {"start": f"{start:%H:%M}", "end": f"{end:%H:%M}"},
            "players": players,
            "courses": courses,
            "priority": priority,
        },
        "scheduling": {
            "unlock_days_before": unlock_days,
            "unlock_time": unlock_time_str,
            "timezone": timezone,
            "prefire_seconds": int(scheduling.get("prefire_seconds", 60)),
            "poll_interval_seconds": float(scheduling.get("poll_interval_seconds", 1.5)),
        },
        "retry": existing.get("retry", {
            "login_attempts": 10,
            "login_delay_seconds": 2,
            "booking_window_minutes": 5,
            "fallback_to_next_best": True,
        }),
        "browser": existing.get("browser", {"headless": True, "timeout_ms": 30000}),
        "notifications": existing.get("notifications", {"macos_desktop": True}),
        "paths": existing.get("paths", {"log_dir": "logs", "screenshot_dir": "screenshots"}),
        "site": existing.get("site", {"base_url": "https://www.fox.tenfore.golf"}),
    }

    with config_path.open("w") as fh:
        fh.write("# Generated by `python main.py setup`\n")
        yaml.safe_dump(config, fh, sort_keys=False, default_flow_style=False)
    console.print(f"[green]✓[/green] Wrote {config_path}")

    # Show the computed unlock moment for confirmation.
    unlock_date = parsed - dt.timedelta(days=unlock_days)
    console.print(Panel.fit(
        f"Booking will unlock on [bold]{unlock_date.strftime('%A, %B %d, %Y')}"
        f" at {unlock_time_str}[/bold] ({timezone or 'system local'}).\n"
        f"The tool will start polling ~{config['scheduling']['prefire_seconds']}s before that.",
        title="When to run", border_style="cyan",
    ))

    # ---- Credentials ----
    if Confirm.ask("\n[bold]Store Tenfore credentials in .env now?[/bold]", default=True):
        email = Prompt.ask("  Tenfore email")
        password = Prompt.ask("  Tenfore password", password=True)
        env_path.write_text(
            f"TENFORE_EMAIL={email}\nTENFORE_PASSWORD={password}\n", encoding="utf-8"
        )
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        console.print(f"[green]✓[/green] Wrote {env_path} (permissions 600, gitignored)")
    else:
        console.print("[yellow]Skipped credentials. Create .env from .env.example "
                      "before running.[/yellow]")

    console.print("\n[bold green]Setup complete![/bold green] Run the booker with: "
                  "[cyan]python main.py run[/cyan]")
