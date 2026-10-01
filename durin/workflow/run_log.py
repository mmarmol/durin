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
safe with no RMW lock. It is read while it is rewritten, though (the runs panel, the
crash sweep, the folder pruner, the run's own next rewrite), so every write replaces
the file whole: a reader sees the previous record or the new one, never a torn file it
would take for "no record". A per-workflow cursor marks how far the dream pass has
consumed.

One state has more than one writer: a ``needs_input`` pause can be answered (resumed),
rejected (an approval pause), or cancelled, each from its own caller, each a
read-then-write on the SAME manifest. ``run_lock_target`` serializes those so exactly
one of a racing pair wins — see its docstring.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text


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

# Per-source cap for ``resume_inputs``: the last outputs of nodes referenced by
# some node's ``inputs_from``, stored on a paused/aborted manifest so a resumed
# walk (whose in-memory trace starts empty) can still compose those inputs.
RESUME_INPUT_MAX_CHARS = 8_000


def _resume_inputs(workflow, result) -> dict[str, str] | None:
    """{source: capped last output} for the union of every node's inputs_from —
    only for runs that can be resumed (needs_input, or aborted with a failed
    node), and only for sources that actually recorded an output."""
    if workflow is None:
        return None
    resumable = (
        (result.status == "needs_input" and getattr(result, "needs_input_node", None))
        or (result.status == "aborted" and getattr(result, "failed_node", None))
    )
    if not resumable:
        return None
    sources: set[str] = set()
    for node in workflow.nodes.values():
        sources.update(getattr(node, "inputs_from", ()) or ())
    if not sources:
        return None
    out: dict[str, str] = {}
    for r in result.runs:
        if r.node_id in sources and r.output:
            out[r.node_id] = r.output[:RESUME_INPUT_MAX_CHARS]
    return out or None


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
            # Script nodes only: what ran and what it printed. Already capped and
            # redacted by the runner — this is a durable readable file, so it must
            # never be the place a credential first appears.
            "command": getattr(r, "command", None),
            "stdout": getattr(r, "stdout", None),
            "stderr": getattr(r, "stderr", None),
            # Producer identity: resolved model/provider (agent nodes only) and the
            # node-definition hash (WorkNode with a raw spec only) — absent (None) on
            # runs the engine could not resolve, e.g. script nodes or a pre-upgrade trace.
            "model": getattr(r, "model", None),
            "provider": getattr(r, "provider", None),
            "node_hash": getattr(r, "node_hash", None),
            # Set only when status=="reused": the run_id of the ORIGINAL pass that
            # produced the artifact this pass reused instead of dispatching the
            # runner (None otherwise — see durin/workflow/engine.py's reuse gate).
            "origin_run_id": getattr(r, "origin_run_id", None),
        }
        for r in result.runs
    ]


def _record_path(workspace: str | Path, name: str, run_id: str) -> Path:
    d = _wf_dir(workspace, name)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.json"


def _write_json(path: Path, data: dict) -> None:
    """Replace ``path`` with ``data`` as JSON in one step: written to a temporary
    file beside it, then renamed over it. A plain rewrite truncates the file
    first, so a reader arriving mid-write would find a torn file, which every
    reader here takes for "no record". The temporary name never matches the
    ``*.json`` globs the listings walk."""
    atomic_write_text(path, json.dumps(data))


