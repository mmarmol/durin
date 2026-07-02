"""Per-run records (a live manifest) for workflow auditability + self-improvement.

``WorkflowResult`` is discarded after a run, but its per-node trace (iteration counts,
decision pass/fail, the persisted session of each node/worker, final status) is exactly
the diagnostic signal dream needs AND the forward-reference an auditor needs. So each run
owns a single durable record. Records live BESIDE the workflow definitions
(``<workspace>/workflows-runs/<name>/<run_id>.json``), never inside ``workflows/`` —
the version store snapshots that directory wholesale, and run records are not versioned
definition state.

The record is a *live manifest*: ``start_run`` writes it ``running`` before the walk,
``update_run`` rewrites it after each node completes (so an in-flight run is observable),
and ``finalize_run`` writes the terminal status. Each file is unique (``<run_id>.json``)
and single-writer (the one run that owns the id), so a full-file rewrite per update is
safe with no RMW lock. A per-workflow cursor marks how far the dream pass has consumed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def runs_root(workspace: str | Path) -> Path:
    return Path(workspace) / "workflows-runs"


def _wf_dir(workspace: str | Path, name: str) -> Path:
    return runs_root(workspace) / name


# Manifest schema version. v1 records (write_run, no schema field) carry only
# {run_id, workflow, status, ts, runs:[{node_id, iteration, passed}]}; readers tolerate them.
SCHEMA = 2

# A run still "running" this long after it started can only be one whose process died
# before finalizing — the gateway's boot-time sweep (reconcile_running) flips it to
# "crashed" so an auditor sees a truthful status. Generous: real runs finalize fast.
RECONCILE_AGE_S = 6 * 3600


def _node_records(result) -> list[dict]:
    """The per-node trace each manifest write embeds: every field an auditor or the
    dream pass reads off a run (session key, fan-out/branch identity, status, route)."""
    return [
        {
            "node_id": r.node_id,
            "iteration": r.iteration,
            "passed": r.passed,
            "session_key": r.session_key,
            "worker_index": r.worker_index,
            "branch_id": r.branch_id,
            "status": r.status,
            "route_label": r.route_label,
        }
        for r in result.runs
    ]


def _record_path(workspace: str | Path, name: str, run_id: str) -> Path:
    d = _wf_dir(workspace, name)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.json"


def start_run(
    workspace: str | Path, name: str, run_id: str, *,
    root_session_key: str | None, started_at: float,
    task: str | None = None,
) -> Path:
    """Write the ``running`` manifest before the walk begins. Returns the record path."""
    record = {
        "schema": SCHEMA,
        "run_id": run_id,
        "workflow": name,
        "status": "running",
        "root_session_key": root_session_key,
        "started_at": started_at,
        "ts": started_at,   # cursor field; finalize bumps it to finished_at
        "task": task,
        "runs": [],
    }
    path = _record_path(workspace, name, run_id)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def update_run(
    workspace: str | Path, name: str, run_id: str, result, *, status: str = "running",
) -> None:
    """Rewrite the manifest with the run's per-node trace so far, preserving the
    ``root_session_key``/``started_at`` from ``start_run``. Single-writer, full rewrite."""
    path = _record_path(workspace, name, run_id)
    base = read_manifest(workspace, name, run_id) or {}
    record = {
        "schema": SCHEMA,
        "run_id": run_id,
        "workflow": name,
        "status": status,
        "root_session_key": base.get("root_session_key"),
        "started_at": base.get("started_at"),
        "ts": base.get("ts", base.get("started_at")),
        "task": base.get("task"),
        "runs": _node_records(result),
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def finalize_run(
    workspace: str | Path, name: str, result, *,
    root_session_key: str | None, started_at: float, finished_at: float,
    task: str | None = None,
) -> Path:
    """Terminal write: the run's final status, ``finished_at``, and full per-node trace.
    ``ts`` advances to ``finished_at`` so the dream cursor consumes the completed run."""
    # Preserve the task from the running manifest when the caller does not supply one
    # (the engine's _finalize_manifest does not hold the task; reading it here keeps
    # finalize_run safe without requiring the engine to carry the value separately).
    effective_task = task
    if effective_task is None:
        prior = read_manifest(workspace, name, result.run_id) or {}
        effective_task = prior.get("task")
    record = {
        "schema": SCHEMA,
        "run_id": result.run_id,
        "workflow": name,
        "status": result.status,
        "root_session_key": root_session_key,
        "started_at": started_at,
        "finished_at": finished_at,
        "ts": finished_at,
        "task": effective_task,
        # The terminal output (the answer, the plan, or — on needs_input — the questions),
        # capped, so a historical audit of the run shows the result, not only the trace.
        "final_output": (result.final_output or "")[:8000],
        "needs_input_node": getattr(result, "needs_input_node", None),
        "runs": _node_records(result),
    }
    path = _record_path(workspace, name, result.run_id)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def write_run(workspace: str | Path, name: str, result, *, ts: float | None = None) -> Path:
    """Persist a run's terminal trace in one shot. Thin wrapper over ``finalize_run`` for
    callers that don't write a live manifest (the dream-pass tests; standalone runs)."""
    now = ts if ts is not None else time.time()
    return finalize_run(
        workspace, name, result,
        root_session_key=None, started_at=now, finished_at=now,
    )


