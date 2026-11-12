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
one of a racing pair wins — see its docstring. A ``running`` record whose process is
gone is the other one: the crash sweeps convert it (``mark_dead_run``) under the
same lock, so a resume claiming it and a sweep converting it never both act.

A stop someone asks for while a run executes is never written into the record:
the walk is that record's only writer then, and rewrites it whole without a lock,
so a second writer's change could be lost under the walk's next rewrite. The stop
is kept in a file of its own beside the record instead (``request_stop``), which
the crash conversion reads, so a run someone stopped ends ``cancelled`` even when
its process died before the stop took effect.
"""

from __future__ import annotations

import glob
import json
import time
from pathlib import Path
from typing import Callable

from loguru import logger

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.workflow import resume_store, workspace_fork


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

# Per-source cap for ``resume_inputs``: the last outputs of nodes that another
# node names as a source — ``inputs_from``, or a parallel node's ``list_from`` /
# ``branches_from`` — stored on a paused/aborted manifest so a resumed walk (whose
# in-memory trace starts empty) can still read them. A node composing its input
# sees a capped copy as it is; a parallel node refuses one that reached the cap
# rather than parse a list from a fragment.
RESUME_INPUT_MAX_CHARS = 8_000


def source_nodes(workflow) -> set[str]:
    """Every node another node names as a source: ``inputs_from``, or a parallel
    node's ``list_from`` / ``branches_from``. Their outputs are what a resumed
    walk, whose own trace starts empty, must still be able to read."""
    sources: set[str] = set()
    for node in workflow.nodes.values():
        sources.update(getattr(node, "inputs_from", ()) or ())
        sources.update(s for s in (getattr(node, "list_from", None),
                                   getattr(node, "branches_from", None)) if s)
    return sources


def _resume_inputs(workflow, result, carried: dict | None = None) -> dict[str, str] | None:
    """{source: capped last output} for every node another node names as a source
    (``inputs_from``, ``list_from``, ``branches_from``) — only for runs that can be
    resumed (needs_input, or aborted with a failed node), and only for sources
    that actually recorded an output.

    ``carried`` is what the record already held when this walk began: a resumed
    walk's trace covers only the nodes it ran, so a source from an earlier attempt
    is kept from there, and a source this walk ran again replaces its entry."""
    if workflow is None:
        return None
    resumable = (
        (result.status == "needs_input" and getattr(result, "needs_input_node", None))
        or (result.status == "aborted" and getattr(result, "failed_node", None))
    )
    if not resumable:
        return None
    sources = source_nodes(workflow)
    if not sources:
        return None
    out: dict[str, str] = {k: v for k, v in (carried or {}).items() if k in sources}
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
            "pass_number": getattr(r, "pass_number", None),
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


# Terminal statuses no resume can enter: a run that ends with one of these has no
# step left to enter, so its record names none and its resume files are removed.
# ``aborted`` and ``needs_input`` are resumable, and ``crashed`` is a run whose
# process died, so all three keep both.
UNRESUMABLE_ENDS = frozenset({"completed", "exhausted", "cancelled"})


def _drop_resume_files(workspace: str | Path, record: dict) -> None:
    """Remove the resume folder of a run whose record was just written with an
    end no resume can enter. Best-effort: never fails the write it follows."""
    if record.get("status") not in UNRESUMABLE_ENDS or not record.get("run_id"):
        return
    try:
        resume_store.discard(resume_store.store_dir_of(workspace, record))
    except Exception:  # noqa: BLE001 - leftover files are pruned with the run's folder later
        pass


# The default of ``update_run``'s ``position``: keep the recorded one. An explicit
# None is a value of its own: there is no step to enter — the walk ended, or the
# input of the step it enters next could not be stored.
KEEP_POSITION = object()


# Most rows of earlier walks a resumed run's record keeps in ``history``; the
# oldest are dropped first. The per-step pass counts in ``passes`` are carried
# separately, so they stay exact however many rows were dropped.
HISTORY_MAX_ROWS = 500


def pass_counts(record: dict) -> dict[str, dict[str, int]]:
    """Per-step pass counts of a run: ``{node_id: {"reached", "completed"}}``.
    ``reached`` is the highest attempt number (``iteration``) any row of the step
    carries; ``completed`` is how many distinct attempt numbers of the step have
    at least one row that did not fail. Counts carried in ``passes`` (earlier
    walks) are added to those of ``runs`` (the current walk), so they never
    depend on ``history``.

    A record resumed before ``passes`` existed (``resumed`` set, no ``passes``
    key) holds only its last walk in ``runs``, while attempt numbers ran on over
    every walk: the attempts below a step's lowest row ran in walks the record no
    longer holds. Counting only the rows would forget every pass those walks
    completed and loosen the step's bound, so on such a record every attempt
    from 1 to ``reached`` counts as completed unless its rows show it failed."""
    counts = {
        node: {"reached": int(c.get("reached", 0)), "completed": int(c.get("completed", 0))}
        for node, c in (record.get("passes") or {}).items()
    }
    reached: dict[str, int] = {}
    seen: dict[str, set[int]] = {}
    done: dict[str, set[int]] = {}
    for row in record.get("runs") or []:
        node = row.get("node_id")
        if not node:
            continue
        it = int(row.get("iteration") or 0)
        reached[node] = max(reached.get(node, 0), it)
        seen.setdefault(node, set()).add(it)
        if row.get("status") != "node_failed":
            done.setdefault(node, set()).add(it)
        else:
            done.setdefault(node, set())
    unrecorded_walks = bool(record.get("resumed")) and "passes" not in record
    for node, top in reached.items():
        entry = counts.setdefault(node, {"reached": 0, "completed": 0})
        entry["reached"] = max(entry["reached"], top)
        if unrecorded_walks:
            failed = seen[node] - done[node]
            entry["completed"] += top - len({it for it in failed if 1 <= it <= top})
        else:
            entry["completed"] += len(done[node])
    return counts


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
    position: dict | None = None,
    surface: str | None = None,
    launch: dict | None = None,
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
    node ids those attempts walked are kept in ``earlier_nodes`` and their rows in
    ``history`` (bounded), with per-step pass counts in ``passes``.
    ``position`` is the step the walk enters first, ``{"node", "seq"}``.
    ``surface`` is the surface that launched the run (``"agent"``, ``"service"``
    or None); a resumed walk keeps the value its record already has instead.
    ``launch`` says where the run was launched from (``{"kind": "session",
    "session_key", "channel", "chat_id"}`` for the agent's tool, ``{"kind":
    "automation", "name"}`` for an automation, ``{"kind": "service", "subject"}``
    for the dashboard and the API); it is written once, by the first walk: a
    resumed walk ignores the argument and keeps what its record has, none
    included, while ``root_session_key`` follows whoever resumes. A run with no
    calling conversation, and a sub-workflow's own run, record none."""
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
        # The surface that launched the run, from its first walk: a resume keeps
        # it, since the steps had the tools of the surface that launched them.
        "surface": prior.get("surface") if resumed else surface,
        # Where the run was launched from, from its first walk: a resume keeps it
        # (see the docstring), so a later reader still knows who started the run.
        "launch": prior.get("launch") if resumed else launch,
        # Sticky once set: every later rewrite carries it, and every resume of the
        # run sets it again, so a reader knows ``runs`` covers only the last walk.
        "resumed": resumed,
        # Every node the earlier attempts of a resumed run walked, gathered across
        # all its resumes and carried forward on every rewrite, so the run's route
        # is on record even though ``runs`` holds only the last walk.
        "earlier_nodes": earlier_nodes,
        # The rows of the earlier walks, oldest first and bounded, and the per-step
        # pass counts they add up to. The counts are computed from the full prior
        # record, so dropping old history rows never changes them.
        "history": (
            (list(prior.get("history") or []) + list(prior.get("runs") or []))[-HISTORY_MAX_ROWS:]
            if resumed else []
        ),
        "passes": pass_counts(prior) if resumed else {},
        # The source outputs the earlier attempts stored, carried forward the same
        # way: this walk's own trace cannot supply them if it stops again.
        "resume_inputs": (prior.get("resume_inputs") or None) if resumed else None,
        # The session of the last shared-context step that finished on the main
        # walk: it holds the whole shared conversation, so a resume reloads it.
        # Carried from the earlier walks until this walk finishes a shared step.
        "shared_session": prior.get("shared_session") if resumed else None,
        # The step to enter next and the sequence number of that position, which
        # names the file holding the step's whole input (resume_store). None once
        # the walk has ended, or while the step it enters has no stored input.
        "position": position,
        # The detached steps launched and not yet in ``runs``: {node_id,
        # iteration, session_key} each. A process that dies leaves them here,
        # and the crash conversion records each as a cut attempt.
        "detached_active": [],
        "runs": [],
    }
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
    return path


