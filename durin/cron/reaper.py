"""Reap stale per-run cron and loop-judge sessions.

Every agent_turn cron execution records into a fresh session keyed
``cron:{id}:run:{ms}`` (see ``CronService._execute_job``), and every loop
judge call records into a fresh session keyed ``loop:judge:run:{ms}`` (see
the ``_loop_judge`` closure in ``durin.cli.commands``). These accumulate
forever; ``config.cron.run_session_retention_hours`` bounds how long they are
kept. The selection logic lives here as a pure function so it is testable
without a live ``SessionManager``; the daily ``memory_dream`` cron pass calls
``reap_expired_run_sessions`` to do the deletions.
"""

from __future__ import annotations

import re
import time
from typing import Any

from loguru import logger

# cron:{id}:run:{ms} or loop:judge:run:{ms}  — the run timestamp is the
# trailing all-digits segment.
_RUN_KEY_RE = re.compile(r"^(?:cron:[^:]+|loop:judge):run:(\d+)$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def select_expired_run_sessions(
    sessions: list[dict[str, Any]],
    retention_hours: int,
    now_ms: int | None = None,
) -> list[str]:
    """Return the keys of per-run cron sessions older than the retention window.

    Only keys of the exact shape ``cron:{id}:run:{ms}`` are considered; the
    run timestamp parsed from the key suffix is compared against
    ``now_ms - retention_hours``. A ``retention_hours`` of ``0`` disables
    reaping (returns ``[]``). Malformed run suffixes are ignored.
    """
    if retention_hours <= 0:
        return []
    if now_ms is None:
        now_ms = _now_ms()
    cutoff_ms = now_ms - retention_hours * 3_600_000
    expired: list[str] = []
    for session in sessions:
        key = session.get("key")
        if not isinstance(key, str):
            continue
        m = _RUN_KEY_RE.match(key)
        if m is None:
            continue
        run_ms = int(m.group(1))
        if run_ms < cutoff_ms:
            expired.append(key)
    return expired


def reap_expired_run_sessions(
    session_manager: Any,
    retention_hours: int,
    now_ms: int | None = None,
) -> int:
    """Delete per-run cron sessions older than the retention window.

    Lists sessions via ``session_manager.list_sessions()`` and deletes each
    expired ``cron:{id}:run:{ms}`` key. Returns the number deleted. Best-effort:
    a failed delete is logged and skipped.
    """
    if retention_hours <= 0:
        return 0
    try:
        sessions = session_manager.list_sessions()
    except Exception:
        logger.exception("cron reaper: could not list sessions")
        return 0
    expired = select_expired_run_sessions(sessions, retention_hours, now_ms)
    deleted = 0
    for key in expired:
        try:
            if session_manager.delete_session(key):
                deleted += 1
        except Exception:
            logger.exception("cron reaper: failed to delete session {}", key)
    if deleted:
        logger.info(
            "cron reaper: deleted {} per-run session(s) older than {}h",
            deleted, retention_hours,
        )
    return deleted
