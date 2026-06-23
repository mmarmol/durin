"""The sequential flow-graph engine.

Walks a parsed Workflow from its start node, following edges. A work node runs via
the injected ``node_runner`` and its output passes along the edge to the next node;
a 'shared'-context node also reads/extends a running shared-context buffer, while an
'own'-context node is isolated (it sees only the upstream output). A decision node
evaluates its command and routes to on_pass / on_fail. A per-node visit cap guards
against infinite loop-backs. The run returns a typed WorkflowResult.

The graph logic is decoupled from real LLM execution: ``node_runner`` is injectable
so this engine is fully unit-testable with a mock. The default runner that wraps
AgentRunner + persists node sessions is Task 5.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass, field
from typing import Callable

from durin.workflow import workspace_fork
from durin.workflow.condition import CommandOutcome, run_command
from durin.workflow.judge import JudgeVerdict
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import DecisionNode, ParallelNode, SubworkflowNode, WorkNode, Workflow


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


@dataclass
class NodeRunResponse:
    output: str
    session_key: str | None = None
    messages: list[dict] = field(default_factory=list)


NodeRunner = Callable[[NodeRunRequest], NodeRunResponse]


class WorkflowEngine:
    def __init__(
        self,
        node_runner: NodeRunner,
        *,
        run_id_factory: Callable[[], str] | None = None,
        command_runner: Callable[..., CommandOutcome] = run_command,
        command_cwd: str | None = None,
        judge_runner: Callable[[str, str, "str | None"], JudgeVerdict] | None = None,
        subworkflow_runner: Callable[..., str] | None = None,
        workspace: str | None = None,
        pick_runner: Callable[[str, list[str], "str | None"], int] | None = None,
    ) -> None:
        self._node_runner = node_runner
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._command_runner = command_runner
        self._command_cwd = command_cwd
        self._judge_runner = judge_runner
        self._subworkflow_runner = subworkflow_runner
        # The real workspace (writing-parallel forks/applies here) and a runner that
        # picks the winning branch for 'choose' reconciliation. Both optional: a
        # read-only engine needs neither.
        self._workspace = workspace
        self._pick_runner = pick_runner

    def run(
        self, workflow: Workflow, task: str, *, root_session_key: str | None = None
    ) -> WorkflowResult:
        run_id = self._run_id_factory()
        runs: list[NodeRun] = []
        shared_context: list[dict] = []
        visits: dict[str, int] = {}
        upstream_output: str | None = None
        final_output: str | None = None
        current: str | None = workflow.start

        while current is not None:
            visits[current] = visits.get(current, 0) + 1
            if visits[current] > workflow.max_visits:
                return WorkflowResult(
                    status="max_visits", final_output=final_output, runs=runs, run_id=run_id
                )
            iteration = visits[current]
            node = workflow.nodes[current]

            if isinstance(node, WorkNode):
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
                )
                resp = self._node_runner(req)
                runs.append(
                    NodeRun(
                        node_id=node.id,
                        iteration=iteration,
                        output=resp.output,
                        session_key=resp.session_key,
                    )
                )
                if node.context == "shared":
                    shared_context.extend(resp.messages)
                upstream_output = resp.output
                final_output = resp.output
                current = node.next

            elif isinstance(node, SubworkflowNode):
                if self._subworkflow_runner is None:
                    raise RuntimeError(
                        f"node {node.id!r} is a subworkflow but the engine has no subworkflow_runner"
                    )
                output = self._subworkflow_runner(node.workflow, upstream_output or task, root_session_key)
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=output))
                upstream_output = output
                final_output = output
                current = node.next

            elif isinstance(node, ParallelNode):
                merged, abort = self._run_parallel(
                    workflow, node, task, run_id, iteration, root_session_key, upstream_output
                )
                runs.append(NodeRun(node_id=node.id, iteration=iteration, output=merged))
                if abort is not None:
                    return WorkflowResult(
                        status="aborted", final_output=abort, runs=runs, run_id=run_id
                    )
                upstream_output = merged
                final_output = merged
                current = node.next

            elif isinstance(node, DecisionNode):
                if node.criteria:
                    if self._judge_runner is None:
                        raise RuntimeError(
                            f"node {node.id!r} needs a judge but the engine has no judge_runner"
                        )
                    verdict = self._judge_runner(node.criteria, upstream_output or "", node.judge_model)
                    passed = verdict.passed
                    runs.append(NodeRun(node_id=node.id, iteration=iteration,
                                        output=verdict.feedback, passed=passed))
                    if not passed:
                        # thread the reviewer feedback to the producer it loops back to
                        prior = upstream_output or ""
                        upstream_output = f"{prior}\n\nReviewer feedback (address this):\n{verdict.feedback}"
                    current = node.on_pass if passed else node.on_fail
                else:
                    outcome = self._command_runner(node.command, cwd=self._command_cwd)
                    runs.append(
                        NodeRun(
                            node_id=node.id,
                            iteration=iteration,
                            output=outcome.output,
                            passed=outcome.passed,
                        )
                    )
                    current = node.on_pass if outcome.passed else node.on_fail

        return WorkflowResult(
            status="completed", final_output=final_output, runs=runs, run_id=run_id
        )

    def _run_one_branch(self, branch, task, upstream, run_id, iteration, root_key, workspace_override):
        return self._node_runner(NodeRunRequest(
            node=branch, task=task, upstream_output=upstream, shared_context=[],
            run_id=run_id, iteration=iteration, root_session_key=root_key,
            workspace_override=workspace_override,
        ))

    def _run_parallel(self, workflow, node, task, run_id, iteration, root_key, upstream):
        """Run a parallel node's branches concurrently and reconcile their writes.

        Returns ``(merged_output, abort_message)``; ``abort_message`` is None on
        success or a string when the run must abort (e.g. a union conflict, or a
        misconfiguration). 'read' branches run against the shared workspace (no writes
        applied); 'choose'/'union' branches each run against a private copy and their
        file changes are reconciled back.
        """
        branches = node.branches
        workers = max(1, len(branches))

        if node.reconcile == "read":
            def _run(bid):
                resp = self._run_one_branch(
                    workflow.nodes[bid], task, upstream, run_id, iteration, root_key, None)
                return bid, resp.output
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            return "\n\n".join(f"[{bid}]\n{out}" for bid, out in results), None

        if self._workspace is None:
            return "", f"parallel node {node.id!r}: reconcile={node.reconcile!r} needs a workspace"

        base = workspace_fork.snapshot(self._workspace)
        forks: list = []

        def _run(bid):
            fork_dir = workspace_fork.fork(self._workspace)
            forks.append(fork_dir)
            resp = self._run_one_branch(
                workflow.nodes[bid], task, upstream, run_id, iteration, root_key, str(fork_dir))
            return bid, resp.output, workspace_fork.diff(base, fork_dir)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, branches))
            if node.reconcile == "choose":
                if self._pick_runner is None:
                    return "", f"parallel node {node.id!r}: 'choose' needs a pick_runner"
                idx = self._pick_runner(node.criteria, [out for _, out, _ in results], node.judge_model)
                idx = idx if isinstance(idx, int) and 0 <= idx < len(results) else 0
                bid, out, cs = results[idx]
                workspace_fork.apply(cs, self._workspace)
                return f"[chosen: {bid}]\n{out}", None
            # union: apply every branch unless two touched the same path
            changesets = [cs for _, _, cs in results]
            conflict = workspace_fork.conflicts(changesets)
            if conflict:
                return "", f"parallel node {node.id!r}: union conflict on {sorted(conflict)}"
            for cs in changesets:
                workspace_fork.apply(cs, self._workspace)
            return "\n\n".join(f"[{bid}]\n{out}" for bid, out, _ in results), None
        finally:
            for fork_dir in forks:
                workspace_fork.cleanup(fork_dir)
