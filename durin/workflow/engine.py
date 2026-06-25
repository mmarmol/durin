"""The sequential flow-graph engine.

Walks a parsed Workflow from its start node, following edges. A work node runs via
the injected ``node_runner`` and its output passes along the edge to the next node;
a 'shared'-context node also reads/extends a running shared-context buffer, while an
'own'-context node is isolated (it sees only the upstream output). A routing node
derives a verdict from its own output and follows the matching edge: a binary node
(on_pass/on_fail) routes on a PASS/FAIL verdict (agent: first-line parse_verdict;
command: exit code); a multi-way node (cases) routes on which declared label the agent
emits (parse_label) — to that label's target, to "default", or aborting if neither. A
per-node visit cap guards against infinite loop-backs. The run returns a typed
WorkflowResult.

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
from durin.workflow.condition import CommandOutcome, run_command
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import NEEDS_INPUT_TARGET, ParallelNode, SubworkflowNode, WorkNode, Workflow
from durin.workflow.verdict import parse_label, parse_verdict


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
    # Engine-provided keyed output folder for this node (write here) and the folder
    # the producing predecessor wrote into (read from). Both None when the engine has
    # no workspace or for command nodes.
    output_dir: str | None = None
    upstream_artifact_dir: str | None = None
    # Index within a dynamic fan-out batch (0, 1, 2, …). When set, the session-persist
    # key includes this suffix so each worker gets a distinct session rather than
    # all workers overwriting the same key.
    worker_index: int | None = None


@dataclass
class NodeRunResponse:
    output: str
    session_key: str | None = None
    messages: list[dict] = field(default_factory=list)
    # True when the node ran but persisting its session failed (the conversation is
    # lost). Lets the engine record a truthful 'persist_failed' status instead of a
    # misleading 'ok' with a silently-absent session.
    persist_failed: bool = False


NodeRunner = Callable[[NodeRunRequest], NodeRunResponse]

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
        command_runner: Callable[..., CommandOutcome] = run_command,
        command_cwd: str | None = None,
        subworkflow_runner: Callable[..., str] | None = None,
        workspace: str | None = None,
        pick_runner: Callable[[str, list[str], "str | None"], int] | None = None,
        max_node_visits: int = 1000,
    ) -> None:
        self._node_runner = node_runner
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._command_runner = command_runner
        self._command_cwd = command_cwd
        self._subworkflow_runner = subworkflow_runner
        # The real workspace (writing-parallel forks/applies here) and a runner that
        # picks the winning branch for 'choose' reconciliation. Both optional: a
        # read-only engine needs neither.
        self._workspace = workspace
        self._pick_runner = pick_runner
        self._max_node_visits = max_node_visits

    def run(
        self,
        workflow: Workflow,
        task: str,
        *,
        root_session_key: str | None = None,
        input_files: list[str] | None = None,
    ) -> WorkflowResult:
        """Run the workflow. A node-execution failure (provider/MCP/tool error) does not
        propagate — it ends the run as a typed ``aborted`` result carrying the partial
        per-node trace, so the run is still recorded for diagnostics. A wiring/config
        error (a node needing a runner the engine wasn't given) fails fast.

        When the engine has a workspace it owns a live run manifest: a ``running`` record
        written before the walk, updated after each node, and finalized on every exit
        path. Manifest writes are best-effort — a record failure never breaks the run."""
        run_id = self._run_id_factory()
        if self._workspace is not None:
            prune_runs(self._workspace)
        runs: list[NodeRun] = []
        # The effective root MUST match node_runner's headless rooting: a None calling
        # session means node sessions are rooted under workflow:<run_id>:root, so the
        # manifest records that same key or runs_for_session can't find the run.
        effective_root = root_session_key or f"workflow:{run_id}:root"
        started_at = time.time()
        self._start_manifest(workflow, run_id, effective_root, started_at)

        def _update() -> None:
            self._update_manifest(workflow, run_id, runs)

        try:
            result = self._walk(
                workflow, self._frame_task(workflow, task), run_id, runs,
                root_session_key=root_session_key,
                input_files=input_files,
                update_manifest=_update,
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

    def _start_manifest(self, workflow, run_id, root_session_key, started_at) -> None:
        if self._workspace is None:
            return
        try:
            run_log.start_run(self._workspace, workflow.name, run_id,
                              root_session_key=root_session_key, started_at=started_at)
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
    def _frame_task(workflow: Workflow, task: str) -> str:
        """Frame the task with the workflow's optional I/O descriptions: the input
        description as a prefix (what the workflow received) and the output description
        as a suffix (what it must ultimately deliver). Both are free-text hints that
        steer the node agents and document the interface — they are not enforced. When
        neither is set the task is returned unchanged."""
        def _desc(d: object) -> str | None:
            text = d.get("description") if isinstance(d, dict) else None
            text = str(text).strip() if text else ""
            return text or None
        intro = _desc(workflow.input)
        goal = _desc(workflow.output)
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
    ) -> WorkflowResult:
        shared_context: list[dict] = []
        visits: dict[str, int] = {}
        upstream_output: str | None = None
        upstream_artifact_dir: str | None = None
        terminal_output_dir: str | None = None
        final_output: str | None = None
        current: str | None = workflow.start

        # Seed an input folder for the start node when input_files are given and a
        # workspace is available — the start node reads them as "previous step's files".
        if input_files and self._workspace is not None:
            input_folder = artifact_dir(self._workspace, run_id, "__input__", 0)
            for path in input_files:
                shutil.copy(path, input_folder / Path(path).name)
            upstream_artifact_dir = str(input_folder)

        while current is not None:
            visits[current] = visits.get(current, 0) + 1
            node = workflow.nodes[current]
            budget = min(getattr(node, "max_visits", None) or workflow.max_visits, self._max_node_visits)
            if visits[current] > budget:
                return WorkflowResult(
                    status="exhausted", final_output=final_output, runs=runs,
                    run_id=run_id, exhausted_node=current,
                )
            iteration = visits[current]

            if isinstance(node, WorkNode):
                # Compute an artifact folder only for a node that can do file I/O — an
                # agent body with file tools — and when a workspace is available. A
                # command node or a no-tools node produces no files, gets no folder, and
                # nils the chain for the next node (below).
                out_dir: str | None = None
                if not node.is_command and node.tools == "default" and self._workspace is not None:
                    out_dir = str(artifact_dir(self._workspace, run_id, node.id, iteration))

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
                    upstream_artifact_dir=upstream_artifact_dir,
                )

                if node.is_command:
                    # Command body: run the shell command; verdict is the exit code.
                    outcome = self._command_runner(node.command, cwd=self._command_cwd)
                    output = outcome.output
                    passed = outcome.passed
                    runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                        output=output, session_key=None,
                                        passed=passed if node.routes else None,
                                        status="no_session"))
                else:
                    # Agent body: run a full agent turn; for a multi-way node the
                    # verdict is a matched case label; for binary routing it is
                    # PASS/FAIL from the first non-empty line; for a linear node
                    # there is no verdict.
                    try:
                        resp = self._node_runner(req)
                    except NodeExecutionError as exc:
                        # The node's turn raised: record an attributable node_failed run
                        # (with the persisted session key) so the manifest captures it,
                        # then re-raise to abort the walk — run() names the node.
                        runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                            output="", session_key=exc.session_key,
                                            status="node_failed", error=str(exc.cause)))
                        if update_manifest is not None:
                            update_manifest()
                        raise
                    output = resp.output
                    if node.cases is not None:
                        # Multi-way: label matching replaces pass/fail.
                        passed = None
                    elif node.routes:
                        passed = parse_verdict(output)
                    else:
                        passed = None
                    runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                        output=output, session_key=resp.session_key,
                                        passed=passed,
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
                    label = parse_label(output, node.cases)
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
                            runs=runs, run_id=run_id,
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
                    current = target
                elif node.routes:
                    if not passed:
                        # Thread reviewer feedback into upstream so the producer sees it.
                        prior = upstream_output or ""
                        upstream_output = (
                            f"{prior}\n\nReviewer feedback (address this):\n{output}"
                        )
                    # Do NOT advance upstream_artifact_dir here: a routing judge's
                    # (empty) folder must never replace the producing node's folder
                    # for the on_pass target — mirrors upstream_output's update rule.
                    current = node.on_pass if passed else node.on_fail
                else:
                    upstream_output = output
                    # Mirrors upstream_output. A node that can't produce files (a command
                    # node, or an agent node without file tools) has out_dir=None, so it
                    # nils the chain for the next node — consistent with it also replacing
                    # the text output.
                    upstream_artifact_dir = out_dir
                    if out_dir is not None:
                        terminal_output_dir = out_dir
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
                        upstream_output, runs
                    )
                else:
                    merged, abort = self._run_parallel(
                        workflow, node, task, run_id, iteration, root_session_key,
                        upstream_output, runs
                    )
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

        return WorkflowResult(
            status="completed", final_output=final_output, runs=runs, run_id=run_id,
            output_dir=terminal_output_dir,
        )

    def _run_one_branch(self, branch, task, upstream, run_id, iteration, root_key, workspace_override, fork_dir=None):
        out_dir: str | None = None
        if fork_dir is not None:
            out_dir = str(artifact_dir(fork_dir, run_id, branch.id, iteration))
        return self._node_runner(NodeRunRequest(
            node=branch, task=task, upstream_output=upstream, shared_context=[],
            run_id=run_id, iteration=iteration, root_session_key=root_key,
            workspace_override=workspace_override,
            output_dir=out_dir,
            upstream_artifact_dir=None,
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

        Tries JSON first; if the parsed value is a list, returns each element
        coerced to str.  Falls back to non-empty lines.  Capped at 50 items
        to bound blast radius on pathological output.

        A planner often wraps the array in a markdown code fence (```/```json);
        strip a leading and trailing fence line before the JSON attempt so the
        fenced array parses to its elements instead of falling through to the
        line-split fallback (which would yield the literal fence lines).
        """
        import json
        candidate = text.strip()
        lines = candidate.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            candidate = "\n".join(lines)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                items = [str(x) for x in parsed]
                return items[:50]
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back: non-empty lines (of the fence-stripped candidate, so a malformed
        # fenced array doesn't leak its fence lines as bogus subtasks).
        items = [line for line in candidate.splitlines() if line.strip()]
        return items[:50]

    def _run_dynamic_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream, runs):
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

    def _run_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream, runs):
        """Run a parallel node's branches concurrently and reconcile their writes.

        Returns ``(merged_output, abort_message)``; ``abort_message`` is None on
        success or a string when the run must abort (e.g. a union conflict, or a
        misconfiguration). 'read' branches run against the shared workspace (no writes
        applied); 'choose'/'union' branches each run against a private copy and their
        file changes are reconciled back. Each branch is appended to ``runs`` so its
        session stays attributable in the trace.
        """
        branches = node.branches
        workers = max(1, min(len(branches), node.max_concurrency))

        if node.reconcile == "read":
            def _run(bid):
                # A branch that raises must not take down the others: catch it and tag the
                # failure so survivors still complete (per-branch isolation, like fan-out).
                try:
                    resp = self._run_one_branch(
                        workflow.nodes[bid], task, upstream, run_id, iteration, root_key, None)
                    return bid, resp.output, resp.session_key, None, resp.persist_failed
                except NodeExecutionError as exc:
                    return bid, "", exc.session_key, str(exc.cause), False
                except Exception as exc:  # noqa: BLE001 - isolate a single branch's failure
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

        base = workspace_fork.snapshot(self._workspace)
        forks: list = []

        def _run(bid):
            fork_dir = workspace_fork.fork(self._workspace)
            forks.append(fork_dir)
            resp = self._run_one_branch(
                workflow.nodes[bid], task, upstream, run_id, iteration, root_key,
                str(fork_dir), fork_dir=fork_dir)
            return bid, resp.output, resp.session_key, workspace_fork.diff(base, fork_dir)

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
