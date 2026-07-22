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

# Age fallback for manifests recorded WITHOUT an owner (pre-ownership
# releases): a run still "running" this long after it started can only be one
# whose process died before finalizing. Owned manifests don't use age at all —
# the sweep checks whether the owner process is alive (the 2026-07-18 ghost
# was 52 minutes old at the post-crash boot, far under any sane age bound).
RECONCILE_AGE_S = 6 * 3600

# Cap for the stored ``resume_upstream`` text on an aborted manifest: the exact
# input the failed node received, kept so a resume can replay it verbatim. Far
# above any sane edge text; a pathological upstream is truncated, and a resume
# of THAT run degrades gracefully (the node sees the capped text).
RESUME_UPSTREAM_MAX_CHARS = 16_000


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
            "exit_code": getattr(r, "exit_code", None),
            "duration_s": getattr(r, "duration_s", None),
            # Failure detail (stderr tail / exception text) for node_failed rows —
            # the evidence the improve pass's script-repair lane reads. Capped so a
            # pathological error cannot bloat every manifest rewrite.
            "error": (r.error or "")[:2000] or None,
            # Files this node added to the run's shared working folder — the folder
            # is shared, so attribution only exists if it is captured per node.
            "artifacts": list(getattr(r, "artifacts", []) or []),
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
    work_dir: str | None = None,
    typical_s: dict[str, float] | None = None,
    typical_total_s: float | None = None,
) -> Path:
    """Write the ``running`` manifest before the walk begins. Returns the record path.
    ``parent_run_id`` marks a nested subworkflow run with the run_id of its caller —
    ``None`` for a top-level run. When ``None`` and a prior manifest for this run_id
    exists (a resume rewrites the record), the prior value is preserved so the
    nested-run marker survives every rewrite. ``work_dir`` is the run's shared working
    folder, recorded from the start so an in-flight run's artifacts are findable."""
    if parent_run_id is None:
        prior = read_manifest(workspace, name, run_id) or {}
        parent_run_id = prior.get("parent_run_id")
    from durin.utils.process_tree import process_identity

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
        "work_dir": work_dir,
        # Which process is executing this run — the crash sweep flips any
        # "running" manifest whose owner is no longer alive.
        "owner": process_identity(),
        # Median per-node seconds from prior completed runs, computed once here so
        # every reader (panel, executions screen, tasks API) shows one number
        # instead of each recomputing it from the manifest history.
        "typical_s": typical_s or {},
        # Median TOTAL seconds of a prior completed run — a separate measurement,
        # never the sum of the per-node medians above, which would add up branches
        # no single run can all take.
        "typical_total_s": typical_total_s,
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
        "work_dir": base.get("work_dir"),
        "owner": base.get("owner"),
        "typical_s": base.get("typical_s") or {},
        "typical_total_s": base.get("typical_total_s"),
        # The node that was in flight has now finished; leaving the marker set
        # would pin a completed node as running for readers of the manifest.
        "active_node": None,
        "runs": _node_records(result),
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def mark_node_started(
    workspace: str | Path, name: str, run_id: str, *,
    node_id: str, label: str, started_at: float,
) -> None:
    """Record which node is in flight, so a reader that arrives mid-node knows.

    The manifest is otherwise only rewritten when a node *completes*, which
    leaves a multi-minute node invisible on disk for its whole duration: a
    reloaded page finds the run alive and its finished nodes listed, but nothing
    about the node actually running. Cleared by the next ``update_run``.

    No-op when no manifest exists — a nested run may not have written one, and
    fabricating a partial record here would confuse the crash sweep.
    """
    base = read_manifest(workspace, name, run_id)
    if base is None:
        return
    base["active_node"] = {"node_id": node_id, "label": label, "started_at": started_at}
    _record_path(workspace, name, run_id).write_text(json.dumps(base), encoding="utf-8")


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
        "work_dir": prior.get("work_dir"),
        "typical_s": prior.get("typical_s") or {},
        "typical_total_s": prior.get("typical_total_s"),
        # The terminal output (the answer, the plan, or — on needs_input — the questions),
        # capped, so a historical audit of the run shows the result, not only the trace.
        "final_output": (result.final_output or "")[:8000],
        "final_output_node": getattr(result, "final_output_node", None),
        "needs_input_node": getattr(result, "needs_input_node", None),
        # Failure-resume anchors: which node aborted the run and the EXACT upstream
        # text it received (verbatim — a retried script parses its stdin, so no
        # framing may pollute it). Only present on aborted runs that name a node.
        "failed_node": getattr(result, "failed_node", None),
        "resume_upstream": (
            (getattr(result, "resume_upstream", None) or "")[:RESUME_UPSTREAM_MAX_CHARS]
            if getattr(result, "resume_upstream", None) is not None else None
        ),
        "output_files": list(getattr(result, "output_files", []) or []),
        "missing_artifacts": list(getattr(result, "missing_artifacts", []) or []),
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
    """Mark orphaned ``running`` manifests as ``crashed`` (keeping the partial trace).

    A manifest that records an ``owner`` is flipped as soon as that process is
    dead — no age heuristic, so a gateway that crashes and restarts within
    minutes still clears its ghosts, while a run owned by another LIVE process
    (e.g. the TUI sharing this workspace) is never touched. Ownerless legacy
    manifests fall back to the ``started_at`` age cutoff. Safe to run at boot
    AND periodically. Returns how many records were reconciled; a malformed
    record is skipped, never fatal."""
    from durin.utils.process_tree import process_alive

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
            if rec.get("status") != "running":
                continue
            owner = rec.get("owner")
            if owner is not None:
                orphaned = not process_alive(owner)
            else:
                orphaned = rec.get("started_at", 0.0) < cutoff
            if orphaned:
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