def start_run(
    workspace: str | Path, name: str, run_id: str, *,
    root_session_key: str | None, started_at: float,
    task: str | None = None,
    parent_run_id: str | None = None,
    work_dir: str | None = None,
    work_key: str | None = None,
    typical_s: dict[str, float] | None = None,
    typical_total_s: float | None = None,
    spec_hash: str | None = None,
    durin_version: str | None = None,
    resumed: bool = False,
) -> Path:
    """Write the ``running`` manifest before the walk begins. Returns the record path.
    ``parent_run_id`` marks a nested subworkflow run with the run_id of its caller —
    ``None`` for a top-level run. When ``None`` and a prior manifest for this run_id
    exists (a resume rewrites the record), the prior value is preserved so the
    nested-run marker survives every rewrite. ``work_dir`` is the run's shared working
    folder, recorded from the start so an in-flight run's artifacts are findable.
    ``work_key`` is the caller-supplied key (see ``WorkflowEngine.run``) that made
    ``work_dir`` a STABLE folder instead of a fresh per-run one — ``None`` when the
    run used the default per-run folder. ``spec_hash``/``durin_version`` identify the
    workflow definition and the engine build that walked it (see
    ``durin/workflow/provenance.py``); both ``None`` when the caller does not supply
    them (e.g. a caller with no workflow object). ``resumed`` marks a walk that
    re-enters an existing run (a failure-resume or a paused run's reply): its
    ``runs`` start empty here, so they never hold the earlier attempts' rows; the
    node ids those attempts walked are kept in ``earlier_nodes`` instead."""
    prior = read_manifest(workspace, name, run_id) or {}
    if parent_run_id is None:
        parent_run_id = prior.get("parent_run_id")
    earlier_nodes = sorted(
        set(prior.get("earlier_nodes") or [])
        | {r["node_id"] for r in prior.get("runs") or [] if r.get("node_id")}
    ) if resumed else []
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
        "work_key": work_key,
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
        # Producer identity for the whole run — see update_run/finalize_run, which
        # carry these two forward unchanged on every later rewrite.
        "spec_hash": spec_hash,
        "durin_version": durin_version,
        # Sticky once set: every later rewrite carries it, and every resume of the
        # run sets it again, so a reader knows ``runs`` covers only the last walk.
        "resumed": resumed,
        # Every node the earlier attempts of a resumed run walked, gathered across
        # all its resumes and carried forward on every rewrite, so the run's route
        # is on record even though ``runs`` holds only the last walk.
        "earlier_nodes": earlier_nodes,
        "runs": [],
    }
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
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
        "work_key": base.get("work_key"),
        "owner": base.get("owner"),
        "typical_s": base.get("typical_s") or {},
        "typical_total_s": base.get("typical_total_s"),
        "spec_hash": base.get("spec_hash"),
        "durin_version": base.get("durin_version"),
        "resumed": base.get("resumed", False),
        "earlier_nodes": base.get("earlier_nodes") or [],
        # The node that was in flight has now finished; leaving the marker set
        # would pin a completed node as running for readers of the manifest.
        "active_node": None,
        "runs": _node_records(result),
    }
    _write_json(path, record)


def mark_node_started(
    workspace: str | Path, name: str, run_id: str, *,
    node_id: str, label: str, started_at: float,
    iteration: int | None = None, session_key: str | None = None,
) -> None:
    """Record which node is in flight, so a reader that arrives mid-node knows.

    The manifest is otherwise only rewritten when a node *completes*, which
    leaves a multi-minute node invisible on disk for its whole duration: a
    reloaded page finds the run alive and its finished nodes listed, but nothing
    about the node actually running. Cleared by the next ``update_run``.

    ``iteration`` and ``session_key`` are what a reader needs to OPEN the node
    rather than merely name it. The key cannot be reconstructed from the node id
    (a persistent-session node omits the iteration suffix), so it is advertised
    rather than left to be guessed. It is ``None`` for every node kind that
    persists no conversation — script, sub-workflow, parallel — where a key
    would point at a session that does not exist.

    No-op when no manifest exists — a nested run may not have written one, and
    fabricating a partial record here would confuse the crash sweep.
    """
    base = read_manifest(workspace, name, run_id)
    if base is None:
        return
    base["active_node"] = {
        "node_id": node_id, "label": label, "started_at": started_at,
        "iteration": iteration, "session_key": session_key,
    }
    _write_json(_record_path(workspace, name, run_id), base)