def update_run(
    workspace: str | Path, name: str, run_id: str, result, *, status: str = "running",
    shared_session: str | None = None,
    position: dict | None | object = KEEP_POSITION,
    detached_active: list[dict] | None = None,
) -> None:
    """Rewrite the manifest with the run's per-node trace so far, preserving the
    ``root_session_key``/``started_at`` from ``start_run``. Single-writer, full rewrite.
    ``shared_session`` replaces the recorded one when given; ``None`` keeps it.
    ``position`` replaces the recorded one when given, ``None`` included (no step
    to enter); left out, the recorded one is kept. ``detached_active`` replaces the
    recorded list of detached steps still running when given; ``None`` keeps it.

    With no record to carry from (it could not be read, or the workflow was
    renamed while the run was in flight and its folder moved), the record
    written names this process as its ``owner`` and now as its start: the walk
    writing it is alive, so the crash sweep leaves it alone while it runs and
    finds it dead once this process is gone. What only ``start_run`` knew
    (``root_session_key``, ``parent_run_id``, ``work_dir``, ``work_key``,
    ``task``, ``surface``, ``launch``) is not recoverable here and is left empty, so the
    record is marked ``incomplete: True``, carried by every later rewrite: a
    dead run with that mark is never resumed (``engine.resume_refusal``), since
    a resume would enter it as a top-level run, in another folder, under no
    root. Every key ``start_run`` writes is also written here, so no field's
    absence could tell such a record apart; the mark does."""
    path = _record_path(workspace, name, run_id)
    found = read_manifest(workspace, name, run_id)
    if found is None:
        from durin.utils.process_tree import process_identity

        now = time.time()
        base: dict = {"owner": process_identity(), "started_at": now, "ts": now,
                      "incomplete": True}
    else:
        base = found
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
        "surface": base.get("surface"),
        "launch": base.get("launch"),
        "resumed": base.get("resumed", False),
        "earlier_nodes": base.get("earlier_nodes") or [],
        "history": base.get("history") or [],
        "passes": base.get("passes") or {},
        "resume_inputs": base.get("resume_inputs"),
        "shared_session": shared_session if shared_session is not None else base.get("shared_session"),
        "position": base.get("position") if position is KEEP_POSITION else position,
        # The node that was in flight has now finished; leaving the marker set
        # would pin a completed node as running for readers of the manifest.
        "active_node": None,
        "detached_active": (list(base.get("detached_active") or [])
                            if detached_active is None else list(detached_active)),
        "runs": _node_records(result),
    }
    if base.get("incomplete"):
        record["incomplete"] = True
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

    Every other field is kept as recorded, ``detached_active`` included.

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
    shared_session: str | None = None,
) -> Path:
    """Terminal write: the run's final status, ``finished_at``, and full per-node trace.
    ``ts`` advances to ``finished_at`` so the dream cursor consumes the completed run.
    ``shared_session`` replaces the recorded one when given; ``None`` keeps it.
    A run that ends with a status no resume can enter (``UNRESUMABLE_ENDS``) has
    its resume files removed after the write."""
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
        "surface": prior.get("surface"),
        "launch": prior.get("launch"),
        "resumed": prior.get("resumed", False),
        "earlier_nodes": prior.get("earlier_nodes") or [],
        "history": prior.get("history") or [],
        "passes": prior.get("passes") or {},
        "shared_session": shared_session if shared_session is not None else prior.get("shared_session"),
        # A run that can still be resumed (aborted, paused) keeps the step it
        # stopped at, which a resume enters again; a run no resume can enter has
        # no step left, and its stored input is removed below.
        "position": None if result.status in UNRESUMABLE_ENDS else prior.get("position"),
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
        # {source: capped output} for the nodes other nodes read from — what a
        # resumed walk seeds its lookups with, including what earlier attempts stored.
        "resume_inputs": _resume_inputs(workflow, result, prior.get("resume_inputs")),
        "resume_upstream": (
            (getattr(result, "resume_upstream", None) or "")[:RESUME_UPSTREAM_MAX_CHARS]
            if getattr(result, "resume_upstream", None) is not None else None
        ),
        "output_files": list(getattr(result, "output_files", []) or []),
        "missing_artifacts": list(getattr(result, "missing_artifacts", []) or []),
        # Every detached step was joined before this write, its row in ``runs``.
        "detached_active": [],
        "runs": _node_records(result),
    }
    path = _record_path(workspace, name, result.run_id)
    _write_json(path, record)
    _drop_resume_files(workspace, record)
    # The run has ended, so a stop asked for while it ran has nothing left to
    # stop. Removed after the record is written: see request_stop.
    _drop_stop_request(stop_request_path(workspace, name, result.run_id))
    # Every script of the run has exited by now; a record left by an earlier
    # walk whose process died goes too.
    _drop_script_records(workspace, name, result.run_id)
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
    reject/approve, which leave both fields unset. When ``status`` is one no
    resume can enter (``UNRESUMABLE_ENDS``) the ``position`` is cleared and the
    run's resume files are removed. Returns the rewritten dict."""
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
    if status in UNRESUMABLE_ENDS:
        # Nothing is left to enter, so the record names no step and the step's
        # stored input goes away with the run's other resume files below.
        record["position"] = None
    if cancelled_by is not None:
        record["cancelled_by"] = cancelled_by
        record["cancelled_at"] = now
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
    _drop_resume_files(workspace, record)
    _drop_stop_request(stop_request_path(workspace, name, run_id))
    _drop_script_records(workspace, name, run_id)
    return record


