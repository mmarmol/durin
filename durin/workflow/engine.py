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

import uuid
from dataclasses import dataclass, field
from typing import Callable

from durin.workflow.condition import CommandOutcome, run_command
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import DecisionNode, WorkNode, Workflow


@dataclass
class NodeRunRequest:
    node: WorkNode
    task: str
    upstream_output: str | None
    shared_context: list[dict]
    run_id: str
    iteration: int
    root_session_key: str | None


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
    ) -> None:
        self._node_runner = node_runner
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._command_runner = command_runner

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

            elif isinstance(node, DecisionNode):
                outcome = self._command_runner(node.command, cwd=None)
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
