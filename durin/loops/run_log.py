"""Loop run manifests: <workspace>/loops-runs/<loop>/<run_id>.json.

Each run file has a single owning writer (the runtime that fired it), so a
full-file atomic rewrite needs no lock — same model as workflow run logs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

SCHEMA = 1
ACTIVE_STATUSES = ("running", "needs_operator", "waiting_info")


def runs_root(workspace: str | Path) -> Path:
    return Path(workspace) / "loops-runs"


def _dir(ws, loop: str) -> Path:
    return runs_root(ws) / loop


def _path(ws, loop: str, run_id: str) -> Path:
    return _dir(ws, loop) / f"{run_id}.json"


def _write(ws, loop: str, run_id: str, record: dict) -> dict:
    d = _dir(ws, loop)
    d.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.time()
    atomic_write_text(_path(ws, loop, run_id), json.dumps(record, indent=2))
    return record


def start_run(ws, loop: str, run_id: str, *, source: str, task: str, origin: dict | None = None) -> dict:
    return _write(ws, loop, run_id, {
        "schema": SCHEMA, "run_id": run_id, "loop": loop, "status": "running",
        "source": source, "task": task[:8000], "origin": origin, "workflow_run_id": None,
        "ask": None, "detail": None, "checks": None, "goal_reached": None,
        "started_at": time.time(), "finished_at": None,
    })


def update_run(ws, loop: str, run_id: str, **fields) -> dict:
    record = read_run(ws, loop, run_id) or {"schema": SCHEMA, "run_id": run_id, "loop": loop}
    record.update(fields)
    return _write(ws, loop, run_id, record)


def finalize_run(ws, loop: str, run_id: str, *, status: str,
                 workflow_run_id: str | None = None, ask: str | None = None,
                 detail: str | None = None, checks: list | None = None,
                 goal_reached: bool | None = None) -> dict:
    record = read_run(ws, loop, run_id) or {"schema": SCHEMA, "run_id": run_id, "loop": loop}
    record.update({
        "status": status, "finished_at": time.time(),
        "workflow_run_id": workflow_run_id or record.get("workflow_run_id"),
        # None keeps the prior value; "" explicitly clears it — distinct from
        # "not provided" so a caller can clear a stale ask/detail on purpose.
        "ask": ask[:2000] if ask is not None else record.get("ask"),
        "detail": detail[:2000] if detail is not None else record.get("detail"),
        "checks": checks if checks is not None else record.get("checks"),
        "goal_reached": goal_reached if goal_reached is not None else record.get("goal_reached"),
    })
    return _write(ws, loop, run_id, record)


def read_run(ws, loop: str, run_id: str) -> dict | None:
    p = _path(ws, loop, run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_dir(d: Path) -> list[dict]:
    out = []
    for p in d.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda m: (m.get("started_at") or 0, m.get("run_id") or ""), reverse=True)
    return out


def list_runs(ws, loop: str, limit: int | None = 50) -> list[dict]:
    d = _dir(ws, loop)
    if not d.is_dir():
        return []
    runs = _load_dir(d)
    return runs if limit is None else runs[:limit]


def list_all_runs(ws, limit: int = 100) -> list[dict]:
    root = runs_root(ws)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in root.iterdir():
        if d.is_dir():
            out.extend(_load_dir(d))
    out.sort(key=lambda m: (m.get("started_at") or 0, m.get("run_id") or ""), reverse=True)
    return out[:limit]


def active_runs(ws, loop: str) -> list[dict]:
    return [m for m in list_runs(ws, loop, limit=None) if m.get("status") in ACTIVE_STATUSES]


def consecutive_no_goal(ws, loop: str) -> int:
    n = 0
    for m in list_runs(ws, loop, limit=None):
        status = m.get("status")
        if status in ACTIVE_STATUSES:
            continue
        if status in ("no_goal", "error"):
            n += 1
        else:
            break
    return n


def reconcile_running(ws, now: float | None = None, max_age_s: float = 6 * 3600) -> list[str]:
    """Boot sweep: flip any ``running`` manifest older than *max_age_s* to
    ``error`` (loops vocabulary; ``ask`` cleared) so a gateway restart mid-run
    does not leave a ``single``-concurrency loop permanently jammed — its next
    trigger sees a stale ``running`` manifest in ``active_runs`` and refuses
    to fire forever. Mirrors ``durin.workflow.run_log.reconcile_running``'s age
    semantics (``started_at`` compared against ``now - max_age_s``). A
    malformed manifest is skipped, never fatal. Returns the flipped run ids.
    """
    root = runs_root(ws)
    if not root.is_dir():
        return []
    if now is None:
        now = time.time()
    cutoff = now - max_age_s
    flipped: list[str] = []
    for loop_dir in root.iterdir():
        if not loop_dir.is_dir():
            continue
        for p in loop_dir.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if rec.get("status") == "running" and (rec.get("started_at") or 0.0) < cutoff:
                rec["status"] = "error"
                rec["ask"] = None
                try:
                    atomic_write_text(p, json.dumps(rec, indent=2))
                    flipped.append(rec.get("run_id"))
                except OSError:
                    continue
    return flipped


def prune_runs(ws, loop: str, keep: int) -> None:
    runs = list_runs(ws, loop, limit=None)
    keepers = set()
    kept = 0
    for m in runs:
        status = m.get("status")
        if status in ("needs_operator", "waiting_info") or kept < keep:
            keepers.add(m["run_id"])
            if status not in ("needs_operator", "waiting_info"):
                kept += 1
    for m in runs:
        if m["run_id"] not in keepers:
            _path(ws, loop, m["run_id"]).unlink(missing_ok=True)