def read_manifest(workspace: str | Path, name: str, run_id: str) -> dict | None:
    """The current manifest for one run, or None if it has none / is unreadable."""
    path = _wf_dir(workspace, name) / f"{run_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def runs_for_session(workspace: str | Path, root_session_key: str) -> list[dict]:
    """Every run manifest whose ``root_session_key`` matches, across all workflows,
    newest-first (by ``ts``). The forward reference from a session to the runs it spawned."""
    root = runs_root(workspace)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for wf_dir in root.iterdir():
        if not wf_dir.is_dir():
            continue
        for f in wf_dir.glob("*.json"):
            if f.name == ".cursor.json":
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if rec.get("root_session_key") == root_session_key:
                out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0.0), reverse=True)
    return out


def reconcile_running(workspace: str | Path, *, now: float, max_age_s: float) -> int:
    """Mark any ``running`` manifest whose ``started_at`` is older than *max_age_s* as
    ``crashed`` (keeping its partial trace). A still-``running`` record long past its
    start could only be a run whose process died before finalizing — this recovers it so
    crash detection is observable. Returns how many records were reconciled. A malformed
    record is skipped, never fatal."""
    root = runs_root(workspace)
    if not root.is_dir():
        return 0
    cutoff = now - max_age_s
    count = 0
    for wf_dir in root.iterdir():
        if not wf_dir.is_dir():
            continue
        for f in wf_dir.glob("*.json"):
            if f.name == ".cursor.json":
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if rec.get("status") == "running" and rec.get("started_at", 0.0) < cutoff:
                rec["status"] = "crashed"
                try:
                    f.write_text(json.dumps(rec), encoding="utf-8")
                    count += 1
                except OSError:
                    continue
    return count


def read_runs_since(workspace: str | Path, name: str, cursor_ts: float = 0.0) -> list[dict]:
    """All run records for *name* newer than *cursor_ts*, oldest-first. Records may be
    live manifests: a caller that needs a terminal run must skip records whose
    ``status`` is ``"running"`` or ``"crashed"``."""
    d = _wf_dir(workspace, name)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in d.glob("*.json"):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("ts", 0.0) > cursor_ts:
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0.0))
    return out


def read_cursor(workspace: str | Path, name: str) -> float:
    f = _wf_dir(workspace, name) / ".cursor.json"
    try:
        return float(json.loads(f.read_text(encoding="utf-8")).get("ts", 0.0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def advance_cursor(workspace: str | Path, name: str, ts: float) -> None:
    d = _wf_dir(workspace, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".cursor.json").write_text(json.dumps({"ts": ts}), encoding="utf-8")


def workflow_names_with_runs(workspace: str | Path) -> list[str]:
    """Names of workflows that have at least one run record."""
    root = runs_root(workspace)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
