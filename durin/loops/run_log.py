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
ACTIVE_STATUSES = ("running", "needs_operator")


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


def start_run(ws, loop: str, run_id: str, *, source: str, task: str) -> dict:
    return _write(ws, loop, run_id, {
        "schema": SCHEMA, "run_id": run_id, "loop": loop, "status": "running",
        "source": source, "task": task[:8000], "workflow_run_id": None,
        "ask": None, "checks": None, "goal_reached": None,
        "started_at": time.time(), "finished_at": None,
    })


def update_run(ws, loop: str, run_id: str, **fields) -> dict:
    record = read_run(ws, loop, run_id) or {"schema": SCHEMA, "run_id": run_id, "loop": loop}
    record.update(fields)
    return _write(ws, loop, run_id, record)


def finalize_run(ws, loop: str, run_id: str, *, status: str,
                 workflow_run_id: str | None = None, ask: str | None = None,
                 checks: list | None = None, goal_reached: bool | None = None) -> dict:
    record = read_run(ws, loop, run_id) or {"schema": SCHEMA, "run_id": run_id, "loop": loop}
    record.update({
        "status": status, "finished_at": time.time(),
        "workflow_run_id": workflow_run_id or record.get("workflow_run_id"),
        "ask": (ask or "")[:2000] or record.get("ask"),
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


def list_runs(ws, loop: str, limit: int = 50) -> list[dict]:
    d = _dir(ws, loop)
    return _load_dir(d)[:limit] if d.is_dir() else []


def list_all_runs(ws, limit: int = 100) -> list[dict]:
    root = runs_root(ws)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in root.iterdir():
        if d.is_dir():
            out.extend(_load_dir(d))
    out.sort(key=lambda m: m.get("started_at") or 0, reverse=True)
    return out[:limit]


def active_runs(ws, loop: str) -> list[dict]:
    return [m for m in list_runs(ws, loop, limit=1000) if m.get("status") in ACTIVE_STATUSES]


def consecutive_no_goal(ws, loop: str) -> int:
    n = 0
    for m in list_runs(ws, loop, limit=1000):
        status = m.get("status")
        if status in ACTIVE_STATUSES:
            continue
        if status in ("no_goal", "error"):
            n += 1
        else:
            break
    return n


def prune_runs(ws, loop: str, keep: int) -> None:
    runs = list_runs(ws, loop, limit=100000)
    keepers = set()
    kept = 0
    for m in runs:
        if m.get("status") == "needs_operator" or kept < keep:
            keepers.add(m["run_id"])
            if m.get("status") != "needs_operator":
                kept += 1
    for m in runs:
        if m["run_id"] not in keepers:
            _path(ws, loop, m["run_id"]).unlink(missing_ok=True)