# Lock target name, kept BESIDE the run's manifest file (not overwriting it) so it
# never matches the `*.json` glob list_runs/list_all_runs/reconcile_running walk —
# mirrors workflow/version_store.py's VERSION_LOCK_NAME/version_lock_target pattern.
_RUN_CLAIM_LOCK_SUFFIX = ".claim"

# The file holding a stop someone asked for, kept BESIDE the run's record for
# the same reason as the claim lock: no ``*.json`` listing can ever match it.
_STOP_REQUEST_SUFFIX = ".stop"

# How long ``request_stop`` waits for the per-run lock before giving up on
# keeping the stop (the live cancel flag its caller set still applies).
STOP_REQUEST_LOCK_TIMEOUT_S = 15.0


def stop_request_path(workspace: str | Path, name: str, run_id: str) -> Path:
    """Where the stop asked for a run is kept: ``<run_id>.stop`` beside its record."""
    return _wf_dir(workspace, name) / f"{run_id}{_STOP_REQUEST_SUFFIX}"


def _read_stop_request(path: Path) -> dict | None:
    """The stop kept at ``path`` as ``{"at", "hard"}``, or None when there is
    none. A file that exists but cannot be read as one is still a stop someone
    asked for: it reads as ``{"at": None, "hard": False}``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"at": None, "hard": False}
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        at = None
    return {"at": at, "hard": data.get("hard") is True}


def _write_stop_request(path: Path, stop: dict) -> None:
    atomic_write_text(path, json.dumps(stop))


def _drop_stop_request(path: Path) -> None:
    """Remove a kept stop. Best-effort: never fails the write it follows."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# Each script process a run starts is recorded in a file of its own beside the
# run's record while it runs, ``<run_id>.script.<pid>``: one file per process,
# so script branches running at once on several threads never rewrite a shared
# file. No ``*.json`` listing matches the name.
_SCRIPT_INFIX = ".script."

# How long the conversion of a dead run waits for a script it signalled: first
# after SIGTERM, then after SIGKILL. ``DEAD_RUN_SCRIPT_BOUND_S`` bounds the whole
# of it, observations included, and sits well under ``DEAD_RUN_LOCK_TIMEOUT_S``
# since it runs under the run's lock, which other finders wait for.
DEAD_RUN_SCRIPT_TERM_WAIT_S = 2.0
DEAD_RUN_SCRIPT_KILL_WAIT_S = 1.0
DEAD_RUN_SCRIPT_BOUND_S = 5.0


def record_script(record_dir: str | Path, run_id: str, *, pid: int, started: str,
                  node_id: str) -> Path:
    """Record a script process a run started, in ``record_dir`` (the folder of
    the run's record): ``{"pid", "started", "node_id"}``, written atomically.
    The process leads a process group of its own (its pid is the group id).
    Returns the file, which the caller removes once the process has exited."""
    path = Path(record_dir) / f"{run_id}{_SCRIPT_INFIX}{pid}"
    atomic_write_text(path, json.dumps({"pid": pid, "started": started, "node_id": node_id}))
    return path


def forget_script(path: str | Path | None) -> None:
    """Remove a script record (``record_script``). Best-effort."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def script_records(workspace: str | Path, name: str, run_id: str) -> list[Path]:
    """The script records of a run, one file per script process."""
    folder = _wf_dir(workspace, name)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{glob.escape(run_id)}{_SCRIPT_INFIX}*"))


def _drop_script_records(workspace: str | Path, name: str, run_id: str) -> None:
    for path in script_records(workspace, name, run_id):
        forget_script(path)


def _read_script_record(path: Path) -> dict | None:
    """A script record's ``{"pid", "started", "node_id"}``, or None when the file
    cannot be read as one (it is then skipped, never taken as proof of anything)."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(entry, dict):
        return None
    pid, started = entry.get("pid"), entry.get("started")
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(started, str) or not started):
        return None
    return entry


def _end_recorded_scripts(workspace: str | Path, name: str,
                          run_id: str) -> tuple[list[dict], list[Path]]:
    """End the script processes a dead run left (``process_tree.end_script_groups``)
    and return ``(left, ended)``: an entry per script of which something may
    still run, ``{"pid", "started", "node_id", "members", "unchecked"}``, and the
    record files of the scripts proven to be over (removed by the caller once
    the converted record is written). A file that cannot be read is skipped
    and left where it is: it proves nothing either way."""
    from durin.utils.process_tree import end_script_groups

    readable: list[tuple[Path, dict]] = []
    for path in script_records(workspace, name, run_id):
        entry = _read_script_record(path)
        if entry is None:
            logger.warning("run {} of workflow {!r}: script record {} cannot be read; "
                           "skipped", run_id, name, path.name)
            continue
        readable.append((path, entry))
    if not readable:
        return [], []
    outcomes = end_script_groups(
        [(e["pid"], e["started"]) for _path, e in readable],
        term_wait_s=DEAD_RUN_SCRIPT_TERM_WAIT_S, kill_wait_s=DEAD_RUN_SCRIPT_KILL_WAIT_S,
        bound_s=DEAD_RUN_SCRIPT_BOUND_S)
    left: list[dict] = []
    ended: list[Path] = []
    for (path, entry), outcome in zip(readable, outcomes):
        if outcome["ended"]:
            ended.append(path)
        else:
            left.append({"pid": entry["pid"], "started": entry["started"],
                         "node_id": entry.get("node_id"), "members": outcome["members"],
                         "unchecked": outcome["unchecked"]})
    return left, ended


def _descendant_records(workspace: str | Path, run_id: str) -> list[tuple[str, dict]]:
    """``(workflow name, record)`` of every sub-workflow run under ``run_id``, at
    any depth (records whose ``parent_run_id`` leads back to it)."""
    root = runs_root(workspace)
    if not root.is_dir():
        return []
    children: dict[str, list[tuple[str, dict]]] = {}
    for path in root.glob("*/*.json"):
        if path.name == ".cursor.json":
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        parent = rec.get("parent_run_id") if isinstance(rec, dict) else None
        if parent:
            children.setdefault(parent, []).append((path.parent.name, rec))
    out: list[tuple[str, dict]] = []
    pending, seen = [run_id], {run_id}
    while pending:
        for name, rec in children.get(pending.pop(), []):
            child_id = rec.get("run_id")
            if isinstance(child_id, str) and child_id not in seen:
                seen.add(child_id)
                out.append((name, rec))
                pending.append(child_id)
    return out


def _pids(pids: list[int]) -> str:
    return ", ".join(str(p) for p in pids)


class LeftBehind(str):
    """A refusal that names something the dead process left. It reads as its
    text everywhere a refusal is used; ``fact`` is the same thing as a clause
    that is true on its own (what is still there), for a caller that says it in
    words of its own."""

    fact: str

    def __new__(cls, text: str, fact: str) -> "LeftBehind":
        out = super().__new__(cls, text)
        out.fact = fact
        return out


