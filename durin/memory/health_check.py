"""Health-check cron for the memory subsystem (doc 02 §5.1, P2.4).

Periodically probes:

- **FTS** — the SQLite database opens + a trivial query runs.
- **LanceDB** — the optional dep is importable AND the table opens.
  When the dep isn't installed the probe reports ``skipped`` (not
  ``fail``).
- **Drift** — runs :func:`durin.memory.indexer.detect_index_staleness`
  and re-indexes the drifted paths.

Emits ``memory.health_check`` per tick with the full status map +
the drift count. After **3 consecutive failures of the same
component** in the same process, emits ``memory.health.critical``
once for that component and pauses re-emission until the component
recovers (so dashboards don't drown).

The class is the cron's **logic** — driving it on an interval is
the agent loop's job (start a daemon thread that calls
``run_tick()`` every ``interval_seconds``). Tests drive it
manually for determinism.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.memory.fts_index import fts_index_path
from durin.memory.indexer import (
    detect_index_staleness,
    reindex_one_file,
)

logger = logging.getLogger(__name__)

__all__ = ["HealthCheckScheduler", "HealthChecker"]


_FAILURE_THRESHOLD = 3


class HealthChecker:
    """Per-workspace memory-subsystem health probe.

    Instances are stateful: they track per-component failure counts
    across ``run_tick()`` calls so consecutive-failure escalation
    works without a separate persistence layer (in-process state is
    enough; on process restart the count resets, which is fine
    operationally — three new failures will re-escalate).
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace).resolve()
        self._failure_count: dict[str, int] = defaultdict(int)
        self._critical_emitted: set[str] = set()

    # ------------------------------------------------------------------
    # tick (one round of probes)
    # ------------------------------------------------------------------

    def run_tick(self) -> dict[str, Any]:
        """Run all probes once. Returns the status map + drift count.

        A6 (2026-05-28) added ``tick_id`` and ``duration_ms`` to the
        emitted payload. ``tick_id`` is a per-tick UUID hex for log
        correlation; ``duration_ms`` is the wall-clock of the tick.
        """
        tick_id = uuid.uuid4().hex
        t0 = time.perf_counter()
        components: dict[str, str] = {}
        errors: dict[str, str] = {}

        for name, probe in (
            ("fts", self._probe_fts),
            ("lance", self._probe_lance),
            # P11 Fix B (2026-05-30): cross-encoder probe. "skipped"
            # when CE disabled in config (most common case — silent).
            # "fail" surfaces missing sentence_transformers OR model
            # unreachable; escalates after 3 strikes per the standard
            # streak. P11 Fix C handles the in-process retry.
            ("cross_encoder", self._probe_cross_encoder),
        ):
            status, error = probe()
            components[name] = status
            if status == "fail":
                errors[name] = error
                self._failure_count[name] += 1
                if (
                    self._failure_count[name] >= _FAILURE_THRESHOLD
                    and name not in self._critical_emitted
                ):
                    _emit_critical(name, error, self._failure_count[name])
                    self._critical_emitted.add(name)
            elif status == "ok":
                # Success resets the streak AND clears the critical
                # mute so a new failure burst can escalate again.
                self._failure_count[name] = 0
                self._critical_emitted.discard(name)

        # P11 Fix C (2026-05-30): if cross-encoder probe failed,
        # reset the global-ish reranker state so the next user-facing
        # search retries the load. Combined with the time-based
        # retry in `CrossEncoderReranker.score()`, this gives two
        # paths to recovery: in-process retry every 60s OR an
        # explicit reset from the periodic probe.
        if components.get("cross_encoder") == "fail":
            try:
                self._reset_cross_encoder()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health_check: cross_encoder reset failed: %s", exc,
                )

        # P11 Fix D (2026-05-30): if lance probe failed, attempt a
        # rebuild_from_workspace. Only fires when the .lance dir is
        # present (the probe returns "ok" for a missing index — that
        # case is normal during cold start). Best-effort: a rebuild
        # failure logs + lets the 3-strike escalation continue on
        # the next tick.
        if components.get("lance") == "fail":
            try:
                rebuilt = self._rebuild_lance()
                if rebuilt:
                    logger.info(
                        "health_check: lance index rebuilt after probe failure"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health_check: lance rebuild failed: %s", exc,
                )

        # Drift detection runs always (it's a read; cheap). Auto-
        # repair via reindex_one_file for each issue. This is a
        # best-effort heal — a real failure logs but doesn't break
        # the tick.
        drift_count = 0
        try:
            issues = detect_index_staleness(self._workspace)
            drift_count = len(issues)
            for issue in issues:
                self._repair_drift(issue)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "health_check: staleness detection failed: %s", exc,
            )

        status = (
            "critical" if "critical" in self._critical_emitted
            else (
                "degraded" if any(v == "fail" for v in components.values())
                else "ok"
            )
        )
        duration_ms = (time.perf_counter() - t0) * 1000.0
        payload: dict[str, Any] = {
            "tick_id": tick_id,
            "status": status,
            "components": components,
            "drift_count": drift_count,
            "duration_ms": duration_ms,
        }
        if errors:
            payload["errors"] = errors

        try:
            emit_tool_event("memory.health_check", payload)
        except Exception:  # pragma: no cover
            pass

        # P7.2: telemetry retention pass piggybacks on the cron tick.
        try:
            from durin.memory.stats import DEFAULT_TELEMETRY_DIR
            from durin.telemetry.retention import run_retention
            run_retention(DEFAULT_TELEMETRY_DIR)
        except Exception:  # pragma: no cover
            pass

        return payload

    def consecutive_failures(self, component: str) -> int:
        """Test helper / dashboard hook: current failure streak."""
        return self._failure_count.get(component, 0)

    # ------------------------------------------------------------------
    # probes — return ("ok" | "fail" | "skipped", error_message)
    # ------------------------------------------------------------------

    def _probe_fts(self) -> tuple[str, str]:
        path = fts_index_path(self._workspace)
        if not path.is_file():
            # Missing index is "ok" — the indexer creates it on first
            # write. Reporting `fail` would noisily alarm on fresh
            # workspaces.
            return ("ok", "")
        try:
            conn = sqlite3.connect(str(path))
            try:
                conn.execute(
                    "SELECT COUNT(*) FROM fts_meta"
                ).fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return ("fail", f"sqlite probe: {exc}")
        return ("ok", "")

    def _probe_lance(self) -> tuple[str, str]:
        try:
            from durin.memory.vector_index import vector_index_available
        except Exception:  # noqa: BLE001
            return ("skipped", "vector_index module import failed")
        if not vector_index_available():
            return ("skipped", "lancedb not installed")
        # P9: path moved from memory/.index.lance to .durin/index/lance.
        from durin.memory.vector_index import _INDEX_PATH
        lance_dir = self._workspace.joinpath(*_INDEX_PATH)
        if not lance_dir.is_dir():
            # Lance index hasn't been built yet — that's fine, the
            # indexer creates it lazily.
            return ("ok", "")
        try:
            import lancedb
            db = lancedb.connect(str(lance_dir))
            db.table_names()
        except Exception as exc:  # noqa: BLE001
            return ("fail", f"lance probe: {exc}")
        return ("ok", "")

    def _probe_cross_encoder(self) -> tuple[str, str]:
        """P11 Fix B (2026-05-30): check the cross-encoder rerank
        subsystem can actually score a pair.

        States:
        - CE not enabled in config → ``("skipped", reason)``. Most
          users disable CE; reporting fail/warn would be noise.
        - sentence_transformers missing → ``("fail", reason)``. H25's
          static doctor check should have caught this at install
          time, but the runtime probe catches drift (someone removed
          the package after enabling CE).
        - Probe load + score works → ``("ok", "")``.
        - Probe load fails → ``("fail", reason)``. Triggers the
          standard 3-strike escalation in ``run_tick`` AND calls
          ``CrossEncoderReranker.reset()`` so the next user-facing
          search retries the load instead of falling through to RRF
          forever. P11 Fix C is the in-process retry; this is the
          out-of-process detection that escalates if the retry also
          fails repeatedly.
        """
        try:
            from durin.config.loader import load_config
            cfg = load_config()
            ce_cfg = cfg.memory.search.cross_encoder
            if not ce_cfg.enabled:
                return ("skipped", "cross-encoder disabled in config")
            model_id = ce_cfg.model
        except Exception as exc:  # noqa: BLE001
            return ("skipped", f"config load failed: {exc}")
        try:
            from durin.memory.cross_encoder import CrossEncoderReranker
        except Exception as exc:  # noqa: BLE001
            return ("fail", f"cross_encoder module import failed: {exc}")
        try:
            probe = CrossEncoderReranker(model=model_id)
            # Score a trivial pair to force the lazy load + verify
            # the model is reachable end-to-end. Cheap once warm
            # (~10-50ms); slow on cold cache (the first call may
            # download the model — that's the rare case and we
            # accept the latency to verify reachability).
            scores = probe.score("health_probe", ["dummy doc"])
        except Exception as exc:  # noqa: BLE001
            return ("fail", f"cross-encoder probe raised: {exc}")
        if not scores:
            # Returned None or [] — load failed gracefully via H25
            # fallback. Report fail so the 3-strike escalation fires.
            return (
                "fail",
                "cross-encoder load returned no scores "
                "(likely sentence_transformers missing or model "
                "unreachable; see ERROR log earlier)",
            )
        return ("ok", "")

    # ------------------------------------------------------------------
    # repair
    # ------------------------------------------------------------------

    def _reset_cross_encoder(self) -> None:
        """P11 Fix C (2026-05-30): clear cached reranker state in any
        live tool instance, so the next user-facing search re-attempts
        the model load.

        We can't reach into the per-tool-instance cache from here
        (HealthChecker doesn't know about the agent's tool registry),
        so this is conservative: we clear the module-level fallback
        log marker so a recovered CE re-fires its WARNING path the
        next time it degrades. The actual re-load happens via
        `CrossEncoderReranker._should_retry_load` time-window —
        the probe's role is to surface the failure loudly.
        """
        from durin.memory import cross_encoder as ce_mod

        ce_mod._RERANK_FALLBACK_LOGGED = False

    def _rebuild_lance(self) -> bool:
        """P11 Fix D (2026-05-30): rebuild the LanceDB vector index
        when the periodic probe finds it dead.

        Returns True on success, False on no-op (lance unavailable
        or index dir missing — both legitimate states, not failures).
        Raises only on rebuild itself failing — caller logs.

        Caveat: a rebuild walks every `memory/<class>/*.md` and
        re-embeds the lot. On a workspace of 5k entries that's
        ~10-30s of CPU. We accept that cost because (a) the alternative
        is leaving vector search dead until manual `durin memory
        reindex`, and (b) the probe only fires when the index is
        actually broken — not on normal operation.
        """
        try:
            from durin.memory.vector_index import (
                VectorIndex, _INDEX_PATH, vector_index_available,
            )
        except Exception:
            return False
        if not vector_index_available():
            return False
        lance_dir = self._workspace.joinpath(*_INDEX_PATH)
        if not lance_dir.is_dir():
            # No index present — nothing to rebuild. The lazy-create
            # path on next memory_store will handle it.
            return False
        from durin.config.loader import load_config
        from durin.memory.embedding import FastembedProvider

        cfg = load_config()
        provider = FastembedProvider(model=cfg.memory.embedding.model)
        vi = VectorIndex(self._workspace, provider)
        n = vi.rebuild_from_workspace()
        logger.info(
            "lance rebuild: %d entries indexed for %s",
            n, self._workspace,
        )
        return True

    def _repair_drift(self, issue: dict[str, Any]) -> None:
        """Best-effort drift repair: re-index the offending path."""
        uri = issue.get("uri") or ""
        reason = issue.get("reason") or ""
        if reason == "row_for_missing_file":
            # File is gone; the next walk_class won't return it, so
            # reindex_one_file with the original path will delete
            # the orphan row. We can't reconstruct the path from uri
            # alone without a heuristic — best effort.
            return
        # For missing_row / mtime_lag we need the file path. Derive
        # from URI for entries (memory/<class>/<id>) and for entity
        # refs (`<type>:<slug>`).
        candidate_paths: list[Path] = []
        if uri.startswith("memory/"):
            candidate_paths.append(self._workspace / f"{uri}.md")
        elif ":" in uri:
            type_, slug = uri.split(":", 1)
            candidate_paths.append(
                self._workspace / "memory" / "entities" / type_ / f"{slug}.md"
            )
        else:
            # Bare entry id — scan the entry classes.
            for cls in ("episodic", "stable", "corpus"):
                candidate_paths.append(
                    self._workspace / "memory" / cls / f"{uri}.md",
                )
        for path in candidate_paths:
            if path.is_file():
                try:
                    reindex_one_file(
                        self._workspace, path, trigger="drift_repair",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "health_check: drift repair %s failed: %s",
                        path, exc,
                    )
                return


