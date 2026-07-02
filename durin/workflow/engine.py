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
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

from durin.workflow import run_log, workspace_fork
from durin.workflow.artifacts import artifact_dir, prune_runs
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import NEEDS_INPUT_TARGET, ParallelNode, SubworkflowNode, WorkNode, Workflow, node_label
from durin.workflow.verdict import parse_label, parse_verdict, strip_label_line, strip_verdict_line


@dataclass
class NodeRunRequest:
    node: WorkNode
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


class NodeExecutionError(RuntimeError):
    """A node's agent turn raised. Carries the node identity, iteration and the session
    key under which the node runner persisted the partial conversation — so the engine
    can record an attributable ``node_failed`` NodeRun and name the node in the aborted
    result, and the failed node's session stays navigable."""

    def __init__(self, node_id: str, iteration: int, session_key: str | None, cause: BaseException) -> None:
        super().__init__(f"node {node_id!r} (iteration {iteration}) failed: {cause}")
        self.node_id = node_id
        self.iteration = iteration
        self.session_key = session_key
        self.cause = cause


class WorkflowEngine:
    def __init__(
        self,
        node_runner: NodeRunner,
        *,
        run_id_factory: Callable[[], str] | None = None,
        subworkflow_runner: Callable[..., str] | None = None,
        workspace: str | None = None,
        pick_runner: Callable[[str, list[str], "str | None"], int] | None = None,
        max_node_visits: int = 1000,
        progress_emit: Callable[[dict], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        prune_keep: int = 20,
    ) -> None:
        self._node_runner = node_runner
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
        node's upstream input, instead of starting a fresh run at the workflow's start."""
        run_id = resume.run_id if resume is not None else self._run_id_factory()

        # Pre-flight input validation: check for missing/colliding files and declared-file contracts
        # Must run before prune_runs and _start_manifest so a pre-flight rejection leaves no trace.
        preflight = self._preflight_inputs(workflow, input_files, run_id,
                                           resuming=resume is not None)
        if preflight is not None:
            return preflight

        if self._workspace is not None:
            prune_runs(self._workspace, keep=self._prune_keep)
        runs: list[NodeRun] = []
        # The effective root MUST match node_runner's headless rooting: a None calling
        # session means node sessions are rooted under workflow:<run_id>:root, so the
        # manifest records that same key or runs_for_session can't find the run.
        effective_root = root_session_key or f"workflow:{run_id}:root"
        started_at = time.time()
        self._start_manifest(workflow, run_id, effective_root, started_at, task)

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
            )
        except WorkflowConfigError as exc:
            # A config/wiring error is fatal and re-raised, but finalize the manifest first
            # so it does not linger as a stale 'running' record — otherwise the crash sweep
            # would later mislabel a deterministic config bug as 'crashed'.
            self._finalize_manifest(
                workflow,
                WorkflowResult(status="aborted", final_output=f"workflow config error: {exc}",
                               runs=runs, run_id=run_id),
                effective_root, started_at)
            raise
        except NodeExecutionError as exc:
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
        self._finalize_manifest(workflow, result, effective_root, started_at)
        return result

    @staticmethod
    def _preflight_inputs(workflow, input_files, run_id, *, resuming: bool):
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
        return None

    def _start_manifest(self, workflow, run_id, root_session_key, started_at, task=None) -> None:
        if self._workspace is None:
            return
        try:
            run_log.start_run(self._workspace, workflow.name, run_id,
                              root_session_key=root_session_key, started_at=started_at,
                              task=task)
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

    def _finalize_manifest(self, workflow, result, root_session_key, started_at) -> None:
        if self._workspace is None:
            return
        try:
            run_log.finalize_run(self._workspace, workflow.name, result,
                                 root_session_key=root_session_key, started_at=started_at,
                                 finished_at=time.time())
        except Exception:  # noqa: BLE001 - a manifest write must not break the run
            logger.exception("workflow run manifest finalize failed for {}", workflow.name)

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
    ) -> WorkflowResult:
        shared_context: list[dict] = []
        visits: dict[str, int] = dict(initial_visits or {})
        upstream_output: str | None = initial_upstream
        # One shared working folder per run: every sequential node reads and writes here,
        # so created/edited files accumulate in one place and each stage sees the prior
        # work (collaboration). Parallel branches fork this and reconcile (see _run_parallel).
        work_dir: str | None = (
            str(artifact_dir(self._workspace, run_id, "work", None))
            if self._workspace is not None else None
        )
        terminal_output_dir: str | None = work_dir
        final_output: str | None = None
        current: str | None = start_at or workflow.start

        # Seed input_files into the shared working folder so the start node reads them as
        # the run's starting files.
        if input_files and work_dir is not None:
            for path in input_files:
                shutil.copy(path, Path(work_dir) / Path(path).name)

        while current is not None:
            # Cooperative cancel: a background run asked to stop ends here, between
            # nodes, carrying the partial trace so far. A node already executing
            # finishes first — best-effort, like cancelling a sub-agent.
            if self._cancel_check is not None and self._cancel_check():
                return WorkflowResult(
                    status="cancelled", final_output=final_output, runs=runs,
                    run_id=run_id,
                )
            visits[current] = visits.get(current, 0) + 1
            node = workflow.nodes[current]
            budget = min(getattr(node, "max_visits", None) or workflow.max_visits, self._max_node_visits)
            if visits[current] > budget:
                return WorkflowResult(
                    status="exhausted", final_output=final_output, runs=runs,
                    run_id=run_id, exhausted_node=current,
                )
            iteration = visits[current]

            # Emit a "node started" frame so the caller can show a spinner on
            # the in-flight node before it finishes.  Fires for every node type
            # (work, parallel, subworkflow) so all appear as "running" before they
            # execute.  Prior nodes carry their finished status; the current node
            # appears as "running".  Best-effort only — a crashing emit must never
            # abort the run.
            if self._progress_emit is not None:
                started = [
                    {"id": r.node_id,
                     "label": node_label(workflow.nodes[r.node_id]) if r.node_id in workflow.nodes else r.node_id,
                     "status": ("failed" if r.status in ("node_failed", "persist_failed") else "done"),
                     "route_label": r.route_label}
                    for r in runs
                ]
                started.append({
                    "id": node.id,
                    "label": node_label(node),
                    "status": "running",
                    "route_label": None,
                })
                try:
                    self._progress_emit({"run_id": run_id, "nodes": started, "done": False})
                except Exception:  # noqa: BLE001 - best-effort
                    pass

            if isinstance(node, WorkNode):
                # A node with file tools works in the run's shared working folder; a
                # no-tools node does no file I/O and gets none.
                out_dir: str | None = work_dir if node.tools == "default" else None

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
                    # nodes read it (a copy, so the runner can't mutate ours).
                    shared_context=list(shared_context) if node.context == "shared" else [],
                    run_id=run_id,
                    iteration=iteration,
                    root_session_key=root_session_key,
                    output_dir=out_dir,
                    budget=budget,
                    fail_would_exhaust=fail_would_exhaust,
                )

                # Run a full agent turn; for a multi-way node the verdict is a matched
                # case label; for binary routing it is PASS/FAIL from the first non-empty
                # line; for a linear node there is no verdict.
                try:
                    resp = self._node_runner(req)
                except NodeExecutionError as exc:
                    # The node's turn raised: record an attributable node_failed run
                    # (with the persisted session key) so the manifest captures it,
                    # then re-raise to abort the walk — run() names the node.
                    runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                        output="", session_key=exc.session_key,
                                        budget=budget,
                                        status="node_failed", error=str(exc.cause)))
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
                                    status="persist_failed" if resp.persist_failed else "ok"))
                if node.context == "shared":
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
                        # asks the user and re-runs the workflow with the answers.
                        return WorkflowResult(
                            status="needs_input", final_output=output,
                            runs=runs, run_id=run_id, needs_input_node=node.id,
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
                else:
                    upstream_output = output
                    final_output = output
                    current = node.next

            elif isinstance(node, SubworkflowNode):
                if self._subworkflow_runner is None:
                    raise WorkflowConfigError(
                        f"node {node.id!r} is a subworkflow but the engine has no subworkflow_runner"
                    )
                output = self._subworkflow_runner(node.workflow, upstream_output or task, root_session_key)
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=output))
                upstream_output = output
                final_output = output
                current = node.next

            elif isinstance(node, ParallelNode):
                if node.worker is not None:
                    merged, abort = self._run_dynamic_parallel(
                        workflow, node, task, run_id, iteration, root_session_key,
                        upstream_output, runs, work_dir=work_dir)
                else:
                    merged, abort = self._run_parallel(
                        workflow, node, task, run_id, iteration, root_session_key,
                        upstream_output, runs, work_dir=work_dir)
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=merged))
                if abort is not None:
                    return WorkflowResult(
                        status="aborted", final_output=abort, runs=runs, run_id=run_id
                    )
                upstream_output = merged
                final_output = merged
                current = node.next

            # The node's record(s) are now appended — refresh the live manifest so an
            # in-flight run is observable before the next node starts.
            if update_manifest is not None:
                update_manifest()
            if self._progress_emit is not None:
                nodes = [
                    {
                        "id": r.node_id,
                        "label": node_label(workflow.nodes[r.node_id]) if r.node_id in workflow.nodes else r.node_id,
                        "status": (
                            "failed"
                            if r.status in ("node_failed", "persist_failed")
                            else "done"
                        ),
                        "route_label": r.route_label,
                    }
                    for r in runs
                ]
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
        return WorkflowResult(
            status="completed", final_output=final_output, runs=runs, run_id=run_id,
            output_dir=terminal_output_dir, output_files=output_files,
        )

    def _run_one_branch(self, branch, task, upstream, run_id, iteration, root_key,
                        workspace_override, out_dir=None):
        return self._node_runner(NodeRunRequest(
            node=branch, task=task, upstream_output=upstream, shared_context=[],
            run_id=run_id, iteration=iteration, root_session_key=root_key,
            workspace_override=workspace_override,
            output_dir=out_dir if getattr(branch, "tools", "none") == "default" else None,
        ))

    @staticmethod
    def _record_branches(runs, results, iteration):
        """Append a per-branch NodeRun (carrying its session_key, branch_id and failure
        status) for each ``(branch_id, output, session_key, error, persist_failed)`` tuple
        — so static-parallel branch sessions stay attributable in the run trace, mirroring
        the dynamic fan-out worker records. ``error`` is None for a branch that completed;
        ``persist_failed`` marks a branch that ran but whose session save raised."""
        for bid, out, session_key, error, persist_failed in results:
            runs.append(NodeRun(node_id=bid, iteration=iteration, output=out,
                                session_key=session_key, branch_id=bid,
                                status=("node_failed" if error else
                                        "persist_failed" if persist_failed else "ok"),
                                error=error))

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
            idx, subtask = args
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
                return idx, resp.output, resp.session_key, None, resp.persist_failed
            except NodeExecutionError as exc:
                return idx, "", exc.session_key, str(exc.cause), False
            except Exception as exc:  # noqa: BLE001 - isolate a single worker's failure
                return idx, "", None, str(exc), False

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = sorted(ex.map(_run_worker, enumerate(subtasks)))

        for idx, out, session_key, error, persist_failed in results:
            runs.append(NodeRun(node_id=node.worker, iteration=iteration, output=out,
                                session_key=session_key, worker_index=idx,
                                status=("node_failed" if error else
                                        "persist_failed" if persist_failed else "ok"),
                                error=error))

        if all(error for _idx, _out, _key, error, _pf in results):
            return "", f"parallel node {node.id!r}: every worker failed"

        merged = "\n\n".join(
            f"[{idx}] {out}" if error is None else f"[{idx}] FAILED: {error}"
            for idx, out, _key, error, _pf in results
        )
        return merged, None

    def _run_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream, runs, work_dir=None):
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

        branches = node.branches
        workers = max(1, min(len(branches), node.max_concurrency))
        # The run's shared working folder, expressed relative to the workspace, so it
        # can be exempted from the fork/diff exclusion — the folder IS branch output,
        # not machine-managed state. None when there is no work_dir or no workspace.
        rel_work = (
            str(Path(work_dir).resolve().relative_to(Path(self._workspace).resolve()))
            if (work_dir and self._workspace) else None
        )

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
            prior = [
                {"id": r.node_id,
                 "label": node_label(workflow.nodes[r.node_id]) if r.node_id in workflow.nodes else r.node_id,
                 "status": ("failed" if r.status in ("node_failed", "persist_failed") else "done"),
                 "route_label": r.route_label}
                for r in runs
            ]
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
                try:
                    resp = self._run_one_branch(
                        workflow.nodes[bid], task, upstream, run_id, iteration, root_key,
                        None, out_dir=work_dir)
                    with _branch_lock:
                        branch_status[bid] = "done"
                    _emit_branches()
                    return bid, resp.output, resp.session_key, None, resp.persist_failed
                except NodeExecutionError as exc:
                    with _branch_lock:
                        branch_status[bid] = "failed"
                    _emit_branches()
                    return bid, "", exc.session_key, str(exc.cause), False
                except Exception as exc:  # noqa: BLE001 - isolate a single branch's failure
                    with _branch_lock:
                        branch_status[bid] = "failed"
                    _emit_branches()
                    return bid, "", None, str(exc), False
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            self._record_branches(runs, results, iteration)
            if all(error for _bid, _out, _key, error, _pf in results):
                return "", f"parallel node {node.id!r}: every branch failed"
            return "\n\n".join(
                f"[{bid}]\n{out}" if error is None else f"[{bid}] FAILED: {error}"
                for bid, out, _key, error, _pf in results), None

        if self._workspace is None:
            return "", f"parallel node {node.id!r}: reconcile={node.reconcile!r} needs a workspace"

        base = workspace_fork.snapshot(self._workspace, extra_include=rel_work)
        forks: list = []

        def _run(bid):
            fork_dir = workspace_fork.fork(self._workspace, extra_include=rel_work)
            forks.append(fork_dir)
            try:
                if rel_work is not None:
                    branch_out = Path(fork_dir) / rel_work
                    branch_out.mkdir(parents=True, exist_ok=True)   # fork copy may be empty
                else:
                    branch_out = artifact_dir(fork_dir, run_id, bid, iteration)
                resp = self._run_one_branch(
                    workflow.nodes[bid], task, upstream, run_id, iteration, root_key,
                    str(fork_dir), out_dir=str(branch_out))
                with _branch_lock:
                    branch_status[bid] = "done"
                _emit_branches()
                return bid, resp.output, resp.session_key, workspace_fork.diff(base, fork_dir, extra_include=rel_work)
            except Exception:
                with _branch_lock:
                    branch_status[bid] = "failed"
                _emit_branches()
                raise

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            self._record_branches(runs, [(bid, out, key, None, False) for bid, out, key, _ in results], iteration)
            if node.reconcile == "choose":
                if self._pick_runner is None:
                    return "", f"parallel node {node.id!r}: 'choose' needs a pick_runner"
                idx = self._pick_runner(node.criteria, [out for _, out, _, _ in results], node.judge_model)
                idx = idx if isinstance(idx, int) and 0 <= idx < len(results) else 0
                bid, out, _key, cs = results[idx]
                workspace_fork.apply(cs, self._workspace)
                return f"[chosen: {bid}]\n{out}", None
            # union: apply every branch unless two touched the same path
            changesets = [cs for _, _, _, cs in results]
            conflict = workspace_fork.conflicts(changesets)
            if conflict:
                return "", f"parallel node {node.id!r}: union conflict on {sorted(conflict)}"
            for cs in changesets:
                workspace_fork.apply(cs, self._workspace)
            return "\n\n".join(f"[{bid}]\n{out}" for bid, out, _, _ in results), None
        finally:
            for fork_dir in forks:
                workspace_fork.cleanup(fork_dir)