def _left_script_refusal(node: object, state: str, own: list[int],
                         group: list[int]) -> str | None:
    """The refusal for one script a dead run left, or None when nothing of it
    is left. A process is said to be the script only when that is proven (its
    recorded identity); others are only in the script's former process group,
    which may not be the script's. Each states the condition under which the
    run can be resumed and gives the reader no order: a model reads it too, and
    must not end processes or remove files on its account."""
    if state == "alive":
        if own and group:
            return LeftBehind(
                f"the script of its step '{node}' from the earlier process is still "
                f"running (pid {_pids(own)}), and so are processes in its former "
                f"process group (pid {_pids(group)}), which may not be that script's. "
                "It can be resumed once they have all ended.",
                f"the script of its step '{node}' is still running (pid {_pids(own)}), "
                f"and so are processes in its former process group (pid {_pids(group)}), "
                "which may not be that script's")
        if own:
            return LeftBehind(
                f"the script of its step '{node}' from the earlier process is still "
                f"running (pid {_pids(own)}). It can be resumed once it has ended.",
                f"the script of its step '{node}' is still running (pid {_pids(own)})")
        return LeftBehind(
            f"processes in the former process group of the script of its step "
            f"'{node}' are still running (pid {_pids(group)}); they may not be that "
            "script's. It can be resumed once they have ended.",
            f"processes in the former process group of the script of its step '{node}' "
            f"are still running (pid {_pids(group)}), which may not be that script's")
    if state == "unknown":
        return LeftBehind(
            f"whether the script of its step '{node}' from the earlier process left "
            f"anything running could not be checked (pid {_pids(group)}). It can be "
            "resumed once nothing of that script is left.",
            f"whether the script of its step '{node}' ended could not be checked "
            f"(pid {_pids(group)})")
    return None


# What a resume is told when the run's record changed after it read it: the
# same words a resume that loses its claim gets.
_TAKEN_REFUSAL = "it was cancelled, or already resumed, before this reached it."
# What it is told when the run's record was removed after it read it.
_GONE_REFUSAL = "its record no longer exists."
# What it is told when a run's lock stays busy past DEAD_RUN_LOCK_TIMEOUT_S, or a
# sub-workflow run's record or a script record changed while it was checked.
_BUSY_REFUSAL = "another caller is acting on it right now. Try again in a moment."
# The refusals that come from another caller acting on the run while it is
# checked, not from anything the earlier process left.
DEAD_RUN_RACE_REFUSALS = frozenset({_TAKEN_REFUSAL, _GONE_REFUSAL, _BUSY_REFUSAL})


def _unreadable_script_refusal(path: Path) -> str:
    return LeftBehind(
        f"a record of one of its scripts, {path}, cannot be read, so whether that "
        "script still runs cannot be checked. It can be resumed once a person has "
        "made sure nothing of that script is left and has removed that file.",
        f"a record of one of its scripts, {path}, cannot be read, so whether that "
        "script still runs cannot be checked")


def _script_checks(workspace: str | Path, name: str,
                   rec: dict) -> list[tuple[Path | None, dict | None, str | None]]:
    """Observe the scripts one run left, holding no lock, in the order they are
    reported: ``(path, entry, refusal)`` per script record file (``entry`` None
    when the file cannot be read), then ``(None, entry, refusal)`` per
    ``scripts_left`` entry without a file. ``refusal`` is None on proof that
    nothing of that script is left. Stops at the first refusal: nothing after
    it is acted on."""
    from durin.utils.process_tree import left_script_state

    rec_id = rec.get("run_id")
    if not isinstance(rec_id, str):
        return []
    left = {e.get("pid"): e for e in rec.get("scripts_left") or [] if isinstance(e, dict)}
    checks: list[tuple[Path | None, dict | None, str | None]] = []
    for path in script_records(workspace, name, rec_id):
        entry = _read_script_record(path)
        if entry is None:
            checks.append((path, None, _unreadable_script_refusal(path)))
            return checks
        known = left.pop(entry["pid"], None) or {}
        members = known.get("members") or [{"pid": entry["pid"], "started": entry["started"]}]
        state, own, group = left_script_state(entry["pid"], entry["started"], members)
        refusal = _left_script_refusal(entry.get("node_id"), state, own, group)
        checks.append((path, entry, refusal))
        if refusal:
            return checks
    for entry in left.values():
        state, own, group = left_script_state(
            entry.get("pid"), entry.get("started"), entry.get("members") or [])
        refusal = _left_script_refusal(entry.get("node_id"), state, own, group)
        checks.append((None, entry, refusal))
        if refusal:
            return checks
    return checks


def _settle_scripts(checks: list[tuple[Path | None, dict | None, str | None]]) -> str | None:
    """Act on ``_script_checks``, under the run's lock: the first refusal, after
    removing the record files of the scripts before it that were proven over. A
    file is removed only when, read again here, it still names the process
    observed. One that names something else now was written after the
    observation, and nothing is known of it: ``_BUSY_REFUSAL``. One already
    gone was removed by another check that proved the same."""
    for path, entry, refusal in checks:
        if refusal:
            return refusal
        if path is None or not path.exists():
            continue
        if _read_script_record(path) != entry:
            return _BUSY_REFUSAL
        forget_script(path)      # proven over
    return None


def _with_run_lock(workspace: str | Path, name: str, run_id: str,
                   act: Callable[[dict | None], str | None]) -> str | None:
    """``act(the run's record as read now)`` under the run's lock, waiting for
    it up to ``DEAD_RUN_LOCK_TIMEOUT_S``; ``_BUSY_REFUSAL`` when it stays busy."""
    try:
        with cross_process_lock(run_lock_target(workspace, name, run_id),
                                timeout=DEAD_RUN_LOCK_TIMEOUT_S):
            return act(read_manifest(workspace, name, run_id))
    except TimeoutError:
        return _BUSY_REFUSAL


