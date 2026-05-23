"""Async-safe runner for the entity-centric dream pipeline (doc 25 §2.A.1).

Wraps :class:`DreamConsolidator` with the production concerns that
the manual ``durin memory dream`` command could ignore but the
auto-triggers (cron daily, post-compaction, session-close, threshold
per-entity) cannot:

- **Lock file** at ``memory/.dream.lock`` so two triggers firing
  within the same window don't both consolidate (race → divergent
  pages, double git commits).
- **Throttle** with ``min_seconds_between_runs`` to absorb bursts —
  e.g. a flurry of ``memory_store`` calls each crossing the entity
  threshold should not produce a dream per call.
- **Stale-lock recovery**: a crashed previous run leaves a lock
  behind; treat lock files older than ``STALE_LOCK_SECONDS`` as
  abandoned and overwrite them. PID inside the lock helps diagnostics.
- **Telemetry**: ``memory.dream.start``, ``memory.dream.end``,
  ``memory.dream.skipped`` so the §2.E aggregator (durin memory stats)
  can show cost-per-day and trigger distribution.

The runner is **synchronous**. Callers that need non-blocking
behaviour wrap with ``asyncio.to_thread`` (cron callback) or
``threading.Thread`` (write-path hooks). Keeping the runner sync makes
testing simpler and the lifecycle (lock acquire → run → release) easy
to reason about in one place.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from durin.agent.tools._telemetry import emit_tool_event

logger = logging.getLogger(__name__)

__all__ = [
    "DreamRunResult",
    "DreamRunner",
]


# Lock older than this is treated as stale (crashed previous process).
# 10 min covers the longest realistic dream run (per Phase 0.3:
# ~36s/consolidation × 16 entities + retries ≈ ~10min worst case).
_STALE_LOCK_SECONDS = 600

# Throttle bookkeeping lives alongside the lock so a single mtime
# observation tells us "did we run recently?" without parsing JSON.
_LOCK_FILENAME = ".dream.lock"
_LAST_RUN_FILENAME = ".dream.last_run"


@dataclass(frozen=True)
class DreamRunResult:
    """Outcome of one dream pass.

    ``ran`` is True iff the consolidator actually executed (lock held,
    entries processed). False means we returned early — ``reason``
    explains why so callers and the §2.E telemetry aggregator can
    distinguish "no work to do" from "throttled" from "concurrent run".
    """

    ran: bool
    reason: str
    entities_consolidated: int
    entities_failed: int
    duration_s: float


class DreamRunner:
    """Coordinates one entity-centric dream pass with lock + throttle."""

    def __init__(
        self,
        workspace: Path,
        *,
        min_seconds_between_runs: int = 300,
        model: str | None = None,
        vector_index: object | None = None,
        llm_invoke: Callable[..., str] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.memory_root = self.workspace / "memory"
        self.min_seconds_between_runs = max(0, int(min_seconds_between_runs))
        self.model = model
        self._vector_index = vector_index
        self._llm_invoke = llm_invoke

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        trigger: str = "manual",
        entity_filter: str | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> DreamRunResult:
        """Execute one dream pass.

        ``trigger`` is a free-text label recorded on every telemetry
        event so the §2.E aggregator can split usage by source
        (``cron_daily`` / ``post_compaction`` / ``session_close`` /
        ``threshold`` / ``manual``).

        ``entity_filter`` narrows the pass to one entity ref
        (``person:marcelo``) — used by the threshold trigger.

        ``on_progress`` is an optional ``(entity_ref, status_msg)``
        callback for CLI display. Telemetry events fire regardless.
        """
        start = time.monotonic()

        # 0. Throttle check (skips the lock work when we're cooling
        #    down — cheap path for bursty triggers).
        if self._is_throttled():
            self._emit_skipped(trigger, "throttle", entity_filter)
            return DreamRunResult(
                ran=False, reason="throttle",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        # 1. Discover pending consolidations. If none, exit before
        #    acquiring the lock — no point serializing readers when
        #    there's nothing to write.
        pending = self._discover_pending(entity_filter)
        if not pending:
            self._emit_skipped(trigger, "no_pending", entity_filter)
            return DreamRunResult(
                ran=False, reason="no_pending",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        # 2. Acquire the lock. If another process beat us to it,
        #    return without complaint — the other run will handle
        #    these entities (or the next trigger will pick up the
        #    leftover).
        acquired = self._acquire_lock(trigger)
        if not acquired:
            self._emit_skipped(trigger, "concurrent_lock", entity_filter)
            return DreamRunResult(
                ran=False, reason="concurrent_lock",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        consolidated = 0
        failed = 0
        try:
            self._emit_start(trigger, entity_filter, len(pending))
            consolidated, failed = self._consolidate(pending, on_progress)
        finally:
            self._release_lock()
            self._touch_last_run()

        duration = time.monotonic() - start
        self._emit_end(trigger, entity_filter, consolidated, failed, duration)
        return DreamRunResult(
            ran=True, reason="ok",
            entities_consolidated=consolidated,
            entities_failed=failed,
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def _discover_pending(self, entity_filter: str | None) -> dict[str, list[Any]]:
        """Reuse the CLI's discovery helper to keep the semantics
        identical between manual and auto-triggered runs."""
        from durin.cli.memory_cmd import _discover_pending_consolidations

        if not (self.memory_root / "episodic").exists():
            return {}
        return _discover_pending_consolidations(
            self.memory_root, entity_filter=entity_filter,
        )

    def _consolidate(
        self,
        pending: dict[str, list[Any]],
        on_progress: Callable[[str, str], None] | None,
    ) -> tuple[int, int]:
        from durin.memory.dream import DreamConsolidator, DreamError

        kwargs: dict[str, Any] = {"workspace": self.workspace}
        if self.model is not None:
            kwargs["model"] = self.model
        if self._vector_index is not None:
            kwargs["vector_index"] = self._vector_index
        if self._llm_invoke is not None:
            kwargs["llm_invoke"] = self._llm_invoke
        consolidator = DreamConsolidator(**kwargs)

        consolidated = 0
        failed = 0
        for ent_ref, entries in pending.items():
            try:
                result = consolidator.consolidate_entity(ent_ref, entries)
                sha = consolidator.apply(ent_ref, result)
                consolidated += 1
                if on_progress is not None:
                    msg = f"→ {sha[:8]}" if sha else "= no changes"
                    on_progress(ent_ref, msg)
            except DreamError as exc:
                failed += 1
                logger.warning("dream consolidate %s failed: %s", ent_ref, exc)
                if on_progress is not None:
                    on_progress(ent_ref, f"✗ {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("dream consolidate %s unexpected error", ent_ref)
                if on_progress is not None:
                    on_progress(ent_ref, f"✗ unexpected: {exc}")
        return consolidated, failed

    # ------------------------------------------------------------------
    # lock + throttle
    # ------------------------------------------------------------------

    @property
    def _lock_path(self) -> Path:
        return self.memory_root / _LOCK_FILENAME

    @property
    def _last_run_path(self) -> Path:
        return self.memory_root / _LAST_RUN_FILENAME

    def _is_throttled(self) -> bool:
        """True when ``now - last_run < min_seconds_between_runs``."""
        if self.min_seconds_between_runs <= 0:
            return False
        try:
            mtime = self._last_run_path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) < self.min_seconds_between_runs

    def _acquire_lock(self, trigger: str) -> bool:
        """Atomic O_CREAT|O_EXCL. Returns False if a fresh lock exists."""
        self.memory_root.mkdir(parents=True, exist_ok=True)
        # If a stale lock exists, remove it first. A stale lock is one
        # whose mtime is older than _STALE_LOCK_SECONDS — the previous
        # process either finished without releasing or crashed.
        try:
            stat = self._lock_path.stat()
        except OSError:
            stat = None
        if stat is not None and (time.time() - stat.st_mtime) > _STALE_LOCK_SECONDS:
            logger.warning("dream_runner: removing stale lock at %s", self._lock_path)
            try:
                self._lock_path.unlink()
            except OSError:
                pass

        try:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            return False
        try:
            payload = json.dumps({
                "pid": os.getpid(),
                "started_at": time.time(),
                "trigger": trigger,
            })
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _release_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            pass

    def _touch_last_run(self) -> None:
        try:
            self._last_run_path.touch()
        except OSError as exc:
            logger.warning("dream_runner: failed to touch last_run marker: %s", exc)

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------

    def _emit_start(self, trigger: str, entity_filter: Optional[str], entities_pending: int) -> None:
        emit_tool_event(
            "memory.dream.start",
            {
                "trigger": trigger,
                "entity_filter": entity_filter or "",
                "entities_pending": entities_pending,
            },
        )

    def _emit_end(
        self,
        trigger: str,
        entity_filter: Optional[str],
        consolidated: int,
        failed: int,
        duration_s: float,
    ) -> None:
        emit_tool_event(
            "memory.dream.end",
            {
                "trigger": trigger,
                "entity_filter": entity_filter or "",
                "entities_consolidated": consolidated,
                "entities_failed": failed,
                "duration_s": duration_s,
            },
        )

    def _emit_skipped(self, trigger: str, reason: str, entity_filter: Optional[str]) -> None:
        emit_tool_event(
            "memory.dream.skipped",
            {
                "trigger": trigger,
                "reason": reason,
                "entity_filter": entity_filter or "",
            },
        )
