"""User notifications: Rich terminal output + optional macOS desktop alerts."""

from __future__ import annotations

import platform
import shutil
import subprocess

from rich.console import Console
from rich.panel import Panel

from .logsetup import get_logger


class Notifier:
    def __init__(self, console: Console, *, macos_desktop: bool = True):
        self.console = console
        self.macos_desktop = macos_desktop
        self.log = get_logger()

    def success(self, title: str, message: str) -> None:
        self.console.print(
            Panel(message, title=f"✅ {title}", border_style="green", expand=False)
        )
        self.log.info("SUCCESS: %s - %s", title, message)
        self._desktop(title, message, sound="Glass")

    def failure(self, title: str, message: str) -> None:
        self.console.print(
            Panel(message, title=f"❌ {title}", border_style="red", expand=False)
        )
        self.log.error("FAILURE: %s - %s", title, message)
        self._desktop(title, message, sound="Basso")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]⚠ {message}[/yellow]")
        self.log.warning(message)

    def info(self, message: str) -> None:
        self.console.print(f"[cyan]ℹ {message}[/cyan]")
        self.log.info(message)

    def _desktop(self, title: str, message: str, *, sound: str | None = None) -> None:
        """Fire a macOS desktop notification via osascript. No-op elsewhere."""
        if not self.macos_desktop:
            return
        if platform.system() != "Darwin" or not shutil.which("osascript"):
            return
        # Escape backslashes first (so we don't double-escape), then quotes/newlines.
        def _osa_safe(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")
        safe_msg = _osa_safe(message)
        safe_title = _osa_safe(title)
        script = f'display notification "{safe_msg}" with title "Tenfore Booker — {safe_title}"'
        if sound:
            script += f' sound name "{sound}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        except Exception as exc:  # never let a notification crash the run
            self.log.debug("Desktop notification failed: %s", exc)