def dead_run_refusal(workspace: str | Path, record: dict) -> str | None:
    """Why a ``crashed`` run must not be resumed yet, beyond what
    ``engine.resume_refusal`` reads from its record, or None. Asked in order:

    - the record still names the process that was running it (``owner``) and
      that process is alive, or cannot be observed. ``mark_dead_run`` removes
      ``owner`` when it converts a run, since its conversion is the proof that
      the process was gone; a ``crashed`` record that still names one was
      marked by something else, which proved nothing;
    - one of its sub-workflow runs, at any depth, is still recorded as
      ``running``: its process may still be working, and nothing of it has
      been looked at yet;
    - a script record (``record_script``) is still present beside the run's
      record or one of its sub-workflow runs' records: something of that
      script may still run. A record that cannot be read refuses with its
      path. One that can is checked with ``process_tree.left_script_state``
      (together with the record's ``scripts_left`` entry for it, which names
      the members seen in its group) and removed on proof that nothing of the
      script is left;
    - a ``scripts_left`` entry whose script may still run.

    Processes are observed holding no lock. What is said and removed is
    decided under each run's lock (``run_lock_target``), one run's lock at a
    time, after reading its record again: when the run's record is no longer
    ``record`` (a resume claimed it, so its new walk may be writing script
    records of its own, or it was ended), nothing is removed and the answer is
    ``_TAKEN_REFUSAL`` (``_GONE_REFUSAL`` when the record was removed); a
    sub-workflow run's record, or a script record, that changed meanwhile gives
    ``_BUSY_REFUSAL``, as does a lock that stays busy. These three are
    ``DEAD_RUN_RACE_REFUSALS``. Every other answer is a ``LeftBehind``: the
    refusal text, with the fact it rests on in ``fact``.

    Observes processes and may wait for a lock, so a caller on an event loop
    runs it off the loop."""
    from durin.utils.process_tree import process_alive

    name, run_id = record.get("workflow"), record.get("run_id")
    if not isinstance(name, str) or not isinstance(run_id, str):
        return None
    owner = record.get("owner")
    owner_alive = owner is not None and process_alive(owner)
    descendants = _descendant_records(workspace, run_id)
    running = next(((n, c) for n, c in descendants if c.get("status") == "running"), None)
    checks = [] if owner_alive or running else _script_checks(workspace, name, record)

    def act_on_run(current: dict | None) -> str | None:
        if current is None:
            return _GONE_REFUSAL
        if current != record:
            return _TAKEN_REFUSAL
        if owner_alive:
            pid = owner.get("pid") if isinstance(owner, dict) else owner
            return LeftBehind(
                f"it was marked interrupted while the process that was running it "
                f"(pid {pid}) still seems alive. It can be resumed once that process "
                "has ended.",
                f"the process that was running it (pid {pid}) still seems alive")
        if running:
            child_name, child = running
            return LeftBehind(
                f"its sub-workflow run '{child.get('run_id')}' of workflow '{child_name}' "
                "is still recorded as running. It can be resumed once that run has ended "
                "or been found dead.",
                f"its sub-workflow run '{child.get('run_id')}' of workflow '{child_name}' "
                "is still recorded as running")
        return _settle_scripts(checks)

    refusal = _with_run_lock(workspace, name, run_id, act_on_run)
    if refusal:
        return refusal
    for child_name, child in descendants:
        child_id = child.get("run_id")
        if not isinstance(child_id, str):
            continue
        child_checks = _script_checks(workspace, child_name, child)

        def act_on_child(current: dict | None, child: dict = child,
                         child_checks: list = child_checks) -> str | None:
            return _BUSY_REFUSAL if current != child else _settle_scripts(child_checks)

        refusal = _with_run_lock(workspace, child_name, child_id, act_on_child)
        if refusal:
            return refusal
    return None


def settle_dead_run(workspace: str | Path, name: str,
                    run_id: str) -> tuple[dict | None, str | None]:
    """What a resume needs before it decides: the run's record after any
    conversion it is owed, and why the resume of a ``crashed`` run must wait
    (``dead_run_refusal``), or None. A ``running`` record whose
    process is gone is converted (``mark_dead_run``); a ``crashed`` one also
    gets its dead sub-workflow runs converted, which a conversion made before
    may have missed, so their scripts are ended and counted too; the refusal
    is ``dead_run_refusal``'s. Takes the per-run locks one at a time and
    observes processes: run it off an event loop."""
    record = read_manifest(workspace, name, run_id)
    if isinstance(record, dict) and record.get("status") == "running":
        mark_dead_run(workspace, name, run_id)
        record = read_manifest(workspace, name, run_id)
    if not isinstance(record, dict) or record.get("status") != "crashed":
        return record, None
    _mark_dead_children(workspace, run_id, now=time.time(), max_age_s=RECONCILE_AGE_S)
    return record, dead_run_refusal(workspace, record)


def stop_requested(workspace: str | Path, name: str, run_id: str) -> dict | None:
    """The stop someone asked for this run while it ran, ``{"at", "hard"}``, or
    None. Kept beside the record until the run ends (see ``request_stop``)."""
    return _read_stop_request(stop_request_path(workspace, name, run_id))


def _is_running(workspace: str | Path, name: str, run_id: str) -> bool:
    record = read_manifest(workspace, name, run_id)
    return isinstance(record, dict) and record.get("status") == "running"


def request_stop(workspace: str | Path, name: str, run_id: str, *, hard: bool) -> bool:
    """Keep a stop someone asked for a ``running`` run, so it outlives the
    process: if the process dies before the run ends, ``mark_dead_run`` ends
    the run ``cancelled`` instead of offering it for resume.

    The stop is written to its own file beside the record (``stop_request_path``),
    ``{"at": <epoch seconds>, "hard": bool}``, never into the record: the walk
    rewrites the record whole without a lock, and a change written into it here
    could be lost under the walk's next rewrite. A second request keeps the
    first ``at`` and can only turn ``hard`` from False to True. Two requests are
    serialized by ``run_lock_target``'s lock.

    Returns True when the stop is kept. Returns False, and keeps nothing, when
    the record does not exist or is not ``running``, or when the lock stayed
    busy or the file could not be written (logged; never raises). This only
    keeps the stop: the caller stops the run itself with
    ``cancellation.request_cancel``.

    The walk ends the run without the lock, and removes the file after its end
    is written. So the file is written first and the record read again after:
    a run that ended in between has its file removed here, and one that ends
    later removes it itself. Either way no stop is left beside an ended run,
    where a later resume of it would find it."""
    if not _is_running(workspace, name, run_id):
        return False
    path = stop_request_path(workspace, name, run_id)
    try:
        with cross_process_lock(run_lock_target(workspace, name, run_id),
                                timeout=STOP_REQUEST_LOCK_TIMEOUT_S):
            if not _is_running(workspace, name, run_id):
                return False
            prior = _read_stop_request(path)
            stop = {
                "at": (prior or {}).get("at") or time.time(),
                "hard": bool(hard) or bool(prior and prior["hard"]),
            }
            if stop != prior:
                _write_stop_request(path, stop)
            if not _is_running(workspace, name, run_id):
                _drop_stop_request(path)
                return False
            return True
    except TimeoutError:
        logger.warning("run {} of workflow {!r}: its lock stayed busy; the stop asked "
                       "for it is not kept beside its record", run_id, name)
        return False
    except OSError:
        logger.exception("run {} of workflow {!r}: could not keep the stop asked for it",
                         run_id, name)
        return False