def typical_node_durations(
    workspace: str | Path, name: str, *, limit: int = 5,
) -> dict[str, float]:
    """Median seconds each node took across this workflow's recent completed runs.

    Only completed runs count: an aborted run's node timings describe a path that
    ended early. The median (not the mean) so one pathological run does not skew
    the number a user reads as "normal". Nodes with no recorded duration — gates
    and routers — are absent rather than zero.
    """
    from statistics import median

    samples: dict[str, list[float]] = {}
    for rec in list_runs(workspace, name, limit=limit):
        if rec.get("status") != "completed":
            continue
        # list_runs returns trimmed summaries (by design, so listing endpoints stay
        # light); the per-node trace only lives in the full manifest.
        manifest = read_manifest(workspace, name, rec["run_id"]) or {}
        for r in manifest.get("runs") or []:
            d = r.get("duration_s")
            nid = r.get("node_id")
            if d is None or not nid:
                continue
            samples.setdefault(nid, []).append(float(d))
    return {nid: float(median(vals)) for nid, vals in samples.items() if vals}


def typical_total_duration(
    workspace: str | Path, name: str, *, limit: int = 5,
) -> float | None:
    """Median seconds a whole run of this workflow takes, across recent completed runs.

    Each run contributes the sum of its OWN node durations — the same quantity a
    surface sums to show a run's actual elapsed, so the two read as a direct
    comparison — and the median of those totals is the estimate.

    Summing the per-node medians instead would sum the union of the paths prior
    runs took: a workflow whose router picks one of several mutually exclusive
    branches has a median for every branch any prior run visited, while a single
    run walks exactly one of them. That over-counts badly (a graph with eight
    exclusive branches counts all eight) and under-counts loops (a node visited
    three times contributes one median). None when no completed run recorded any
    node duration — absent rather than a guessed zero.
    """
    from statistics import median

    totals: list[float] = []
    for rec in list_runs(workspace, name, limit=limit):
        if rec.get("status") != "completed":
            continue
        manifest = read_manifest(workspace, name, rec["run_id"]) or {}
        durations = [float(r["duration_s"]) for r in manifest.get("runs") or []
                     if r.get("duration_s") is not None]
        if durations:
            totals.append(sum(durations))
    return float(median(totals)) if totals else None


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


