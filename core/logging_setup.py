"""Application logging — stdlib ``logging`` with real levels, a rotating file
log, and an optional callback handler that forwards records to the in-app Live
Logs panel.

Replaces ad-hoc ``print()`` + substring-based severity. Call ``setup_logging()``
once at startup (writes a banner + the rotating file log), then attach the GUI
with ``add_callback_handler(fn)`` so log records also surface in the Live Logs.
New code should ``get_logger(__name__).info(...)`` instead of printing.
"""
from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Callable
from pathlib import Path

APP_LOGGER = "rendermapper"

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def get_logger(name: str = "") -> logging.Logger:
    """Return a child of the app logger (e.g. ``get_logger(__name__)``)."""
    return logging.getLogger(f"{APP_LOGGER}.{name}" if name else APP_LOGGER)


class CallbackHandler(logging.Handler):
    """Forward records to a ``callback(level_name, message)`` — e.g. the Live
    Logs panel — so the GUI shows the same stream as the file log."""

    def __init__(self, callback: Callable[[str, str], None]) -> None:
        super().__init__()
        self._cb = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._cb(record.levelname.lower(), record.getMessage())
        except Exception:
            self.handleError(record)


def setup_logging(log_path: Path | str | None = None, level: int = logging.INFO,
                  version: str = "") -> logging.Logger:
    """Configure the app logger once: console + rotating file handler, with a
    startup banner. Idempotent — re-calling replaces existing handlers."""
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter(_FMT, _DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_path is not None:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            p, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)

    logger.propagate = False
    logger.info("%s", "=" * 60)
    logger.info("Render Mapper Pro %s — session start", version or "?")
    return logger


def add_callback_handler(callback: Callable[[str, str], None],
                         level: int = logging.INFO) -> CallbackHandler:
    """Route log records into a GUI callback (level_name, message). Returns the
    handler so it can later be removed."""
    handler = CallbackHandler(callback)
    handler.setLevel(level)
    logging.getLogger(APP_LOGGER).addHandler(handler)
    return handler