def run_lock_target(workspace: str | Path, name: str, run_id: str) -> Path:
    """The cross-process lock target serializing every caller that checks a
    resumable run's status and then acts on it — a ``needs_input`` run, an
    ``aborted`` one with a ``failed_node``, or a ``crashed`` one with a
    position: ``WorkflowsService.cancel_run``
    finalizing a paused run, every resume claiming the run (moving it to
    ``running`` before actually resuming it — ``ResumeClaim``, used by the
    workflows service and the ``run_workflow`` tool alike), and the
    approval-reject short-circuit inside the service's resume path, the crash
    conversion of a ``running`` run whose process is gone (``mark_dead_run``),
    ``dead_run_refusal`` removing a dead run's script records, and
    ``request_stop`` keeping a stop beside a running record. Each of those
    is a read-then-write on the SAME manifest; without a lock, two racing callers
    can both read the resumable status before either writes, so — for example —
    two resumes both run real nodes, or a resume runs them on a run
    ``cancel_run`` just finalized. Whichever caller acquires the lock first wins
    outright: the loser's own re-read, taken only once it acquires the lock in
    turn, sees the winner's already-written status and refuses instead of acting
    a second time. Every OTHER manifest write (a running walk's own per-node
    updates, a terminal ``finalize_run``) stays single-writer with no lock, same
    as this module's own docstring says — this guards only the check-then-act
    window on a stopped run, where an outside caller (not the walk itself) can
    act on the manifest."""
    return _wf_dir(workspace, name) / f"{run_id}{_RUN_CLAIM_LOCK_SUFFIX}"


def claim_for_resume(workspace: str | Path, name: str, run_id: str) -> dict:
    """Move a resumable manifest (``needs_input``, ``aborted`` with a
    ``failed_node``, or ``crashed`` with a position) to ``running`` IN PLACE,
    owned by this process, preserving
    every other field — called under ``run_lock_target``'s lock, right before the
    caller releases it and actually resumes the run, so a second resume or a
    ``cancel_run`` (or an approval reject) racing in right behind sees this run
    is no longer resumable and refuses instead of acting on a run that is, by
    then, genuinely resuming. ``WorkflowEngine.run``'s own ``_start_manifest``
    fully rewrites the manifest once the resume gets a workflow-run thread (a
    fresh ``started_at``, the resumed walk's own ``runs``) — this claim only
    needs to survive until that first real write, not to be a lasting record
    itself. That can take a while when every run thread is busy, and a stopped
    run is usually resumed after a restart, so its recorded owner is often a dead
    process: the claim re-stamps the owner, or the crash sweep would flip the
    waiting resume to ``crashed``. A resume whose engine never makes that write
    undoes the claim with ``release_resume_claim``. A stop file still beside the
    record belongs to an earlier walk (a process that died while keeping one)
    and is removed, so it cannot end this walk cancelled. Returns the claimed
    record."""
    from durin.utils.process_tree import process_identity

    prior = read_manifest(workspace, name, run_id) or {}
    record = dict(prior)
    record["status"] = "running"
    record["owner"] = process_identity()
    path = _record_path(workspace, name, run_id)
    _write_json(path, record)
    _drop_stop_request(stop_request_path(workspace, name, run_id))
    return record


def release_resume_claim(
    workspace: str | Path, name: str, run_id: str, *, prior: dict, claimed: dict,
) -> bool:
    """Undo ``claim_for_resume`` for a resume whose engine never wrote the run's
    own manifest: put back ``prior``, the record exactly as it was before the
    claim (``needs_input``, ``aborted`` or ``crashed``, with the owner it had),
    so the run can be resumed — or, when paused, cancelled — again. A claimed manifest owned by
    this live process is one that neither the crash sweep nor the ``tasks``
    self-heal ever ends, and one ``cancel_run`` and a new resume both refuse.
    Does nothing — returns False — once the manifest differs from ``claimed``:
    the engine took the run over. Called under ``run_lock_target``'s lock, like
    the claim."""
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


def runs_launched_by(workspace: str | Path, name: str, session_key: str) -> list[dict]:
    """The full records of workflow ``name`` that the conversation ``session_key``
    launched, newest first. A record says so by its ``launch`` (a session launch
    names the conversation; any other launch names none); a record written before
    launches were kept says so by its ``root_session_key``. A sub-workflow's own
    run is never one. Reads only that workflow's folder and skips a file that
    cannot be read; blocking, so an event loop calls it through a thread."""
    folder = _wf_dir(workspace, name)
    if not session_key or not folder.is_dir():
        return []
    out: list[dict] = []
    for f in folder.glob("*.json"):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("run_id"), str):
            continue
        if rec.get("parent_run_id"):
            continue
        launch = rec.get("launch")
        if launch is None:
            launched_from = rec.get("root_session_key")
        elif isinstance(launch, dict) and launch.get("kind") == "session":
            launched_from = launch.get("session_key")
        else:
            launched_from = None
        if launched_from == session_key:
            out.append(rec)
    out.sort(key=lambda r: r.get("started_at") or r.get("ts") or 0.0, reverse=True)
    return out


# The error of the row a dead run's conversion writes for an attempt that was cut.
DEAD_RUN_ERROR = "the process ended while this step was running"

# How long a conversion waits for the per-run lock. A record whose lock is busy
# for longer is left for the next sweep.
DEAD_RUN_LOCK_TIMEOUT_S = 15.0


def _owner_gone(record: dict, now: float, max_age_s: float) -> bool:
    """Whether the process executing a ``running`` record is gone. A record with
    an ``owner`` is dead as soon as that process is not alive; a record without
    one (written before owners existed) only once its ``started_at``, a positive
    time, is older than ``max_age_s``. Without such a start time the record has
    no age to judge it by, and it is never taken for dead: it may belong to a
    walk that is still running."""
    from durin.utils.process_tree import process_alive

    owner = record.get("owner")
    if owner is not None:
        return not process_alive(owner)
    try:
        started = float(record.get("started_at"))
    except (TypeError, ValueError):
        return False   # no start time, or an unreadable one
    return 0.0 < started < now - max_age_s


def run_in_flight(workspace: str | Path, name: str) -> str | None:
    """The id of a run of workflow ``name`` recorded ``running`` whose process
    is not provably gone (``_owner_gone``: alive, or not observable), or None.
    A ``running`` record whose owner is gone is a dead run, not one in flight.
    Reads only; observes processes, so it is not for an event loop."""
    folder = _wf_dir(workspace, name)
    if not folder.is_dir():
        return None
    now = time.time()
    for f in sorted(folder.glob("*.json")):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(rec, dict) and rec.get("status") == "running"
                and not _owner_gone(rec, now, RECONCILE_AGE_S)):
            return rec.get("run_id") or f.stem
    return None


def _cut_row(entry: dict) -> dict:
    """The row of an attempt the process's death cut short: a failed attempt, so
    it uses an attempt number but no pass, with every field a finished row has
    and ``cut: True``. ``entry`` is an ``active_node`` or ``detached_active`` item."""
    return {
        "node_id": entry["node_id"],
        "iteration": entry.get("iteration"),
        "passed": None,
        "session_key": entry.get("session_key"),
        "worker_index": None,
        "branch_id": None,
        "budget": None,
        "pass_number": None,
        "status": "node_failed",
        "route_label": None,
        "exit_code": None,
        "duration_s": None,
        "error": DEAD_RUN_ERROR,
        "artifacts": [],
        "command": None,
        "stdout": None,
        "stderr": None,
        "model": None,
        "provider": None,
        "node_hash": None,
        "origin_run_id": None,
        "cut": True,
    }