def list_all_runs(workspace: str | Path, limit: int = 50) -> list[dict]:
    """Newest-first run summaries across every workflow — the global feed the runs
    sidebar tab reads. Each entry is a ``list_runs``-style summary plus ``"workflow"``
    (which workflow it belongs to).

    ``needs_input`` entries are exempt from ``limit``: they are actionable resume
    points, and the tray must never lose one to the cap. Terminal entries are capped
    at ``limit`` after the needs_input entries are set aside, then the two groups are
    merged back into one newest-first list. A ``needs_input`` entry also carries
    ``"questions"`` — the manifest's ``final_output`` capped at 500 chars, the same
    convention as the tasks API's ``needs_input_detail`` — so the tray can show what
    the run is waiting on without a second fetch.
    """
    needs_input: list[dict] = []
    terminal: list[dict] = []
    for name in workflow_names_with_runs(workspace):
        for entry in list_runs(workspace, name, limit=10**9):
            entry = {**entry, "workflow": name}
            if entry.get("status") == "needs_input":
                manifest = read_manifest(workspace, name, entry["run_id"]) or {}
                entry["questions"] = (manifest.get("final_output") or "")[:500]
                needs_input.append(entry)
            else:
                terminal.append(entry)
    terminal.sort(key=lambda r: r.get("started_at") or 0.0, reverse=True)
    terminal = terminal[:max(1, int(limit))]
    out = needs_input + terminal
    out.sort(key=lambda r: r.get("started_at") or 0.0, reverse=True)
    return out


# A manifest with one of these statuses is done for good — eligible for pruning and
# counted against `keep`. "running" and "needs_input" are excluded on purpose: a running
# record is live, and a needs_input manifest is a resume point (deleting it would strand
# a workflow the caller can no longer resume). Malformed/unreadable files are skipped —
# never deleted — so a read glitch cannot destroy a record (fail open).
_TERMINAL_STATUSES = {"completed", "exhausted", "aborted", "cancelled", "crashed"}


def live_run_ids(workspace: str | Path) -> set[str]:
    """Run ids whose working folders must survive folder pruning: runs still
    executing, plus paused ``needs_input`` runs that can be resumed into the
    same folder. The same set of states ``prune_manifests`` refuses to delete —
    the folder pruner and the manifest pruner must agree on what "live" means,
    or a resumable run keeps its manifest but loses its files. A ``needs_input``
    record with no re-entry node cannot be resumed (the resume endpoints reject
    it), so it is not owed protection. Unreadable manifests are skipped."""
    out: set[str] = set()
    root = runs_root(workspace)
    if not root.is_dir():
        return out
    for f in root.glob("*/*.json"):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = rec.get("status")
        if status == "running" or (status == "needs_input" and rec.get("needs_input_node")):
            rid = rec.get("run_id")
            if rid:
                out.add(rid)
    return out


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
            status = rec.get("status")
            if status == "running":
                continue   # live record: never delete, never counted
            if status == "needs_input":
                if rec.get("needs_input_node"):
                    continue   # a resumable pause point: never delete, never counted
                # A needs_input with no re-entry node predates the resume feature;
                # the resume endpoints reject it, so protecting it would only
                # accumulate unactionable ghosts — retain it like any terminal.
            elif status not in _TERMINAL_STATUSES:
                continue   # unknown/foreign status: fail open, never delete
            terminal.append((rec.get("ts", 0.0), f))
        terminal.sort(key=lambda pair: pair[0], reverse=True)   # newest first
        for _ts, path in terminal[keep:]:
            path.unlink()
    except OSError:
        pass


def reconcile_one(workspace: str | Path, name: str, run_id: str) -> bool:
    """Flip ONE ``running`` manifest to ``crashed`` iff its owner process is
    dead. The self-heal the `tasks` tool applies when the user pokes a run
    the sweep hasn't reached yet — so "status"/"stop" answer with the truth
    instead of describing a process that no longer exists. Ownerless legacy
    manifests are left to the age sweep. Returns True when flipped."""
    from durin.utils.process_tree import process_alive

    rec = read_manifest(workspace, name, run_id)
    if not rec or rec.get("status") != "running":
        return False
    owner = rec.get("owner")
    if owner is None or process_alive(owner):
        return False
    rec["status"] = "crashed"
    try:
        _record_path(workspace, name, run_id).write_text(
            json.dumps(rec), encoding="utf-8")
    except OSError:
        return False
    return True