def finalize_run(
    workspace: str | Path, name: str, result, *,
    root_session_key: str | None, started_at: float, finished_at: float,
    task: str | None = None,
    parent_run_id: str | None = None,
    workflow=None,
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
        "work_key": prior.get("work_key"),
        "typical_s": prior.get("typical_s") or {},
        "typical_total_s": prior.get("typical_total_s"),
        "spec_hash": prior.get("spec_hash"),
        "durin_version": prior.get("durin_version"),
        "resumed": prior.get("resumed", False),
        "earlier_nodes": prior.get("earlier_nodes") or [],
        # The terminal output (the answer, the plan, or — on needs_input — the questions),
        # capped, so a historical audit of the run shows the result, not only the trace.
        "final_output": (result.final_output or "")[:8000],
        "final_output_node": getattr(result, "final_output_node", None),
        "final_route_label": getattr(result, "final_route_label", None),
        "needs_input_node": getattr(result, "needs_input_node", None),
        # "approval" (a WorkNode.approval pause) or "question" (a __needs_input__
        # pause) when status=="needs_input", None otherwise — see
        # durin/workflow/approval.py for how a paused approval's reply is resolved.
        "ask_kind": getattr(result, "ask_kind", None),
        # True only for a run the approver explicitly rejected (status "cancelled")
        # — distinguishes that from any other reason a run ends cancelled.
        "rejected": getattr(result, "rejected", False),
        # Failure-resume anchors: which node aborted the run and the EXACT upstream
        # text it received (verbatim — a retried script parses its stdin, so no
        # framing may pollute it). Only present on aborted runs that name a node.
        "failed_node": getattr(result, "failed_node", None),
        # {source: capped output} for inputs_from sources — resume composition seeds.
        "resume_inputs": _resume_inputs(workflow, result),
        "resume_upstream": (
            (getattr(result, "resume_upstream", None) or "")[:RESUME_UPSTREAM_MAX_CHARS]
            if getattr(result, "resume_upstream", None) is not None else None
        ),
        "output_files": list(getattr(result, "output_files", []) or []),
        "missing_artifacts": list(getattr(result, "missing_artifacts", []) or []),
        "runs": _node_records(result),
    }
    path = _record_path(workspace, name, result.run_id)
    _write_json(path, record)
    return path


def finalize_short_circuit(
    workspace: str | Path, name: str, run_id: str, *,
    status: str, final_output: str | None, rejected: bool = False,
    cancelled_by: dict | None = None,
) -> dict:
    """Rewrite an existing manifest to a terminal status IN PLACE, preserving every
    field it already has — ``runs``, ``work_dir``, ``work_key``, ``task``,
    ``typical_s``/``typical_total_s``, ``spec_hash``, ``durin_version``,
    ``root_session_key``, ``started_at``, etc.

    For a resume reply that ends the run WITHOUT invoking the engine (an approval
    reject, or an approve on a terminal approval node with no ``next``): unlike
    ``finalize_run``, there is no fresh ``WorkflowResult`` carrying a real per-node
    trace here, so rewriting the record from one (with an empty ``runs``) would
    silently discard the trace the paused manifest already recorded. Reading the
    prior record and overriding only the terminal fields keeps that trace intact.

    Sets ``status``, ``final_output`` (capped exactly like ``finalize_run`` caps
    it), ``finished_at``/``ts``, ``rejected``, clears ``active_node`` (nothing is
    in flight any more), and drops the pause markers ``needs_input_node`` and
    ``ask_kind`` to ``None`` — the run is no longer answerable. ``cancelled_by``
    (``durin.service.approvals.decider_of``'s shape) is recorded together with
    ``cancelled_at`` only when a caller passes it — a person explicitly cancelling
    a paused run through ``WorkflowsService.cancel_run``, never an approval
    reject/approve, which leave both fields unset. Returns the rewritten dict."""
    prior = read_manifest(workspace, name, run_id) or {}
    now = time.time()
    record = dict(prior)
    record["status"] = status
    record["final_output"] = (final_output or "")[:8000]
    record["finished_at"] = now
    record["ts"] = now
    record["rejected"] = rejected
    record["active_node"] = None
    record["needs_input_node"] = None
    record["ask_kind"] = None
    if cancelled_by is not None:
        record["cancelled_by"] = cancelled_by
        record["cancelled_at"] = now
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
    return record


# Lock target name, kept BESIDE the run's manifest file (not overwriting it) so it
# never matches the `*.json` glob list_runs/list_all_runs/reconcile_running walk —
# mirrors workflow/version_store.py's VERSION_LOCK_NAME/version_lock_target pattern.
_RUN_CLAIM_LOCK_SUFFIX = ".claim"