def _dead_run_record(workspace: str | Path, record: dict, now_ts: float,
                     stop: object = None) -> dict:
    """The record a dead ``running`` run is converted to (see ``mark_dead_run``).
    ``stop`` is the stop someone asked for the run, or None. Raises when the
    record or its resume files cannot be read as expected; the caller then
    falls back to ``_minimal_mark``."""
    position = record.get("position")
    if position is not None and not (isinstance(position, dict) and position.get("node")):
        raise ValueError(f"the position is not a step: {position!r}")
    active = record.get("active_node")
    stopped = bool(stop)
    if not isinstance(record.get("runs") or [], list):
        raise ValueError("the runs are not a list")
    detached = record.get("detached_active") or []
    if not isinstance(detached, list) or not all(
            isinstance(e, dict) and isinstance(e.get("node_id"), str) for e in detached):
        raise ValueError("a detached_active entry names no step")
    rows = list(record.get("runs") or [])
    cut: set[tuple] = set()
    if position and isinstance(active, dict) and active.get("node_id") == position["node"]:
        rows.append(_cut_row(active))
        cut.add((active["node_id"], active.get("iteration")))
    for entry in detached:
        key = (entry["node_id"], entry.get("iteration"))
        if key not in cut:
            rows.append(_cut_row(entry))
            cut.add(key)
    # A detached step that started is never a step to run again: a resume would
    # launch its side effect a second time. The engine never writes one as the
    # position (a launch whose next input cannot be stored names no step), so a
    # record naming one comes from another build; it keeps that position, unnamed
    # as failed.
    started_detached = {e["node_id"] for e in detached}

    out = dict(record)
    out.pop("owner", None)
    out.update(
        status="cancelled" if stopped else "crashed",
        active_node=None,
        runs=rows,
    )
    if "position" in record:
        # A record written before positions existed is only marked, as the
        # sweep always marked it; every later record gets its end time.
        out.update(finished_at=now_ts, ts=now_ts)
    if "detached_active" in record:
        out["detached_active"] = []
    if stopped:
        # A stop was asked for: the run ends where nothing can resume it, so it
        # names no step and its resume files are removed after the write. The
        # stop itself goes on the record, which is the run's last word.
        out["position"] = None
        out["stop_requested"] = stop
    elif position and position["node"] not in started_detached:
        # The step the record names is the step the run failed at: the one that
        # was running, or the one about to start. What it was to receive comes
        # from the resume store, used only when stored for that same step.
        out["failed_node"] = position["node"]
        store = resume_store.store_dir_of(workspace, record)
        stored = resume_store.load_input(store, position.get("seq") or 0)
        if stored is not None and stored[0] == position["node"]:
            upstream = stored[1]
            if upstream is not None and not isinstance(upstream, str):
                raise ValueError("the stored input of the step is not text")
            out["resume_upstream"] = (
                upstream[:RESUME_UPSTREAM_MAX_CHARS] if upstream is not None else None)
        recorded = record.get("resume_inputs") or {}
        if not isinstance(recorded, dict):
            raise ValueError("the recorded resume_inputs are not a mapping")
        sources = {node: text[:RESUME_INPUT_MAX_CHARS]
                   for node, text in resume_store.load_sources(store).items()}
        out["resume_inputs"] = {**recorded, **sources} or None
    return out


def _minimal_mark(record: dict, stop: object = None) -> dict:
    """The mark a dead run gets when its full conversion cannot be built: status
    ``crashed``, ``active_node`` cleared and ``owner`` removed (both describe a
    process found gone), every other field as it was. The run then reads as
    ended, and no sweep finds it again.

    A run someone asked to stop (``stop``) is marked ``cancelled`` instead, with
    the stop on the record and no step named, so it is never offered for resume
    whatever else the record carries."""
    out = dict(record)
    out.pop("owner", None)
    out.update(status="crashed", active_node=None)
    if stop:
        out.update(status="cancelled", stop_requested=stop)
        if "position" in out:
            out["position"] = None
    return out


