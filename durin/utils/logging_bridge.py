"""Utilities for redirecting stdlib logging to loguru."""
from __future__ import annotations

import logging

from loguru import logger


class _LoguruBridge(logging.Handler):
    """Route stdlib log records into loguru with consistent formatting."""

    _LEVEL_MAP: dict[int, str] = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def __init__(self, lib_name: str | None = None) -> None:
        super().__init__()
        self.lib_name = lib_name

    def emit(self, record: logging.LogRecord) -> None:
        level = self._LEVEL_MAP.get(record.levelno, "INFO")
        # A fixed lib_name labels a single redirected library; when None
        # (the root/durin bridge) fall back to the record's own logger
        # name so each submodule stays identifiable in gateway.log.
        label = self.lib_name or record.name
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame, depth = frame.f_back, depth + 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, "[{lib}] {message}", lib=label, message=record.getMessage()
        )


def redirect_lib_logging(name: str, level: str | None = None) -> None:
    """Redirect stdlib logging from *name* into loguru.

    Adds a bridge handler if one is not already present and disables
    propagation so messages are not duplicated.  When *level* is None the
    handler does not filter — loguru's own level controls visibility.
    """
    lib_logger = logging.getLogger(name)
    if not any(isinstance(h, _LoguruBridge) for h in lib_logger.handlers):
        handler = _LoguruBridge(name)
        if level is not None:
            handler.setLevel(getattr(logging, level.upper(), logging.WARNING))
        lib_logger.handlers = [handler]
        lib_logger.propagate = False


def redirect_durin_logging(level: str = "INFO") -> None:
    """Route durin's own stdlib loggers (``durin.*``) into loguru.

    Many subsystems (memory, security, several tools, telemetry) create
    their logger with ``logging.getLogger(__name__)`` rather than loguru.
    Without this bridge those records never reach loguru's sinks: their
    INFO is dropped by stdlib's WARNING-default root level, and their
    WARNING/ERROR fall through to stdlib's last-resort stderr handler —
    which in daemon mode lands in ``gateway.boot.log`` (plain text,
    excluded from the dashboard Logs panel) instead of the structured
    ``gateway.log``.

    Installing the bridge on the ``durin`` parent logger captures every
    ``durin.<module>`` record and forwards it to loguru (and thus
    gateway.log), without pulling in unrelated third-party libraries.
    The logger level is lowered to *level* so INFO records propagate to
    the handler; loguru's own sink level decides final visibility.
    Idempotent — safe to call more than once per process.
    """
    durin_logger = logging.getLogger("durin")
    if not any(isinstance(h, _LoguruBridge) for h in durin_logger.handlers):
        durin_logger.handlers = [_LoguruBridge()]
    durin_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    durin_logger.propagate = False