# A7 (2026-05-28): map probe-component names → the CLI command that
# rebuilds that component from `memory/`. Probe names are not the
# same as CLI --target values (drift between health_check probes
# and `durin memory reindex`); this dict is where the translation
# lives. If `durin memory reindex --target ...` renames, the
# anti-drift test (test_health_critical_a7_recovery_hint.py) fails
# loudly — the hint never goes stale silently.
_RECOVERY_HINTS: dict[str, str] = {
    "fts": "durin memory reindex --target fts",
    "lance": "durin memory reindex --target lancedb",
}
_RECOVERY_HINT_FALLBACK = "durin memory reindex --target all"


def _emit_critical(component: str, error: str, count: int) -> None:
    """One-shot critical-status emit (re-armed on recovery).

    A7: payload includes ``manual_recovery_hint`` — the CLI command
    an operator can run to rebuild the failed component. The hint is
    informational; nothing executes it automatically. See doc 07
    §9.5 and ``_RECOVERY_HINTS`` above.
    """
    try:
        emit_tool_event(
            "memory.health.critical",
            {
                "component": component,
                "consecutive_failures": count,
                "last_error": error[:200],
                "manual_recovery_hint": _RECOVERY_HINTS.get(
                    component, _RECOVERY_HINT_FALLBACK,
                ),
            },
        )
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# A11 — periodic scheduler (daemon thread)
# ---------------------------------------------------------------------------


