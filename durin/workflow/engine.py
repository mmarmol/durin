"""The sequential flow-graph engine.

Walks a parsed Workflow from its start node, following edges. A work node runs via
the injected ``node_runner`` and its output passes along the edge to the next node;
a 'shared'-context node also reads/extends a running shared-context buffer, while an
'own'-context node is isolated (it sees only the upstream output). A routing node
derives a verdict from its own output and follows the matching edge: a binary node
(on_pass/on_fail) routes on a PASS/FAIL verdict (first-line parse_verdict); a multi-way
node (cases) routes on which declared label the agent emits (parse_label) — to that
label's target, to "default", or aborting if neither. A per-node visit cap guards
against infinite loop-backs. The run returns a typed WorkflowResult.

The graph logic is decoupled from real LLM execution: ``node_runner`` is injectable
so this engine is fully unit-testable with a mock. The default runner that wraps
AgentRunner + persists node sessions is Task 5.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.workflow import run_log, workspace_fork
from durin.workflow.artifacts import artifact_dir, prune_runs
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import (
    NEEDS_INPUT_TARGET,
    ParallelNode,
    ScriptNode,
    SubworkflowNode,
    Workflow,
    WorkNode,
    node_label,
)
from durin.workflow.verdict import parse_label, parse_verdict, strip_label_line, strip_verdict_line


@dataclass
class NodeRunRequest:
    node: WorkNode | ScriptNode
    task: str
    upstream_output: str | None
    shared_context: list[dict]
    run_id: str
    iteration: int
    root_session_key: str | None
    # When set, the node's file tools operate here (a private branch copy) instead of
    # the shared workspace — used by writing-in-parallel so branches don't collide.
    workspace_override: str | None = None
    # Engine-provided working folder for this node: read earlier steps' files here and
    # write yours here. Under the run's one shared working folder this is the same path
    # for every sequential node, so files accumulate and each stage sees the prior work.
    # None when the engine has no workspace or for a no-tools node (which does no file I/O).
    output_dir: str | None = None
    # Index within a dynamic fan-out batch (0, 1, 2, …). When set, the session-persist
    # key includes this suffix so each worker gets a distinct session rather than
    # all workers overwriting the same key.
    worker_index: int | None = None
    # The node's effective visit budget (min of its own max_visits / the workflow
    # default / the global ceiling). Lets the runner tell the model which pass this
    # is and that the final allowed pass IS final. None when budgets don't apply
    # (parallel branches/workers, which are not loop targets).
    budget: int | None = None
    # True when this is a binary routing node whose on_fail target has no visits
    # left: a FAIL verdict now ends the run as 'exhausted' instead of looping. The
    # runner tells the gate so its last verdict is definitive, not another loop turn.
    fail_would_exhaust: bool = False
    # The engine's own cooperative-cancel poll, handed only to a script node so its
    # runner can check it mid-subprocess (an agent turn has no equivalent mid-turn
    # hook, so it stays None there — unchanged, between-nodes-only cancellation).
    cancel_check: Callable[[], bool] | None = None
    # Synchronous sink for in-node progress ({"round", "activity"}). The node runs
    # on a worker thread with its own event loop, so this must never be awaited
    # from inside the node — it marshals to the gateway loop itself.
    progress: Any = None


@dataclass
class NodeRunResponse:
    output: str
    session_key: str | None = None
    # The node's OWN contribution to the conversation (its user turn + the turns it
    # generated) — NOT the full prompt. The engine extends the shared-context buffer
    # with exactly this, so inherited context and system prompts never re-enter it.
    messages: list[dict] = field(default_factory=list)
    # True when the node ran but persisting its session failed (the conversation is
    # lost). Lets the engine record a truthful 'persist_failed' status instead of a
    # misleading 'ok' with a silently-absent session.
    persist_failed: bool = False
    # The routing verdict the node recorded via the forced `route` tool call (a deterministic
    # label from the node's own enum). None when the node does not route or the route call could
    # not produce a valid label — the engine then falls back to parsing the node's text output.
    route_label: str | None = None
    # The subprocess exit code for a script node (None for agent nodes, which
    # have no exit code). Recorded in the NodeRun trace and the run manifest.
    exit_code: int | None = None


NodeRunner = Callable[[NodeRunRequest], NodeRunResponse]


@dataclass
class ResumeState:
    """Where to re-enter a run that ended needs_input: the same run_id (same working
    folder and node-session keys), the node that asked, the visit counts already
    consumed, and the composed answers context fed to that node as upstream input."""

    run_id: str
    start_at: str
    visits: dict[str, int]
    upstream: str | None = None


def build_resume_state(manifest: dict, answers: str) -> ResumeState:
    """The ResumeState for re-entering a needs_input run, built from its manifest.
    The caller validates the manifest first (status == "needs_input" and a
    needs_input_node present); this only folds the mechanical parts: max iteration
    per node as the consumed visit counts, and the answers framed against the
    questions the run ended with."""
    visits: dict[str, int] = {}
    for r in manifest.get("runs", []):
        nid, it = r.get("node_id"), r.get("iteration", 1)
        if nid:
            visits[nid] = max(visits.get(nid, 0), int(it))
    questions = manifest.get("final_output") or ""
    return ResumeState(
        run_id=manifest["run_id"],
        start_at=manifest["needs_input_node"],
        visits=visits,
        upstream=(
            "This run previously stopped to ask for more information.\n\n"
            f"--- Your questions were ---\n{questions}\n\n"
            f"--- The user's answers ---\n{answers}\n\nContinue from here."
        ),
    )

# Upper bound on messages carried in the running shared-context buffer. A long
# chain of 'shared' nodes would otherwise grow this without limit and balloon the
# prompt for every later node; keep only the most recent N messages.
_SHARED_CONTEXT_MAX_MESSAGES = 200


class WorkflowConfigError(RuntimeError):
    """The workflow is wired wrong (e.g. a subworkflow node but no subworkflow runner). A
    programmer/config error — it fails fast rather than being swallowed as a run abort."""


class ScriptCancelled(RuntimeError):
    """A script node's subprocess was killed mid-run by a cooperative cancel (as
    opposed to timing out or exiting non-zero). Raised by the script runner and
    carried as a NodeExecutionError's cause so run() can end the run 'cancelled'
    rather than 'aborted'."""


class NodeExecutionError(RuntimeError):
    """A node's agent turn raised. Carries the node identity, iteration and the session
    key under which the node runner persisted the partial conversation — so the engine
    can record an attributable ``node_failed`` NodeRun and name the node in the aborted
    result, and the failed node's session stays navigable. ``exit_code`` is set by the
    script runner for a non-zero subprocess exit (None for a timeout, a spawn error, or
    any agent-node failure) so the node_failed trace row carries it too."""

    def __init__(
        self, node_id: str, iteration: int, session_key: str | None, cause: BaseException,
        *, exit_code: int | None = None,
    ) -> None:
        super().__init__(f"node {node_id!r} (iteration {iteration}) failed: {cause}")
        self.node_id = node_id
        self.iteration = iteration
        self.session_key = session_key
        self.cause = cause
        self.exit_code = exit_code


class WorkflowEngine:
    def __init__(
        self,
        node_runner: NodeRunner,
        *,
        script_runner: NodeRunner | None = None,
        run_id_factory: Callable[[], str] | None = None,
        subworkflow_runner: Callable[..., "WorkflowResult | str"] | None = None,  # (name, task, root_session_key, work_dir=None, parent_run_id=None, progress_emit=None, cancel_check=None, parent_node_id=None) -> child WorkflowResult (a plain str still means "completed with this output")
        workspace: str | None = None,
        pick_runner: Callable[[str, list[str], "str | None"], int] | None = None,
        max_node_visits: int = 1000,
        progress_emit: Callable[[dict], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        prune_keep: int = 20,
    ) -> None:
        self._node_runner = node_runner
        self._script_runner = script_runner
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._subworkflow_runner = subworkflow_runner
        # The real workspace (writing-parallel forks/applies here) and a runner that
        # picks the winning branch for 'choose' reconciliation. Both optional: a
        # read-only engine needs neither.
        self._workspace = workspace
        self._pick_runner = pick_runner
        self._max_node_visits = max_node_visits
        # Optional callback for live per-node progress; called after each node record.
        # Best-effort: exceptions in the callback are silently swallowed so they cannot
        # interrupt a run. None means no progress signalling (CLI/test callers).
        self._progress_emit = progress_emit
        # Optional cooperative-cancel poll, checked at the top of the node walk so a
        # background run can be stopped between nodes. None means the run is never
        # cancelled from outside (CLI/test callers).
        self._cancel_check = cancel_check
        self._prune_keep = prune_keep

    def run(
        self,
        workflow: Workflow,
        task: str,
        *,
        root_session_key: str | None = None,
        input_files: list[str] | None = None,
        output_format: str | None = None,
        resume: ResumeState | None = None,
        work_dir_override: str | None = None,
        parent_run_id: str | None = None,
    ) -> WorkflowResult:
        """Run the workflow. A node-execution failure (provider/MCP/tool error) does not
        propagate — it ends the run as a typed ``aborted`` result carrying the partial
        per-node trace, so the run is still recorded for diagnostics. A wiring/config
        error (a node needing a runner the engine wasn't given) fails fast.

        When the engine has a workspace it owns a live run manifest: a ``running`` record
        written before the walk, updated after each node, and finalized on every exit
        path. Manifest writes are best-effort — a record failure never breaks the run.

        When ``resume`` is given, the walk re-enters at ``resume.start_at`` under the
        same ``run_id`` (so it shares the prior run's working folder and node-session
        keys) with the visit counts already consumed and ``resume.upstream`` as that
        node's upstream input, instead of starting a fresh run at the workflow's start.

        When ``work_dir_override`` is given, the run uses this folder as its working
        directory instead of creating its own folder under the workspace.

        ``parent_run_id`` marks this run as a nested subworkflow invocation, recording
        the caller's run_id in the manifest — ``None`` for a top-level run."""
        run_id = resume.run_id if resume is not None else self._run_id_factory()

        # Pre-flight input validation: check for missing/colliding files and declared-file contracts
        # Must run before prune_runs and _start_manifest so a pre-flight rejection leaves no trace.
        preflight = self._preflight_inputs(workflow, input_files, run_id,
                                           resuming=resume is not None)
        if preflight is not None:
            return preflight

        if self._workspace is not None and work_dir_override is None:
            # Live runs are exempt from pruning by status, not by age: a long node
            # freezes its folder's mtime, and enough concurrent runs starting during
            # it would otherwise evict a mid-flight run's working folder.
            prune_runs(self._workspace, keep=self._prune_keep,
                       protect=run_log.live_run_ids(self._workspace))
        runs: list[NodeRun] = []
        # The run's shared working folder is fixed here (not in the walk) so the
        # manifest can record it from the very first write — an in-flight run's
        # artifacts are then findable by any observer, not only after completion.
        work_dir: str | None = work_dir_override or (
            str(artifact_dir(self._workspace, run_id, "work", None))
            if self._workspace is not None else None
        )
        # The effective root MUST match node_runner's headless rooting: a None calling
        # session means node sessions are rooted under workflow:<run_id>:root, so the
        # manifest records that same key or runs_for_session can't find the run.
        effective_root = root_session_key or f"workflow:{run_id}:root"
        started_at = time.time()
        self._start_manifest(workflow, run_id, effective_root, started_at, task,
                             parent_run_id, work_dir=work_dir)

        def _update() -> None:
            self._update_manifest(workflow, run_id, runs)

        try:
            result = self._walk(
                workflow, self._frame_task(workflow, task, output_format), run_id, runs,
                root_session_key=root_session_key,
                input_files=None if resume else input_files,
                update_manifest=_update,
                start_at=resume.start_at if resume else None,
                initial_visits=dict(resume.visits) if resume else None,
                initial_upstream=resume.upstream if resume else None,
                work_dir=work_dir,
                own_work_dir=work_dir_override is None,
            )
        except WorkflowConfigError as exc:
            # A config/wiring error is fatal and re-raised, but finalize the manifest first
            # so it does not linger as a stale 'running' record — otherwise the crash sweep
            # would later mislabel a deterministic config bug as 'crashed'.
            self._finalize_manifest(
                workflow,
                WorkflowResult(status="aborted", final_output=f"workflow config error: {exc}",
                               runs=runs, run_id=run_id),
                effective_root, started_at, parent_run_id)
            raise
        except NodeExecutionError as exc:
            if isinstance(exc.cause, ScriptCancelled):
                # A running script was killed by a cooperative cancel: end the run
                # 'cancelled' (not 'aborted'), carrying the partial trace the walk
                # already built. The walk's node_failed NodeRun for this node stays
                # as-is (an honest per-node record); only the run-level status changes.
                result = WorkflowResult(
                    status="cancelled", final_output="run cancelled", runs=runs, run_id=run_id,
                )
            else:
                # A named node failure: the walk already appended its node_failed NodeRun.
                # Carry the node identity into the aborted result so the abort names it.
                result = WorkflowResult(
                    status="aborted",
                    final_output=f"workflow aborted: node {exc.node_id!r} (iteration {exc.iteration}) failed: {exc.cause}",
                    runs=runs, run_id=run_id,
                    failed_node=exc.node_id, failed_iteration=exc.iteration,
                )
        except Exception as exc:  # noqa: BLE001 - a node failure becomes a typed aborted result
            result = WorkflowResult(
                status="aborted", final_output=f"workflow error: {exc}",
                runs=runs, run_id=run_id,
            )
        self._finalize_manifest(workflow, result, effective_root, started_at, parent_run_id)
        return result

    def _preflight_inputs(self, workflow, input_files, run_id, *, resuming: bool):
        """Validate the run's inputs before any node (or manifest) exists. Returns a
        terminal WorkflowResult on a problem, else None. Deterministic and LLM-free:
        a missing/colliding input file is the caller's error (aborted, naming it); a
        workflow that declares file input given none ends needs_input immediately so
        the invoking agent asks the user for the file instead of burning node turns."""
        if input_files:
            for p in input_files:
                if not Path(p).is_file():
                    return WorkflowResult(status="aborted", runs=[], run_id=run_id,
                                          final_output=f"input file not found: {p}")
            names = [Path(p).name for p in input_files]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                return WorkflowResult(
                    status="aborted", runs=[], run_id=run_id,
                    final_output=("input files collide on the same name(s): "
                                  f"{', '.join(dupes)} — pass files with distinct names"))
        wants_file = bool((workflow.input or {}).get("file"))
        if wants_file and not input_files and not resuming:
            desc = (workflow.input or {}).get("description") or ""
            hint = f" ({desc})" if desc else ""
            return WorkflowResult(
                status="needs_input", runs=[], run_id=run_id,
                final_output=("This workflow expects one or more input files"
                              f"{hint}. Please provide them (input_files) and run it again."))
        # Missing referenced script files are the author's error: abort before any
        # node runs or any manifest exists, naming the node and file.
        if self._workspace is not None:
            scripts_dir = Path(self._workspace) / "workflows" / "scripts"
            for node in workflow.nodes.values():
                if isinstance(node, ScriptNode) and node.script:
                    if not (scripts_dir / node.script).is_file():
                        return WorkflowResult(
                            status="aborted", runs=[], run_id=run_id,
                            final_output=(f"node {node.id!r}: script file not found: "
                                          f"{node.script} (expected under workflows/scripts/)"))
        # Declared script-node secrets are the author's contract with the store:
        # an unknown name or one whose scope does not allow the 'exec' consumer
        # can never be injected, so abort now — before any node runs or a manifest
        # exists — naming the node and every unresolvable name at once.
        declared = [n for n in workflow.nodes.values()
                    if isinstance(n, ScriptNode) and getattr(n, "secrets", ())]
        if declared:
            from durin.security.secrets import get_secret_store, scope_allows
            entries = get_secret_store().all()
            for node in declared:
                missing = [s for s in node.secrets if s not in entries]
                denied = [s for s in node.secrets
                          if s in entries and not scope_allows(entries[s].scope, "exec")]
                if missing or denied:
                    parts = []
                    if missing:
                        parts.append(f"not in the secret store: {', '.join(missing)}")
                    if denied:
                        parts.append(f"missing the 'exec' scope: {', '.join(denied)}")
                    return WorkflowResult(
                        status="aborted", runs=[], run_id=run_id,
                        final_output=(f"node {node.id!r} declares secrets that cannot "
                                      f"be provided — {'; '.join(parts)}. Store the "
                                      f"secret and grant it the 'exec' scope, or fix "
                                      f"the node's 'secrets' list (the `workflows` "
                                      f"skill documents script-node secrets)."))
        return None

    def _start_manifest(self, workflow, run_id, root_session_key, started_at, task=None,
                        parent_run_id=None, work_dir=None) -> None:
        if self._workspace is None:
            return
        typical = {}
        typical_total = None
        try:
            typical = run_log.typical_node_durations(self._workspace, workflow.name)
            typical_total = run_log.typical_total_duration(self._workspace, workflow.name)
        except Exception:  # noqa: BLE001 - history is a nicety; never block a run
            pass
        try:
            run_log.start_run(self._workspace, workflow.name, run_id,
                              root_session_key=root_session_key, started_at=started_at,
                              task=task, parent_run_id=parent_run_id, work_dir=work_dir,
                              typical_s=typical, typical_total_s=typical_total)
        except Exception:  # noqa: BLE001 - a manifest write must not break the run
            logger.exception("workflow run manifest start failed for {}", workflow.name)

    def _update_manifest(self, workflow, run_id, runs) -> None:
        if self._workspace is None:
            return
        try:
            run_log.update_run(self._workspace, workflow.name, run_id,
                               WorkflowResult(status="running", final_output=None, runs=runs))
        except Exception:  # noqa: BLE001 - a manifest write must not break the run
            logger.exception("workflow run manifest update failed for {}", workflow.name)

    def _finalize_manifest(self, workflow, result, root_session_key, started_at,
                           parent_run_id=None) -> None:
        if self._workspace is None:
            return
        try:
            run_log.finalize_run(self._workspace, workflow.name, result,
                                 root_session_key=root_session_key, started_at=started_at,
                                 finished_at=time.time(), parent_run_id=parent_run_id)
        except Exception:  # noqa: BLE001 - a manifest write must not break the run
            logger.exception("workflow run manifest finalize failed for {}", workflow.name)
        else:
            # Bound manifest growth for this workflow name right after a successful
            # terminal write. Nested runs prune their own (child-name) manifest store —
            # manifests are per workflow name, unlike the shared .workflow folder tree
            # that nested runs must not touch. Best-effort: never break the run.
            try:
                run_log.prune_manifests(self._workspace, workflow.name, keep=self._prune_keep)
            except Exception:  # noqa: BLE001 - pruning must not break the run
                logger.exception("workflow run manifest prune failed for {}", workflow.name)

    @staticmethod
    def _frame_task(workflow: Workflow, task: str, output_format: str | None = None) -> str:
        """Frame the task with the workflow's optional I/O descriptions: the input
        description as a prefix (what the workflow received) and the output description
        as a suffix (what it must ultimately deliver). Both are free-text hints that
        steer the node agents and document the interface — they are not enforced. A
        call-time ``output_format`` (the caller's delivery instruction for THIS run)
        overrides the workflow's default output description. When neither an output
        nor an input applies the task is returned unchanged."""
        def _desc(d: object) -> str | None:
            text = d.get("description") if isinstance(d, dict) else None
            text = str(text).strip() if text else ""
            return text or None
        intro = _desc(workflow.input)
        goal = (output_format or "").strip() or _desc(workflow.output)
        prefix = f"This workflow's input is: {intro}\n\n" if intro else ""
        suffix = f"\n\nThe workflow's final deliverable should be: {goal}" if goal else ""
        # Declared artifacts ride in the framing so every node knows the file
        # contract the run must fulfil (paths relative to the working folder).
        artifacts = (workflow.output or {}).get("artifacts") or []
        if artifacts:
            lines = "\n".join(
                f"  - {a['path']}" + (f" — {a['description']}" if a.get("description") else "")
                for a in artifacts
            )
            suffix += ("\n\nThe run must produce these files in the shared working "
                       f"folder:\n{lines}")
        return f"{prefix}{task}{suffix}" if (prefix or suffix) else task

    def _walk(
        self,
        workflow: Workflow,
        task: str,
        run_id: str,
        runs: list[NodeRun],
        *,
        root_session_key: str | None = None,
        input_files: list[str] | None = None,
        update_manifest: Callable[[], None] | None = None,
        start_at: str | None = None,
        initial_visits: dict[str, int] | None = None,
        initial_upstream: str | None = None,
        work_dir: str | None = None,
        own_work_dir: bool = True,
    ) -> WorkflowResult:
        shared_context: list[dict] = []
        visits: dict[str, int] = dict(initial_visits or {})
        upstream_output: str | None = initial_upstream
        # One shared working folder per run (chosen by run(), which also records it in
        # the manifest): every sequential node reads and writes here, so created/edited
        # files accumulate in one place and each stage sees the prior work
        # (collaboration). Parallel branches fork this and reconcile (see _run_parallel).
        terminal_output_dir: str | None = work_dir
        final_output: str | None = None
        final_output_node: str | None = None
        current: str | None = start_at or workflow.start

        # Seed input_files into the shared working folder so the start node reads them as
        # the run's starting files.
        if input_files and work_dir is not None:
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            for path in input_files:
                shutil.copy(path, Path(work_dir) / Path(path).name)

        while current is not None:
            # Cooperative cancel: a background run asked to stop ends here, between
            # nodes, carrying the partial trace so far. A node already executing
            # finishes first — best-effort, like cancelling a sub-agent.
            if self._cancel_check is not None and self._cancel_check():
                return WorkflowResult(
                    status="cancelled", final_output=final_output, runs=runs,
                    run_id=run_id, final_output_node=final_output_node,
                )
            visits[current] = visits.get(current, 0) + 1
            node = workflow.nodes[current]
            budget = min(getattr(node, "max_visits", None) or workflow.max_visits, self._max_node_visits)
            if visits[current] > budget:
                return WorkflowResult(
                    status="exhausted", final_output=final_output, runs=runs,
                    run_id=run_id, exhausted_node=current, final_output_node=final_output_node,
                )
            iteration = visits[current]

            # Emit a "node started" frame so the caller can show a spinner on
            # the in-flight node before it finishes.  Fires for every node type
            # (work, parallel, subworkflow) so all appear as "running" before they
            # execute.  Prior nodes carry their finished status; the current node
            # appears as "running".  Best-effort only — a crashing emit must never
            # abort the run.
            node_started_at = time.time()
            # Guarded like every other manifest write in this class: without a
            # workspace there is no manifest to mark, and calling through would
            # raise on every node of every workspace-less run — an exception the
            # handler below would then swallow on a purely normal path, hiding
            # any real write failure among the noise.
            if self._workspace is not None:
                try:
                    run_log.mark_node_started(
                        self._workspace, workflow.name, run_id,
                        node_id=node.id, label=node_label(node), started_at=node_started_at,
                    )
                except Exception:  # noqa: BLE001 - observability write; never break the run
                    logger.exception("workflow node start marker failed for {}", workflow.name)

            # The shared working folder is one folder for every sequential node, so
            # nothing on disk records which node wrote what — a before/after listing
            # around this node's turn is the only way to attribute a file to it.
            # Best-effort like the rest of this block: an unreadable folder yields an
            # empty set rather than raising, so a snapshot failure never breaks the run.
            def _work_snapshot() -> set[str]:
                if work_dir is None:
                    return set()
                root = Path(work_dir)
                try:
                    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
                except OSError:
                    return set()

            before_files = _work_snapshot()

            if self._progress_emit is not None:
                from durin.workflow.progress import finished_frames, pending_frames, running_frame

                started = finished_frames(workflow, runs)
                started.append(running_frame(
                    node, iteration=iteration,
                    budget=budget if isinstance(node, (WorkNode, ScriptNode)) else None,
                    started_at=node_started_at,
                ))
                started.extend(pending_frames(workflow, node.id,
                                             [r.node_id for r in runs]))
                try:
                    self._progress_emit({"run_id": run_id, "nodes": started, "done": False})
                except Exception:  # noqa: BLE001 - best-effort
                    pass

            # What the in-flight node is doing right now, reported from inside its
            # turn. The node hands over raw state ({"round", "activity", "max_rounds"})
            # and the engine re-emits a whole frame set, because only the engine knows
            # the other nodes — a bare fragment could not be merged into a node list.
            # `_node`, `_iter`, `_budget`, `_started` and `_activity` are pinned as
            # default arguments so this closure keeps this visit's values rather than
            # the enclosing loop's — `node`, `iteration`, `budget`, `node_started_at`
            # and `node_activity` are all rebound on the next visit. That rebinding
            # would be able to corrupt an in-flight closure if this callback could
            # still be invoked once the loop has moved on, but it can't: it is only
            # ever called synchronously on this walk thread, inside the `runner(req)`
            # call below, and must stay synchronous and never await there or the node
            # deadlocks — the same walk-thread-only invariant that makes the pinning
            # sufficient.
            node_activity: dict = {"round": None, "activity": None, "max_rounds": None}

            def _node_progress(update: dict, _node=node, _iter=iteration,
                               _budget=budget, _started=node_started_at,
                               _activity=node_activity) -> None:
                _activity.update(update)
                if self._progress_emit is None:
                    return
                from durin.workflow.progress import finished_frames, pending_frames, running_frame

                frames = finished_frames(workflow, runs)
                frames.append(running_frame(
                    _node, iteration=_iter,
                    budget=_budget if isinstance(_node, (WorkNode, ScriptNode)) else None,
                    started_at=_started,
                    activity=_activity["activity"],
                    round_=_activity["round"],
                    max_rounds=_activity["max_rounds"],
                ))
                frames.extend(pending_frames(workflow, _node.id,
                                             [r.node_id for r in runs]))
                try:
                    self._progress_emit({"run_id": run_id, "nodes": frames, "done": False})
                except Exception:  # noqa: BLE001 - best-effort
                    logger.opt(exception=True).debug("workflow node progress re-emit failed (suppressed)")

            if isinstance(node, (WorkNode, ScriptNode)):
                if isinstance(node, ScriptNode) and self._script_runner is None:
                    raise WorkflowConfigError(
                        f"node {node.id!r} is a script node but the engine has no script_runner"
                    )
                # A node with file tools works in the run's shared working folder; a
                # no-tools node does no file I/O and gets none. A script node always
                # gets the working folder (its whole point is deterministic file work).
                out_dir: str | None = (
                    work_dir if (isinstance(node, ScriptNode) or node.tools == "default") else None
                )

                fail_would_exhaust = False
                if node.cases is None and node.on_fail is not None:
                    t = workflow.nodes.get(node.on_fail)
                    if t is not None:
                        t_budget = min(
                            getattr(t, "max_visits", None) or workflow.max_visits,
                            self._max_node_visits,
                        )
                        fail_would_exhaust = visits.get(node.on_fail, 0) >= t_budget

                req = NodeRunRequest(
                    node=node,
                    task=task,
                    upstream_output=upstream_output,
                    # 'own' nodes are isolated from the shared buffer; 'shared'
                    # nodes read it (a copy, so the runner can't mutate ours). A
                    # script node has no context field — it never reads the buffer.
                    shared_context=list(shared_context) if (isinstance(node, WorkNode) and node.context == "shared") else [],
                    run_id=run_id,
                    iteration=iteration,
                    root_session_key=root_session_key,
                    output_dir=out_dir,
                    budget=budget,
                    fail_would_exhaust=fail_would_exhaust,
                    # Only a script node gets the poll hook — an agent node's
                    # cancellation stays between-nodes-only (unchanged).
                    cancel_check=self._cancel_check if isinstance(node, ScriptNode) else None,
                    progress=_node_progress,
                )

                # Run a full agent turn; for a multi-way node the verdict is a matched
                # case label; for binary routing it is PASS/FAIL from the first non-empty
                # line; for a linear node there is no verdict.
                node_t0 = time.monotonic()
                try:
                    runner = self._script_runner if isinstance(node, ScriptNode) else self._node_runner
                    resp = runner(req)
                except NodeExecutionError as exc:
                    # The node's turn raised: record an attributable node_failed run
                    # (with the persisted session key) so the manifest captures it,
                    # then re-raise to abort the walk — run() names the node.
                    runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                        output="", session_key=exc.session_key,
                                        budget=budget,
                                        status="node_failed", error=str(exc.cause),
                                        exit_code=getattr(exc, "exit_code", None),
                                        duration_s=round(time.monotonic() - node_t0, 3)))
                    runs[-1].artifacts = sorted(_work_snapshot() - before_files)[:20]
                    if update_manifest is not None:
                        update_manifest()
                    raise
                output = resp.output
                route_label = getattr(resp, "route_label", None)
                if node.cases is not None:
                    # Multi-way: label matching replaces pass/fail.
                    passed = None
                elif node.routes:
                    passed = (route_label == "PASS") if route_label is not None else parse_verdict(output)
                else:
                    passed = None
                runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                    output=output, session_key=resp.session_key,
                                    passed=passed, budget=budget,
                                    status="persist_failed" if resp.persist_failed else "ok",
                                    exit_code=getattr(resp, "exit_code", None),
                                    duration_s=round(time.monotonic() - node_t0, 3)))
                runs[-1].artifacts = sorted(_work_snapshot() - before_files)[:20]
                if isinstance(node, WorkNode) and node.context == "shared":
                    shared_context.extend(resp.messages)
                    if len(shared_context) > _SHARED_CONTEXT_MAX_MESSAGES:
                        # Keep only the most recent messages so a long shared chain
                        # cannot grow the buffer (and every later node's prompt) unboundedly.
                        del shared_context[:-_SHARED_CONTEXT_MAX_MESSAGES]

                if node.cases is not None:
                    # Multi-way routing: match the agent's output against declared labels.
                    _UNSET = object()
                    label = route_label if route_label in node.cases else parse_label(output, node.cases)
                    if label is not None:
                        target = node.cases[label]
                    else:
                        # No label matched — fall back to "default" case, or abort.
                        target = node.cases.get("default", _UNSET)
                        if target is _UNSET:
                            expected = sorted(node.cases)
                            abort_msg = (
                                f"node {node.id!r}: agent output did not match any expected label "
                                f"({', '.join(expected)})"
                            )
                            return WorkflowResult(
                                status="aborted",
                                final_output=abort_msg,
                                runs=runs,
                                run_id=run_id,
                            )
                        label = "default"
                    # Record the matched label in the NodeRun trace.
                    runs[-1].route_label = label
                    if target == NEEDS_INPUT_TARGET:
                        # The gate routed to the reserved needs-input terminal: end the run
                        # asking the caller for more information. The node's output carries
                        # the questions; the invoking agent (which owns the user channel)
                        # asks the user and re-runs the workflow with the answers. The
                        # routing label is transport metadata, not part of the question —
                        # strip it like the terminal-completion path does (falling back to
                        # the raw output if the label was the only line).
                        return WorkflowResult(
                            status="needs_input",
                            final_output=strip_label_line(output, node.cases) or output,
                            runs=runs, run_id=run_id, needs_input_node=node.id,
                            final_output_node=node.id,
                        )
                    if target is not None:
                        # Thread this node's output as neutral context before routing to
                        # the target. Unlike the binary fail-edge (which always carries
                        # remediation feedback), a cases route may be a forward dispatch,
                        # not a loop-back, so the framing is intentionally neutral.
                        prior = upstream_output or ""
                        upstream_output = (
                            f"{prior}\n\nContext from {node.id!r}:\n{output}"
                        )
                    if target is None:
                        # The run ends at this multi-way node: whatever it produced
                        # besides the label line is its real contribution.
                        residue = strip_label_line(output, node.cases)
                        if residue:
                            final_output = residue
                            final_output_node = node.id
                    current = target
                elif node.routes:
                    if not passed:
                        # Thread reviewer feedback into upstream so the producer sees it.
                        prior = upstream_output or ""
                        upstream_output = (
                            f"{prior}\n\nReviewer feedback (address this):\n{output}"
                        )
                    current = node.on_pass if passed else node.on_fail
                    if current is None:
                        residue = strip_verdict_line(output)
                        if residue:
                            final_output = residue
                            final_output_node = node.id
                else:
                    upstream_output = output
                    final_output = output
                    final_output_node = node.id
                    current = node.next

            elif isinstance(node, SubworkflowNode):
                if self._subworkflow_runner is None:
                    raise WorkflowConfigError(
                        f"node {node.id!r} is a subworkflow but the engine has no subworkflow_runner"
                    )
                sub_t0 = time.monotonic()
                outcome = self._subworkflow_runner(
                    node.workflow, upstream_output or task, root_session_key,
                    work_dir=work_dir, parent_run_id=run_id,
                    progress_emit=self._progress_emit, cancel_check=self._cancel_check,
                    parent_node_id=node.id,
                )
                sub_duration = round(time.monotonic() - sub_t0, 3)
                # The runner returns the child's WorkflowResult so its terminal status
                # reaches this run. A plain string (legacy runner or test double) keeps
                # meaning "completed with this output".
                if isinstance(outcome, str):
                    child_status, output = "completed", outcome
                else:
                    child_status, output = outcome.status, (outcome.final_output or "")
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=output,
                                    duration_s=sub_duration))
                # The nested run shares this same work_dir (see SubworkflowRunner's
                # work_dir_override) and runs synchronously on this thread, so the
                # diff here is deterministic and credits the sub-workflow as a whole —
                # its own manifest separately attributes files to its inner nodes.
                runs[-1].artifacts = sorted(_work_snapshot() - before_files)[:20]
                if child_status == "needs_input":
                    # The child stopped to ask: pause THIS run the same way, keyed to
                    # this node — resume re-enters here and re-runs the child with the
                    # framed answers as its task (its prior files persist in the shared
                    # working folder).
                    return WorkflowResult(
                        status="needs_input", final_output=output, runs=runs,
                        run_id=run_id, needs_input_node=node.id,
                        final_output_node=node.id,
                    )
                if child_status == "cancelled":
                    return WorkflowResult(
                        status="cancelled", final_output=output or final_output,
                        runs=runs, run_id=run_id, final_output_node=node.id,
                    )
                if child_status != "completed":   # aborted / exhausted / anything else
                    msg = f"sub-workflow {node.workflow!r} {child_status}"
                    if output:
                        msg = f"{msg}: {output}"
                    runs[-1].status = "node_failed"
                    runs[-1].error = msg
                    return WorkflowResult(
                        status="aborted", final_output=msg, runs=runs, run_id=run_id,
                        failed_node=node.id, failed_iteration=iteration,
                        final_output_node=node.id,
                    )
                upstream_output = output
                final_output = output
                final_output_node = node.id
                current = node.next
                # A cancel can land while the child ran without the child noticing
                # (e.g. between its last node and its return). Ordinarily the next
                # loop iteration's cancel_check at the top would catch it — but when
                # this was the last node, there is no next iteration, and the walk
                # would otherwise fall through to the completed result below,
                # misreporting a cancelled run as completed. Re-consult here so this
                # path agrees with the top-of-loop check above on status and shape.
                if self._cancel_check is not None and self._cancel_check():
                    return WorkflowResult(
                        status="cancelled", final_output=final_output, runs=runs,
                        run_id=run_id, final_output_node=final_output_node,
                    )

            elif isinstance(node, ParallelNode):
                if node.worker is not None:
                    merged, abort = self._run_dynamic_parallel(
                        workflow, node, task, run_id, iteration, root_session_key,
                        upstream_output, runs, work_dir=work_dir)
                elif node.branches_from is not None:
                    resolved, abort = self._resolve_runtime_branches(
                        workflow, node, runs, upstream_output)
                    # An empty resolved list is a legitimate "nothing applies" pass:
                    # record the node with empty output and continue to `next`.
                    merged = ""
                    if abort is None and resolved:
                        merged, abort = self._run_parallel(
                            workflow, node, task, run_id, iteration, root_session_key,
                            upstream_output, runs, work_dir=work_dir,
                            branch_ids=resolved)
                else:
                    merged, abort = self._run_parallel(
                        workflow, node, task, run_id, iteration, root_session_key,
                        upstream_output, runs, work_dir=work_dir)
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=merged))
                # Branches/workers run concurrently in their own (possibly forked,
                # possibly shared) folders, so a per-branch diff here would be racy or
                # meaningless; the parallel node's own aggregate entry is diffed once
                # its branches have finished and reconciled, sequentially on this thread.
                runs[-1].artifacts = sorted(_work_snapshot() - before_files)[:20]
                if abort is not None:
                    return WorkflowResult(
                        status="aborted", final_output=abort, runs=runs, run_id=run_id
                    )
                upstream_output = merged
                final_output = merged
                final_output_node = node.id
                current = node.next

            # The node's record(s) are now appended — refresh the live manifest so an
            # in-flight run is observable before the next node starts.
            if update_manifest is not None:
                update_manifest()
            if work_dir is not None and own_work_dir:
                try:
                    os.utime(Path(work_dir).parent, None)   # keep .workflow/<run_id>/ recent while running
                except OSError:
                    pass
            if self._progress_emit is not None:
                from durin.workflow.progress import finished_frames

                nodes = finished_frames(workflow, runs)
                try:
                    self._progress_emit({"run_id": run_id, "nodes": nodes, "done": False})
                except Exception:  # noqa: BLE001 - progress is best-effort; never break the run
                    pass

        output_files: list[str] = []
        if terminal_output_dir is not None:
            root_dir = Path(terminal_output_dir)
            output_files = sorted(
                str(p.relative_to(root_dir)) for p in root_dir.rglob("*") if p.is_file()
            )
        # The declared file contract (output.artifacts): report promised paths the
        # completed run did not produce. A warning, never a failure — the caller
        # (an orchestrating agent or the next stage) learns immediately which file
        # is absent instead of failing confusingly downstream.
        declared = [a["path"] for a in (workflow.output or {}).get("artifacts") or []]
        produced = set(output_files)
        missing = [p for p in declared if p not in produced]
        return WorkflowResult(
            status="completed", final_output=final_output, runs=runs, run_id=run_id,
            output_dir=terminal_output_dir, output_files=output_files,
            final_output_node=final_output_node, missing_artifacts=missing,
        )

    def _run_one_branch(self, branch, task, upstream, run_id, iteration, root_key,
                        workspace_override, out_dir=None):
        # Same kind dispatch as the linear walk: a script branch runs the script
        # contract (stdin = the parallel's upstream text, cwd = out_dir) beside
        # the agent branches.
        is_script = isinstance(branch, ScriptNode)
        if is_script and self._script_runner is None:
            raise WorkflowConfigError(
                f"branch {branch.id!r} is a script node but the engine has no script_runner"
            )
        runner = self._script_runner if is_script else self._node_runner
        return runner(NodeRunRequest(
            node=branch, task=task, upstream_output=upstream, shared_context=[],
            run_id=run_id, iteration=iteration, root_session_key=root_key,
            workspace_override=workspace_override,
            output_dir=out_dir if (is_script or getattr(branch, "tools", "none") == "default") else None,
        ))

    @staticmethod
    def _record_branches(runs, results, iteration):
        """Append a per-branch NodeRun (carrying its session_key, branch_id and failure
        status) for each ``(branch_id, output, session_key, error, persist_failed,
        duration, exit_code)`` tuple — so static-parallel branch sessions stay
        attributable in the run trace, mirroring the dynamic fan-out worker records.
        ``error`` is None for a branch that completed; ``persist_failed`` marks a branch
        that ran but whose session save raised; ``exit_code`` is set for script branches
        (None for agent branches), matching the linear script contract."""
        for bid, out, session_key, error, persist_failed, duration, exit_code in results:
            runs.append(NodeRun(node_id=bid, iteration=iteration, output=out,
                                session_key=session_key, branch_id=bid,
                                status=("node_failed" if error else
                                        "persist_failed" if persist_failed else "ok"),
                                error=error, duration_s=duration, exit_code=exit_code))

    @staticmethod
    def _parse_subtasks(text: str) -> list[str]:
        """Parse a runtime list of subtasks from a node's output text.

        The list is expected as a JSON array, but a model often wraps it in prose
        and/or a markdown code fence (e.g. "These are the two seams: ```json [...] ```").
        So extract the array even when embedded: try a fenced code block's body, then
        the largest ``[ ... ]`` span, then the whole text — the first that parses as a
        JSON list wins. Only if none do does it fall back to non-empty lines (a last
        resort that, on prose, would otherwise split every sentence into a bogus
        subtask). Capped at 50 items to bound blast radius on pathological output.
        """
        import json
        import re
        candidate = text.strip()
        # Most-specific candidate substrings first: a fenced block's body, then the
        # widest bracketed span, then the whole text.
        attempts: list[str] = []
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate)
        if fence:
            attempts.append(fence.group(1).strip())
        arr = re.search(r"\[[\s\S]*\]", candidate)
        if arr:
            attempts.append(arr.group(0))
        attempts.append(candidate)
        for block in attempts:
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            # A parsed list wins — even if empty: an empty array means "no subtasks",
            # which must not fall through to the line-split fallback.
            if isinstance(parsed, list):
                return [str(x) for x in parsed][:50]
        # Last resort: a bare newline-separated list (no JSON array found at all).
        items = [line.strip() for line in candidate.splitlines() if line.strip()]
        return items[:50]

    def _run_dynamic_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream, runs, work_dir=None):
        """Run a dynamic parallel node: parse a runtime list and run the worker once per item.

        Returns ``(merged_output, abort_message)``.  Each worker run is appended to
        ``runs`` so the trace shows individual worker outputs.
        """
        # Resolve the list source: prefer the most recent recorded output of the
        # list_from node (it may not be the immediate predecessor); fall back to
        # upstream_output for the common case where it is.
        list_text: str | None = None
        for recorded in reversed(runs):
            if recorded.node_id == node.list_from:
                list_text = recorded.output
                break
        if list_text is None:
            list_text = upstream or ""

        subtasks = self._parse_subtasks(list_text)
        if not subtasks:
            return "", None

        worker_node = workflow.nodes[node.worker]
        workers = max(1, min(len(subtasks), node.max_concurrency))

        def _run_worker(args):
            # A worker that raises must not take down the whole fan-out: catch it and
            # return a tagged failure so survivors still complete (per-future isolation).
            # Each worker times itself: concurrent units cannot share a walk-level clock.
            idx, subtask = args
            t0 = time.monotonic()
            try:
                resp = self._node_runner(NodeRunRequest(
                    node=worker_node,
                    task=subtask,
                    upstream_output=subtask,
                    shared_context=[],
                    run_id=run_id,
                    iteration=iteration,
                    root_session_key=root_key,
                    worker_index=idx,
                    output_dir=work_dir if worker_node.tools == "default" else None,
                ))
                return idx, resp.output, resp.session_key, None, resp.persist_failed, round(time.monotonic() - t0, 3)
            except NodeExecutionError as exc:
                return idx, "", exc.session_key, str(exc.cause), False, round(time.monotonic() - t0, 3)
            except Exception as exc:  # noqa: BLE001 - isolate a single worker's failure
                return idx, "", None, str(exc), False, round(time.monotonic() - t0, 3)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = sorted(ex.map(_run_worker, enumerate(subtasks)))

        for idx, out, session_key, error, persist_failed, duration in results:
            runs.append(NodeRun(node_id=node.worker, iteration=iteration, output=out,
                                session_key=session_key, worker_index=idx,
                                status=("node_failed" if error else
                                        "persist_failed" if persist_failed else "ok"),
                                error=error, duration_s=duration))

        if all(error for _idx, _out, _key, error, _pf, _d in results):
            return "", f"parallel node {node.id!r}: every worker failed"

        merged = "\n\n".join(
            f"[{idx}] {out}" if error is None else f"[{idx}] FAILED: {error}"
            for idx, out, _key, error, _pf, _d in results
        )
        return merged, None

    @staticmethod
    def _parse_branch_ids(text: str) -> list[str] | None:
        """Parse a runtime branch-id list from the branches_from node's output.

        Two accepted forms, both deterministic-script friendly: a JSON array of id
        strings anywhere in the text (fenced, bracketed span, or the whole text), or
        — when no JSON array parses — the LAST non-empty line as comma-separated ids
        (the same last-line contract `cases` routing uses). Returns None when the
        text yields nothing to even attempt (empty), else the parsed list — which
        may legitimately be empty (an explicit ``[]`` means "no branches apply").
        """
        import json
        import re
        candidate = text.strip()
        if not candidate:
            return None
        attempts: list[str] = []
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate)
        if fence:
            attempts.append(fence.group(1).strip())
        arr = re.search(r"\[[\s\S]*\]", candidate)
        if arr:
            attempts.append(arr.group(0))
        attempts.append(candidate)
        for block in attempts:
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()][:50]
        last_line = candidate.splitlines()[-1]
        return [tok.strip().strip("'\"") for tok in last_line.split(",") if tok.strip()][:50]

    def _resolve_runtime_branches(self, workflow, node, runs, upstream):
        """Resolve a ``branches_from`` parallel node's branch ids for this pass.

        Reads the most recent recorded output of the source node (falling back to the
        upstream edge text), parses the id list, dedupes preserving order, and holds
        every id to the same rules as a static branch: it must name a declared
        WorkNode without a persistent session. An id that fails is an authoring bug
        in the emitting node — abort naming it rather than guessing.
        Returns ``(ids, abort_message)``.
        """
        source_text: str | None = None
        for recorded in reversed(runs):
            if recorded.node_id == node.branches_from:
                source_text = recorded.output
                break
        if source_text is None:
            source_text = upstream or ""

        parsed = self._parse_branch_ids(source_text)
        ids: list[str] = []
        for bid in parsed or []:
            if bid not in ids:
                ids.append(bid)
        for bid in ids:
            target = workflow.nodes.get(bid)
            if not isinstance(target, (WorkNode, ScriptNode)):
                return [], (f"parallel node {node.id!r}: branches_from resolved unknown or "
                            f"non-runnable branch {bid!r} (from node {node.branches_from!r}; "
                            "a branch must be a work or script node)")
            if isinstance(target, WorkNode) and target.session == "persistent":
                return [], (f"parallel node {node.id!r}: branch {bid!r} cannot use "
                            "session='persistent' (concurrent units have per-unit sessions)")
        return ids, None

    def _run_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream, runs, work_dir=None, branch_ids=None):
        """Run a parallel node's branches concurrently and reconcile their writes.

        Returns ``(merged_output, abort_message)``; ``abort_message`` is None on
        success or a string when the run must abort (e.g. a union conflict, or a
        misconfiguration). 'read' branches run against the shared workspace (no writes
        applied) but are still handed the run's shared working folder; 'choose'/'union'
        branches each run against a private copy of the workspace WITH the working
        folder's current files seeded in, and their folder writes reconcile back
        (choose/union) exactly like their other workspace writes. Each branch is
        appended to ``runs`` so its session stays attributable in the trace.

        Per-branch progress is emitted via ``_progress_emit`` as branches start and
        finish.  The parallel node's entry in the frame carries a ``"branches"`` list
        with each branch's current status ("running", "done", or "failed").  Emits are
        best-effort — a crashing emit never alters the run or its result.
        """
        import threading

        # branch_ids overrides the static list for a branches_from node — the branch
        # set was resolved (and validated) from the source node's output this pass.
        branches = tuple(branch_ids) if branch_ids is not None else node.branches
        workers = max(1, min(len(branches), node.max_concurrency))
        # Writing branches (choose/union) fork the run's SHARED WORKING FOLDER, not
        # the durin workspace: the folder is where sequential nodes collaborate and
        # is therefore the write surface a branch legitimately owns. Forking the
        # whole workspace copied sessions/, memory/ and every other state dir per
        # branch — ruinous on a real workspace, and a concurrent gateway write there
        # could even read as a phantom branch change in the diff.
        fork_root = work_dir if work_dir else self._workspace

        # Shared branch-status map, guarded by a lock because branches run in
        # ThreadPoolExecutor workers concurrently.
        branch_status: dict[str, str] = {bid: "running" for bid in branches}
        _branch_lock = threading.Lock()

        def _emit_branches() -> None:
            """Emit a progress frame with the current per-branch statuses.  The frame
            carries prior finished nodes plus the parallel node itself (status 'running')
            annotated with a snapshot of each branch's live status."""
            if self._progress_emit is None:
                return
            from durin.workflow.progress import finished_frames

            prior = finished_frames(workflow, runs)
            with _branch_lock:
                branch_list = [
                    {"id": bid, "label": node_label(workflow.nodes[bid]) if bid in workflow.nodes else bid, "status": st}
                    for bid, st in branch_status.items()
                ]
            prior.append({"id": node.id, "label": node_label(node), "status": "running", "route_label": None,
                          "branches": branch_list})
            try:
                self._progress_emit({"run_id": run_id, "nodes": prior, "done": False})
            except Exception:  # noqa: BLE001 - best-effort; must never alter the run
                pass

        # Emit once with all branches "running" so the UI shows them immediately.
        _emit_branches()

        if node.reconcile == "read":
            def _run(bid):
                # A branch that raises must not take down the others: catch it and tag the
                # failure so survivors still complete (per-branch isolation, like fan-out).
                # Each branch times itself: concurrent units cannot share a walk-level clock.
                t0 = time.monotonic()
                try:
                    resp = self._run_one_branch(
                        workflow.nodes[bid], task, upstream, run_id, iteration, root_key,
                        None, out_dir=work_dir)
                    with _branch_lock:
                        branch_status[bid] = "done"
                    _emit_branches()
                    return (bid, resp.output, resp.session_key, None, resp.persist_failed,
                            round(time.monotonic() - t0, 3), getattr(resp, "exit_code", None))
                except NodeExecutionError as exc:
                    with _branch_lock:
                        branch_status[bid] = "failed"
                    _emit_branches()
                    return (bid, "", exc.session_key, str(exc.cause), False,
                            round(time.monotonic() - t0, 3), getattr(exc, "exit_code", None))
                except Exception as exc:  # noqa: BLE001 - isolate a single branch's failure
                    with _branch_lock:
                        branch_status[bid] = "failed"
                    _emit_branches()
                    return bid, "", None, str(exc), False, round(time.monotonic() - t0, 3), None
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            self._record_branches(runs, results, iteration)
            if all(error for _bid, _out, _key, error, _pf, _d, _ec in results):
                return "", f"parallel node {node.id!r}: every branch failed"
            return "\n\n".join(
                f"[{bid}]\n{out}" if error is None else f"[{bid}] FAILED: {error}"
                for bid, out, _key, error, _pf, _d, _ec in results), None

        if self._workspace is None:
            return "", f"parallel node {node.id!r}: reconcile={node.reconcile!r} needs a workspace"
        Path(fork_root).mkdir(parents=True, exist_ok=True)   # a fresh run's folder may not exist yet

        base = workspace_fork.snapshot(fork_root)
        forks: list = []

        def _run(bid):
            # Each branch gets a private copy of the working folder: its file tools
            # anchor there (workspace override) and its writes diff against the base.
            fork_dir = workspace_fork.fork(fork_root)
            forks.append(fork_dir)
            try:
                resp = self._run_one_branch(
                    workflow.nodes[bid], task, upstream, run_id, iteration, root_key,
                    str(fork_dir), out_dir=str(fork_dir))
                with _branch_lock:
                    branch_status[bid] = "done"
                _emit_branches()
                return (bid, resp.output, resp.session_key,
                        getattr(resp, "exit_code", None), workspace_fork.diff(base, fork_dir))
            except Exception:
                with _branch_lock:
                    branch_status[bid] = "failed"
                _emit_branches()
                raise

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            self._record_branches(
                runs, [(bid, out, key, None, False, None, ec) for bid, out, key, ec, _ in results],
                iteration)
            if node.reconcile == "choose":
                if self._pick_runner is None:
                    return "", f"parallel node {node.id!r}: 'choose' needs a pick_runner"
                idx = self._pick_runner(node.criteria, [out for _, out, _, _, _ in results], node.judge_model)
                idx = idx if isinstance(idx, int) and 0 <= idx < len(results) else 0
                bid, out, _key, _ec, cs = results[idx]
                workspace_fork.apply(cs, fork_root)
                return f"[chosen: {bid}]\n{out}", None
            # union: apply every branch unless two touched the same path
            changesets = [cs for _, _, _, _, cs in results]
            conflict = workspace_fork.conflicts(changesets)
            if conflict:
                return "", f"parallel node {node.id!r}: union conflict on {sorted(conflict)}"
            for cs in changesets:
                workspace_fork.apply(cs, fork_root)
            return "\n\n".join(f"[{bid}]\n{out}" for bid, out, _, _, _ in results), None
        finally:
            for fork_dir in forks:
                workspace_fork.cleanup(fork_dir)
