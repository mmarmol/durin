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
            "budget": getattr(r, "budget", None),
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
    parent_run_id: str | None = None,
) -> Path:
    """Write the ``running`` manifest before the walk begins. Returns the record path.
    ``parent_run_id`` marks a nested subworkflow run with the run_id of its caller —
    ``None`` for a top-level run. When ``None`` and a prior manifest for this run_id
    exists (a resume rewrites the record), the prior value is preserved so the
    nested-run marker survives every rewrite."""
    if parent_run_id is None:
        prior = read_manifest(workspace, name, run_id) or {}
        parent_run_id = prior.get("parent_run_id")
    record = {
        "schema": SCHEMA,
        "run_id": run_id,
        "workflow": name,
        "status": "running",
        "root_session_key": root_session_key,
        "started_at": started_at,
        "ts": started_at,   # cursor field; finalize bumps it to finished_at
        "task": task,
        "parent_run_id": parent_run_id,
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
        "parent_run_id": base.get("parent_run_id"),
        "runs": _node_records(result),
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def finalize_run(
    workspace: str | Path, name: str, result, *,
    root_session_key: str | None, started_at: float, finished_at: float,
    task: str | None = None,
    parent_run_id: str | None = None,
) -> Path:
    """Terminal write: the run's final status, ``finished_at``, and full per-node trace.
    ``ts`` advances to ``finished_at`` so the dream cursor consumes the completed run."""
    # Preserve the task/parent_run_id from the running manifest when the caller does not
    # supply them (the engine's _finalize_manifest does not hold either; reading them here
    # keeps finalize_run safe without requiring the engine to carry the values separately).
    prior = read_manifest(workspace, name, result.run_id) or {}
    effective_task = task if task is not None else prior.get("task")
    effective_parent_run_id = parent_run_id if parent_run_id is not None else prior.get("parent_run_id")
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
        "parent_run_id": effective_parent_run_id,
        # The terminal output (the answer, the plan, or — on needs_input — the questions),
        # capped, so a historical audit of the run shows the result, not only the trace.
        "final_output": (result.final_output or "")[:8000],
        "final_output_node": getattr(result, "final_output_node", None),
        "needs_input_node": getattr(result, "needs_input_node", None),
        "output_files": list(getattr(result, "output_files", []) or []),
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


def list_runs(workspace: str | Path, name: str, limit: int = 20) -> list[dict]:
    """Newest-first manifest summaries for one workflow — the run-history listing.
    Full manifests stay one read away via read_manifest."""
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
        out.append({
            "run_id": rec.get("run_id"),
            "status": rec.get("status"),
            "started_at": rec.get("started_at"),
            "finished_at": rec.get("finished_at"),
            "task": (rec.get("task") or "")[:200],
            "needs_input_node": rec.get("needs_input_node"),
            "parent_run_id": rec.get("parent_run_id"),
        })
    out.sort(key=lambda r: r.get("started_at") or 0.0, reverse=True)
    return out[:max(1, int(limit))]


# A manifest with one of these statuses is done for good — eligible for pruning and
# counted against `keep`. "running" and "needs_input" are excluded on purpose: a running
# record is live, and a needs_input manifest is a resume point (deleting it would strand
# a workflow the caller can no longer resume). Malformed/unreadable files are skipped —
# never deleted — so a read glitch cannot destroy a record (fail open).
_TERMINAL_STATUSES = {"completed", "exhausted", "aborted", "cancelled", "crashed"}


def prune_manifests(workspace: str | Path, name: str, keep: int = 20) -> None:
    """Delete the oldest terminal run manifests for *name* beyond the *keep* most
    recent, keyed by ``ts``. Best-effort: any OSError is swallowed, so a failure here
    never breaks the caller (mirrors ``artifacts.prune_runs``).

    Pruning is deliberately independent of the dream-pass cursor: an unconsumed
    terminal record older than the retained window may be deleted before the dream
    pass reads it (a documented gap, not a bug) — coupling pruning to the cursor
    would let a disabled/stalled dream pass block pruning forever.
    """
    try:
        d = _wf_dir(workspace, name)
        if not d.is_dir():
            return
        terminal: list[tuple[float, Path]] = []
        for f in d.glob("*.json"):
            if f.name == ".cursor.json":
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue   # malformed/unreadable: skip, never delete
            if rec.get("status") not in _TERMINAL_STATUSES:
                continue   # running/needs_input: never delete, never counted
            terminal.append((rec.get("ts", 0.0), f))
        terminal.sort(key=lambda pair: pair[0], reverse=True)   # newest first
        for _ts, path in terminal[keep:]:
            path.unlink()
    except OSError:
        pass