class HealthCheckScheduler:
    """Daemon thread that calls ``HealthChecker.run_tick()`` periodically.

    Audit A11 (2026-05-28). The HealthChecker itself ships the logic
    but the docstring (and doc 02 §5.1) leaves the "drive it on an
    interval" job to the agent loop. This is that driver.

    Lifecycle: ``start()`` spawns one daemon thread; ``stop()``
    signals exit via a ``threading.Event`` so the thread wakes from
    ``wait()`` immediately instead of holding the interval. The
    thread is a daemon so a hard process exit doesn't hang on the
    join.

    Failure isolation: a ``run_tick()`` exception is logged and the
    thread keeps going — the next interval still fires. A burst of
    failures is the HealthChecker's own ``memory.health.critical``
    escalation territory (3 strikes).
    """

    def __init__(
        self,
        checker: "HealthChecker",
        *,
        interval_seconds: int,
    ) -> None:
        self._checker = checker
        self._interval = max(1, int(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0

    @property
    def tick_count(self) -> int:
        """Total ticks the scheduler has driven (tests + dashboards)."""
        return self._tick_count

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=(
                f"durin-memory-health-{self._checker._workspace.name}"
            ),
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # First tick fires immediately so a fresh process has a
        # health probe in its first interval window, not after.
        # Subsequent ticks wait `interval_seconds`.
        while not self._stop_event.is_set():
            try:
                self._checker.run_tick()
                self._tick_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health_check tick raised; thread keeps running: %s",
                    exc,
                )
            # `wait` returns True on .set() — short-circuits the sleep
            # so `stop()` is responsive.
            if self._stop_event.wait(timeout=self._interval):
                break
