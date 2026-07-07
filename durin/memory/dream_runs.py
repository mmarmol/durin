"""Durable record of what each dream run did.

The "última corrida" card and run history used to be re-derived from the
telemetry stream, which is the wrong source: it is capped (the digest reads only
the newest N events, and a single refine pass emits dozens of per-pair events that
flood that window — pushing older run summaries, and eventually even the latest,
out of view), and it is retention-managed (compressed at 30 days, deleted at 90).
So a run's summary would silently disappear as ordinary activity accumulated.

This append-only JSONL under the workspace is the durable source of truth for the
run summaries. It is written once per dream run (best-effort) and survives deploys,
gateway restarts, and telemetry retention. The per-item activity feed still comes
from telemetry (recent detail); only the RUN summaries live here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

_RUNS_FILE = ".dream_runs.jsonl"
# Keep the file bounded; older runs age out of the file (git history still retains
# them if the workspace is committed). 200 daily runs ≈ half a year of history.
_MAX_RUNS = 200

__all__ = ["record_dream_run", "read_dream_runs"]


def _path(workspace: str | Path) -> Path:
    return Path(workspace) / "memory" / _RUNS_FILE


def record_dream_run(workspace: str | Path, summary: dict) -> None:
    """Append one timestamped run record to the durable store. Best-effort: a
    write failure must never break the dream cron."""
    try:
        p = _path(workspace)
        p.parent.mkdir(parents=True, exist_ok=True)
        at_ms = int(summary.get("at_ms")
                    or datetime.now(timezone.utc).timestamp() * 1000)
        rec = {**summary, "at_ms": at_ms}
        existing = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        existing.append(json.dumps(rec, ensure_ascii=False))
        atomic_write_text(p, "\n".join(existing[-_MAX_RUNS:]) + "\n")
    except Exception as exc:  # noqa: BLE001 — durability is best-effort
        logger.warning("record_dream_run failed: %s", exc)


def read_dream_runs(workspace: str | Path, limit: int = 50) -> list[dict]:
    """Return the most recent run records, newest first."""
    p = _path(workspace)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("at_ms", 0), reverse=True)
    return out[:limit]