def mark_dead_run(
    workspace: str | Path, name: str, run_id: str, *,
    now: float | None = None, max_age_s: float = RECONCILE_AGE_S,
) -> dict | None:
    """Convert a ``running`` run whose process is gone into the record a failed
    step leaves, so a resume can enter it. Returns the written record, or None
    when nothing was written: the record is not ``running``, its owner is alive
    (an owner-less one counts as dead once its positive ``started_at`` is older
    than ``max_age_s`` before ``now``, and never without one), the file cannot
    be read or parsed as a record, the write failed, or its lock stayed busy for
    ``DEAD_RUN_LOCK_TIMEOUT_S``. Never raises. A
    dead run whose conversion cannot be built (a field or a resume file of an
    unexpected shape) gets ``_minimal_mark`` instead, with one warning.

    The record is re-read under ``run_lock_target``'s lock, so two finders (the
    sweep and the ``tasks`` tool, or two processes) convert it once, and a
    resume that claimed it first (owner re-stamped to a live process) is left
    alone. The conversion:

    - ``status``: ``cancelled`` when someone asked to stop the run — a stop file
      kept beside the record (``request_stop``), or a ``stop_requested`` the
      record holds itself — else ``crashed``; ``finished_at``/``ts`` now;
      ``active_node`` None and ``owner`` removed, since both describe a
      process that no longer exists. A record without ``owner`` is one this
      conversion made: it is the proof that the owner was gone, which a later
      reader cannot repeat (after a restart in place the same pid and start
      belong to the restarted process, and only that process can tell);
    - a row for the step that was running (when ``active_node`` is the step the
      ``position`` names) and for every step in ``detached_active``:
      ``status: "node_failed"``, ``cut: True``, ``error`` ``DEAD_RUN_ERROR``;
      ``detached_active`` then empty;
    - when not cancelled and the record has a position: ``failed_node`` (the
      position's step), ``resume_upstream`` (its stored input, capped) and
      ``resume_inputs`` (the stored sources, capped, over the recorded ones);
    - when cancelled: ``position`` None, the stop written to the record's
      ``stop_requested`` (the stop file's value when there is one), and the
      resume files removed.

    The stop file is removed once the converted record is written; a write
    that fails leaves it, for the next sweep to find.

    The run's process counts as gone only on proof (``process_tree.process_alive``):
    when it cannot be observed, the record is left ``running`` for the next
    sweep and nothing is signalled.

    Before the record is written, the script processes the run recorded
    (``record_script``) are ended (``process_tree.end_script_groups``): a group
    is signalled only after its leader is verified to be the recorded process
    and to lead its group, SIGTERM then SIGKILL, and a group counts as ended
    only when no member that has not exited is left. Otherwise a resume would
    run the step again while its earlier script still runs. This happens under
    the lock: a resume claims the run under the same lock, so no resume can
    start the step again between the check that the run's process is gone and
    the end of its scripts; the whole of it is bounded by
    ``DEAD_RUN_SCRIPT_BOUND_S``, well under the lock's timeout. A script of
    which something may still run (members left after SIGKILL, members of a
    group whose leader was already gone, which are never signalled, or an
    observation that failed) is named in the record's ``scripts_left``
    (``{"pid", "started", "node_id", "members", "unchecked"}``, ``members`` the
    identities seen in its group), and ``dead_run_refusal`` refuses the
    resume until nothing of it is left. Only the records of scripts proven over
    are removed, once the converted record is written.

    The private copies the run's parallel step (``choose``/``union``) was
    writing in are removed with the conversion, in two moves: under the lock
    they are renamed out of the run's name (``workspace_fork.retire_branches``),
    and once the lock is released the renamed directory is deleted. The delete
    cannot take the copies of a resume that claimed the run right after the
    conversion, since those are made under the run's name. Copies stay when
    ``scripts_left`` is set: a script may still be writing in one, and the step
    removes them when it is entered again. Each run id handles its own copies,
    so a converted sub-workflow run (below) clears its own.

    Then the dead sub-workflow runs under this run (``running`` records whose
    ``parent_run_id`` leads back to it) are converted the same way, so their
    scripts end too before a resume runs the sub-workflow step again.

    A step listed in ``detached_active`` is never written as ``failed_node``:
    a record naming one as its position keeps that position and gets no
    ``failed_node`` or resume fields.

    A record without a position (written before positions existed, or a
    sub-workflow's own run) gets no ``failed_node`` and no resume fields. One
    written before positions existed (no ``position`` key) also keeps its
    ``finished_at``/``ts``: it is only marked."""
    now = time.time() if now is None else now
    try:
        with cross_process_lock(run_lock_target(workspace, name, run_id),
                                timeout=DEAD_RUN_LOCK_TIMEOUT_S):
            record = read_manifest(workspace, name, run_id)
            if not isinstance(record, dict) or record.get("status") != "running":
                return None
            if not _owner_gone(record, now, max_age_s):
                return None
            stop_path = stop_request_path(workspace, name, run_id)
            stop = _read_stop_request(stop_path) or record.get("stop_requested") or None
            try:
                out = _dead_run_record(workspace, record, time.time(), stop)
            except Exception as exc:  # noqa: BLE001 - an odd record is still marked ended
                logger.warning("run {} of workflow {!r}: its process is gone, but its record "
                               "cannot be converted ({}); marked {} only",
                               run_id, name, exc, "cancelled" if stop else "crashed")
                out = _minimal_mark(record, stop)
            left, ended = _end_recorded_scripts(workspace, name, run_id)
            if left:
                logger.warning("run {} of workflow {!r}: {} script(s) of the dead run may "
                               "still run: {}", run_id, name, len(left),
                               ", ".join(str(m["pid"]) for e in left for m in e["members"]))
                out["scripts_left"] = left
            _write_json(_record_path(workspace, name, run_id), out)
            _drop_stop_request(stop_path)
            for path in ended:
                forget_script(path)
            retired = _retire_copies(out, record, run_id)
    except TimeoutError:
        logger.warning("run {} of workflow {!r}: its lock stayed busy; the crash sweep "
                       "leaves it for its next round", run_id, name)
        return None
    except Exception:  # noqa: BLE001 - a sweep must never stop on one record
        logger.exception("run {} of workflow {!r}: could not write the dead run's record",
                         run_id, name)
        return None
    if retired is not None:
        workspace_fork.cleanup(retired)
    if out["status"] in UNRESUMABLE_ENDS:
        _drop_resume_files(workspace, out)
    _mark_dead_children(workspace, run_id, now=now, max_age_s=max_age_s)
    return out


def _retire_copies(out: dict, record: dict, run_id: str) -> Path | None:
    """Under the run's lock: rename the private copies of the dead run's parallel
    step (``branches/<run_id>`` beside its working folder) out of the run's name,
    and return the new path. The caller deletes it once the lock is released: a
    resume claims the run under the same lock and its step makes new copies under
    ``branches/<run_id>``, so a delete after the claim would remove those. With
    ``scripts_left`` a script may still be writing in a copy, so they stay (the
    step removes them when it is entered again)."""
    work_dir = out.get("work_dir") or record.get("work_dir")
    if not work_dir or out.get("scripts_left"):
        return None
    return workspace_fork.retire_branches(work_dir, run_id)


def _mark_dead_children(workspace: str | Path, run_id: str, *, now: float,
                        max_age_s: float) -> None:
    """Convert the dead ``running`` sub-workflow runs under ``run_id``, at any
    depth (a child already ``crashed`` can still have a running child of its
    own). One run's lock at a time. Best-effort: a sweep finds any left."""
    for name, rec in _descendant_records(workspace, run_id):
        child_id = rec.get("run_id")
        if rec.get("status") == "running" and isinstance(child_id, str):
            mark_dead_run(workspace, name, child_id, now=now, max_age_s=max_age_s)


def reconcile_running(workspace: str | Path, *, now: float, max_age_s: float) -> int:
    """Convert orphaned ``running`` manifests with ``mark_dead_run`` (keeping the
    partial trace).

    A manifest that records an ``owner`` is converted as soon as that process is
    dead — no age heuristic, so a gateway that crashes and restarts within
    minutes still clears its ghosts, while a run owned by another LIVE process
    (e.g. the TUI sharing this workspace) is never touched. Ownerless legacy
    manifests fall back to the ``started_at`` age cutoff; one without a positive
    ``started_at`` is never converted. Safe to run at boot AND periodically.
    Returns how many records were converted; a malformed record, or one whose
    lock is busy, is skipped, never fatal."""
    root = runs_root(workspace)
    if not root.is_dir():
        return 0
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
            # Only a record that looks dead without the lock is worth locking:
            # the conversion checks it again under the lock.
            if not isinstance(rec, dict) or rec.get("status") != "running":
                continue
            if not _owner_gone(rec, now, max_age_s):
                continue
            if mark_dead_run(workspace, wf_dir.name, f.stem,
                             now=now, max_age_s=max_age_s) is not None:
                count += 1
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
    about the node's real cost. So is a cut row (``cut: True``, an attempt the
    process's death ended): it says how long the step ran before the process
    died, not how long it takes.
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
            if d is None or not nid or r.get("status") == "reused" or r.get("cut"):
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
            # A stop file goes with its record (an ended run keeps none, but
            # one a dead process left behind must not outlive the record).
            _drop_stop_request(path.with_suffix(_STOP_REQUEST_SUFFIX))
            _drop_script_records(workspace, name, path.stem)
    except OSError:
        pass


def reconcile_one(workspace: str | Path, name: str, run_id: str) -> bool:
    """Convert ONE ``running`` manifest with ``mark_dead_run`` iff its owner
    process is dead. The self-heal the `tasks` tool applies when the user pokes
    a run the sweep hasn't reached yet — so "status"/"stop" answer with the
    truth instead of describing a process that no longer exists. Ownerless
    legacy manifests are left to the age sweep. Returns True when converted,
    whatever status the run ended with (``crashed``, or ``cancelled`` when a
    stop had been asked for)."""
    rec = read_manifest(workspace, name, run_id)
    if not isinstance(rec, dict) or rec.get("status") != "running" or rec.get("owner") is None:
        return False
    return mark_dead_run(workspace, name, run_id) is not None
