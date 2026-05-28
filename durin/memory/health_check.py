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
from collections import defaultdict
from pathlib import Path
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.memory.fts_index import fts_index_path
from durin.memory.indexer import (
    detect_index_staleness,
    reindex_one_file,
)

logger = logging.getLogger(__name__)

__all__ = ["HealthChecker"]


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
        """Run all probes once. Returns the status map + drift count."""
        components: dict[str, str] = {}
        errors: dict[str, str] = {}

        for name, probe in (
            ("fts", self._probe_fts),
            ("lance", self._probe_lance),
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
        payload: dict[str, Any] = {
            "status": status,
            "components": components,
            "drift_count": drift_count,
        }
        if errors:
            payload["errors"] = errors

        try:
            emit_tool_event("memory.health_check", payload)
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
        lance_dir = self._workspace / "memory" / ".index.lance"
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

    # ------------------------------------------------------------------
    # repair
    # ------------------------------------------------------------------

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
                    reindex_one_file(self._workspace, path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "health_check: drift repair %s failed: %s",
                        path, exc,
                    )
                return


def _emit_critical(component: str, error: str, count: int) -> None:
    """One-shot critical-status emit (re-armed on recovery)."""
    try:
        emit_tool_event(
            "memory.health.critical",
            {
                "component": component,
                "consecutive_failures": count,
                "last_error": error[:200],
            },
        )
    except Exception:  # pragma: no cover
        pass