def run_lock_target(workspace: str | Path, name: str, run_id: str) -> Path:
    """The cross-process lock target serializing every caller that checks a
    ``needs_input`` run's status and then acts on it: ``WorkflowsService.cancel_run``
    finalizing it, ``WorkflowsService.execute``'s resume path claiming it (moving it
    off ``needs_input`` before actually resuming), and the approval-reject
    short-circuit inside that same resume path. Each of those is a read-then-write
    on the SAME manifest; without a lock, two racing callers can both read
    ``needs_input`` before either writes, so — for example — a resume goes on to run
    real nodes on a run ``cancel_run`` just finalized. Whichever caller acquires the
    lock first wins outright: the loser's own re-read, taken only once it acquires
    the lock in turn, sees the winner's already-written status and refuses instead
    of acting a second time. Every OTHER manifest write (a running walk's own
    per-node updates, a terminal ``finalize_run``) stays single-writer with no lock,
    same as this module's own docstring says — this guards only the ``needs_input``
    check-then-act window, where an outside caller (not the walk itself) can act on
    the manifest."""
    return _wf_dir(workspace, name) / f"{run_id}{_RUN_CLAIM_LOCK_SUFFIX}"


def claim_for_resume(workspace: str | Path, name: str, run_id: str) -> dict:
    """Move a ``needs_input`` manifest to ``running`` IN PLACE, owned by this
    process, preserving every other field — called under ``run_lock_target``'s
    lock, right before the caller releases it and actually resumes the run, so a
    ``cancel_run`` (or an approval reject) racing in right behind sees this run is
    no longer ``needs_input`` and refuses instead of finalizing a run that is, by
    then, genuinely resuming. ``WorkflowEngine.run``'s own ``_start_manifest``
    fully rewrites the manifest once the resume gets a workflow-run thread (a fresh
    ``started_at``, the resumed walk's own ``runs``) — this claim only needs to
    survive until that first real write, not to be a lasting record itself. That
    can take a while when every run thread is busy, and a paused run is usually
    answered after a restart, so its recorded owner is often a dead process: the
    claim re-stamps the owner, or the crash sweep would flip the waiting resume to
    ``crashed``. A resume whose engine never makes that write undoes the claim
    with ``release_resume_claim``. Returns the claimed record."""
    from durin.utils.process_tree import process_identity

    prior = read_manifest(workspace, name, run_id) or {}
    record = dict(prior)
    record["status"] = "running"
    record["owner"] = process_identity()
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
    return record


def release_resume_claim(
    workspace: str | Path, name: str, run_id: str, *, prior: dict, claimed: dict,
) -> bool:
    """Undo ``claim_for_resume`` for a resume whose engine never wrote the run's
    own manifest: put back ``prior``, the paused record exactly as it was before
    the claim (``needs_input``, its original owner), so the run can be answered
    or cancelled again. A claimed manifest owned by this live process is one
    that neither the crash sweep nor the ``tasks`` self-heal ever ends, and one
    ``cancel_run`` and a new resume both refuse. Does nothing — returns False —
    once the manifest differs from ``claimed``: the engine took the run over.
    Called under ``run_lock_target``'s lock, like the claim."""
    if read_manifest(workspace, name, run_id) != claimed:
        return False
    _write_json(_record_path(workspace, name, run_id), prior)
    return True


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
                    _write_json(f, rec)
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
    _write_json(d / ".cursor.json", {"ts": ts})


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
    and routers — are absent rather than zero. A reused row (status "reused",
    duration_s 0) is excluded too: it did not run, so its duration says nothing
    about the node's real cost.
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
            # A reused row's 0.0 does not describe how long the node takes when it
            # actually runs — counting it would deflate the median toward zero the
            # more often the node gets skipped.
            if d is None or not nid or r.get("status") == "reused":
                continue
            samples.setdefault(nid, []).append(float(d))
    return {nid: float(median(vals)) for nid, vals in samples.items() if vals}


