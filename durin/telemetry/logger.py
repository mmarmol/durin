"""Append-only JSON-lines telemetry logger.

Writes one .jsonl file per session under ~/.cache/durin/telemetry/.
Each line is a self-contained event with timestamp, type, and payload.
Zero external dependencies — pure stdlib JSON + file append.

Audit A8 (2026-05-28): the logger gained an `extra_sinks` list so an
optional HTTPS push (`PushSink`) can fan out the same events to a
remote endpoint. The JSONL local persistence is ALWAYS the primary
sink; extra_sinks are additive and isolated — failures there never
affect the JSONL write. See doc 07 §12.2.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".cache" / "durin" / "telemetry"
_MAX_EVENTS_PER_FILE = 10_000


class _Sink(Protocol):
    """Anything that can absorb a (type, data) event.

    The concrete consumer today is ``durin.telemetry.push.PushSink``;
    future sinks (e.g. an in-memory ring buffer for tests, a metrics
    exporter) can plug in by implementing this protocol.
    """

    def log(self, event_type: str, data: dict[str, Any]) -> None: ...


class TelemetryLogger:
    """Append-only structured event logger for a single session.

    A8: ``extra_sinks`` carries additional consumers (e.g. PushSink).
    ``log()`` writes to the JSONL file FIRST (canonical persistence),
    then iterates the extra sinks. Any sink raising an exception is
    logged and skipped — telemetry must never break the calling tool.
    """

    def __init__(self, path: Path, *, session_key: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._count = 0
        self._extra_sinks: list[_Sink] = []
        # F20 (audit third pass, 2026-05-28): identity fields the
        # emit_tool_event helper auto-injects into every payload so
        # dashboards can join cross-event by (session_key, iteration)
        # without each callsite stamping the IDs by hand.
        self._session_key = session_key
        self._iteration = 0

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def iteration(self) -> int:
        return self._iteration

    def set_iteration(self, iteration: int) -> None:
        """Update the per-turn counter. AgentLoop calls this from its
        `on_iteration` callback so subsequent `emit_tool_event` calls
        in this turn carry the right value."""
        self._iteration = int(iteration)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def extra_sinks(self) -> list[_Sink]:
        """The list of fan-out sinks. Exposed so the agent loop can
        call ``flush()`` on each during shutdown."""
        return self._extra_sinks

    def add_sink(self, sink: _Sink) -> None:
        """Register an additional sink (e.g. PushSink) that receives
        every event after the JSONL write."""
        self._extra_sinks.append(sink)

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
        # A8: fan out to extra sinks (e.g. PushSink). Isolation: each
        # sink runs in its own try/except so a failure in one (network
        # down, endpoint 5xx, etc.) never affects the JSONL write or
        # the other sinks.
        for sink in self._extra_sinks:
            try:
                sink.log(event_type, data or {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "telemetry sink %s.log() raised; event dropped for "
                    "this sink only: %s",
                    type(sink).__name__, exc,
                )

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
    return TelemetryLogger(target_dir / filename, session_key=session_key)


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
