"""Append-only JSON-lines telemetry logger.

Writes one .jsonl file per session under ~/.cache/durin/telemetry/.
Each line is a self-contained event with timestamp, type, and payload.
Zero external dependencies — pure stdlib JSON + file append.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path.home() / ".cache" / "durin" / "telemetry"
_MAX_EVENTS_PER_FILE = 10_000


class TelemetryLogger:
    """Append-only structured event logger for a single session."""

    __slots__ = ("_path", "_count")

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if self._count >= _MAX_EVENTS_PER_FILE:
            return
        entry = {
            "ts": time.time(),
            "type": event_type,
        }
        if data:
            entry["data"] = data
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
        self._count += 1

    def log_rate_limit(
        self,
        attempt: int,
        delay_s: float,
        error_snippet: str,
        status_code: int | None = None,
        persistent: bool = False,
    ) -> None:
        self.log("provider.rate_limit", {
            "attempt": attempt,
            "delay_s": round(delay_s, 1),
            "status_code": status_code,
            "persistent": persistent,
            "error": error_snippet[:200],
        })

    def log_rate_limit_exhausted(
        self,
        attempts: int,
        error_snippet: str,
    ) -> None:
        self.log("provider.rate_limit_exhausted", {
            "attempts": attempts,
            "error": error_snippet[:200],
        })


def get_session_logger(
    session_key: str,
    base_dir: Path | None = None,
) -> TelemetryLogger:
    """Get or create a telemetry logger for a session.

    File naming: sanitized session key + date suffix for rotation.
    """
    import re
    from datetime import date

    target_dir = base_dir or _DEFAULT_DIR
    safe_key = re.sub(r"[^\w\-]", "_", session_key)[:80]
    safe_key = re.sub(r"\.{2,}", "_", safe_key)
    today = date.today().isoformat()
    filename = f"{safe_key}_{today}.jsonl"
    return TelemetryLogger(target_dir / filename)


# Per-task telemetry binding. Mirrors the file_state ContextVar pattern so a
# tool can resolve the active session's logger at execution time without having
# to thread it through every constructor. AgentLoop binds the session logger
# before invoking the runner and resets the token on exit.
_current_logger: ContextVar[TelemetryLogger | None] = ContextVar(
    "durin_telemetry_logger",
    default=None,
)


def current_telemetry() -> TelemetryLogger | None:
    """Return the TelemetryLogger bound to the current task, or None."""
    return _current_logger.get()


def bind_telemetry(logger: TelemetryLogger) -> Token[TelemetryLogger | None]:
    """Bind a telemetry logger for the current async task."""
    return _current_logger.set(logger)


def reset_telemetry(token: Token[TelemetryLogger | None]) -> None:
    _current_logger.reset(token)