def typical_total_duration(
    workspace: str | Path, name: str, *, limit: int = 5,
) -> float | None:
    """Median seconds a whole run of this workflow takes, across recent completed runs.

    Each run contributes the sum of its OWN node durations — the same quantity a
    surface sums to show a run's actual elapsed, so the two read as a direct
    comparison — and the median of those totals is the estimate. Reused rows
    (status "reused") are excluded from that sum, same as in
    typical_node_durations: their duration is not what a fresh dispatch costs.

    Summing the per-node medians instead would sum the union of the paths prior
    runs took: a workflow whose router picks one of several mutually exclusive
    branches has a median for every branch any prior run visited, while a single
    run walks exactly one of them. That over-counts badly (a graph with eight
    exclusive branches counts all eight) and under-counts loops (a node visited
    three times contributes one median). None when no completed run recorded any
    node duration — absent rather than a guessed zero.

    The median only compares like with like when those runs took the same route:
    a router whose runs skip in 0 s, answer in minutes or investigate for half an
    hour has a median that describes none of them. A run's route is the set of
    nodes it walked — so extra passes through a revision loop stay on the same
    route — and when the runs measured here walked different sets, there is no
    single typical total and the result is None. The estimate is made before the
    new run has taken any route, so it cannot pick the matching one instead.

    A resumed run's seconds are left out: a resume starts its rows over, so they
    hold only the last walk's. Its route still counts, taken from those rows plus
    the nodes its earlier attempts walked (``earlier_nodes``). Leaving the run out
    altogether would hide a route that only resumed runs take, such as a branch
    through an approval, and show runs on it another route's median.
    """
    from statistics import median

    totals: list[float] = []
    routes: set[frozenset[str]] = set()
    for rec in list_runs(workspace, name, limit=limit):
        if rec.get("status") != "completed":
            continue
        manifest = read_manifest(workspace, name, rec["run_id"]) or {}
        rows = manifest.get("runs") or []
        route = frozenset(r["node_id"] for r in rows if r.get("node_id")) | frozenset(
            manifest.get("earlier_nodes") or [])
        if manifest.get("resumed"):
            routes.add(route)
            continue
        # Same exclusion as typical_node_durations: a reused row's duration is not
        # what a fresh dispatch would have cost, so it must not count toward the
        # run's total either.
        durations = [float(r["duration_s"]) for r in rows
                     if r.get("duration_s") is not None and r.get("status") != "reused"]
        if durations:
            totals.append(sum(durations))
            routes.add(route)
    if not totals or len(routes) > 1:
        return None
    return float(median(totals))


def list_runs(workspace: str | Path, name: str, limit: int = 20) -> list[dict]:
    """Newest-first manifest summaries for one workflow — the run-history listing.
    Full manifests stay one read away via read_manifest.

    ``origin`` is the manifest's ``root_session_key`` under the name the webui's
    tray/inbox filters on (e.g. distinguishing an `automation:<name>` origin from
    an interactive session). ``ask_kind``, ``final_route_label``, and ``rejected``
    ride along too, so a caller building a feed row no longer needs the full
    manifest just to tell an approval pause from a question pause, read which
    verdict ended a run, or know a cancelled run was an explicit rejection."""
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
            "origin": rec.get("root_session_key"),
            "ask_kind": rec.get("ask_kind"),
            "final_route_label": rec.get("final_route_label"),
            "rejected": rec.get("rejected", False),
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


def live_work_keys(workspace: str | Path) -> set[tuple[str, str]]:
    """(workflow, work_key) pairs for every run ``live_run_ids`` would also
    protect — running, or a resumable ``needs_input`` — that names a work_key.

    A parked (``needs_input``) run releases its keyed folder's cross-process
    lock the instant it parks (``WorkflowEngine.run``'s lock is scoped to one
    call, not to the run's whole paused lifetime), so ``artifacts.
    _prune_keyed_dirs``'s age sweep cannot rely on "is the lock held right
    now" alone to spare a parked run — it must also consult which
    (workflow, work_key) pairs a still-live manifest claims, the same "live"
    definition ``live_run_ids`` uses to protect per-run folders. A run with
    no work_key contributes nothing (there is no keyed folder to protect).
    Unreadable manifests are skipped, same as ``live_run_ids``."""
    out: set[tuple[str, str]] = set()
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
            workflow = rec.get("workflow")
            work_key = rec.get("work_key")
            if workflow and work_key:
                out.add((workflow, work_key))
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
        _write_json(_record_path(workspace, name, run_id), rec)
    except OSError:
        return False
    return True
