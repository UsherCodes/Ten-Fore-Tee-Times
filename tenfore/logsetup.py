"""Logging configuration: timestamped file log + Rich console handler."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "tenfore"


def setup_logging(log_dir: Path, console: Console, *, verbose: bool = False) -> logging.Logger:
    """Configure and return the package logger.

    All attempts (login, polling, booking) are written with timestamps to
    ``log_dir/tenfore.log`` (appended across runs — no rotation) and mirrored
    to the console.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "tenfore.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    console_handler = RichHandler(
        console=console,
        show_path=False,
        rich_tracebacks=True,
        markup=True,
    )
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console_handler)

    logger.debug("Logging initialised -> %s", logfile)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
