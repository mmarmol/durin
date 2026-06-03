"""Gateway log file sink: JSONL + size rotation + gz + age retention.

The gateway's structured log lands in ``gateway.log`` as one JSON line
per event (loguru ``serialize=True``). Rotated segments are gz-compressed
and deleted past ``retention_days``. The human-readable stderr sink set up
elsewhere is untouched — this adds the FILE sink only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

__all__ = ["configure_gateway_file_logging", "install_excepthook"]


def configure_gateway_file_logging(
    log_file: Path,
    *,
    max_file_mb: int,
    retention_days: int,
) -> int:
    """Add a JSONL rotating/compressing file sink. Returns the sink id."""
    return logger.add(
        str(log_file),
        serialize=True,                      # one JSON object per line
        rotation=f"{max_file_mb} MB",
        retention=f"{retention_days} days",
        compression="gz",                    # rotated segments -> .gz
        level="INFO",
        enqueue=True,                        # process/thread-safe writes
        backtrace=False,
        diagnose=False,
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )


def install_excepthook() -> None:
    """Route uncaught exceptions to loguru so they land in the JSONL sink."""
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.bind(channel="-").opt(exception=(exc_type, exc_value, exc_tb)).error(
            "uncaught exception"
        )

    sys.excepthook = _hook
