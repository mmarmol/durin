"""The workflow definition: a flow graph of nodes, parsed from a JSON-style dict.

A workflow is NOT a linear pipeline. It is a graph the user draws: work nodes do
a task and point to a next node; decision nodes route the flow (continue or loop
back) based on a condition. This slice supports a single objective condition type
(a shell command; exit 0 = pass). The parsed form is plain dataclasses the engine
walks deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union


class WorkflowError(ValueError):
    """Raised when a workflow definition is malformed."""


@dataclass(frozen=True)
class WorkNode:
    """A node that runs an agent turn and produces an output."""

    id: str
    model: str | None = None              # None = engine default
    context: Literal["own", "shared"] = "own"
    prompt: str = ""                      # the node's system/role framing
    next: str | None = None              # next node id; None = end
    tools: Literal["none", "default"] = "none"   # "default" = standard tool set
    kind: Literal["work"] = "work"


@dataclass(frozen=True)
class DecisionNode:
    """A node that routes the flow on a command's exit code (0 = pass)."""

    id: str
    command: str = ""
    on_pass: str | None = None           # next node on pass; None = end
    on_fail: str | None = None           # next node on fail (e.g. loop back)
    criteria: str = ""                   # judgment condition: a reviewer evaluates against this
    judge_model: str | None = None       # optional model for the judge (None = default)
    kind: Literal["decision"] = "decision"


@dataclass(frozen=True)
class SubworkflowNode:
    """A node that runs another workflow and uses its output."""

    id: str
    workflow: str = ""               # name of the workflow to run
    next: str | None = None          # next node id; None = end
    kind: Literal["subworkflow"] = "subworkflow"


@dataclass(frozen=True)
class ParallelNode:
    """A node that runs a set of work-node branches concurrently and merges their outputs."""

    id: str
    branches: tuple[str, ...] = ()
    next: str | None = None
    kind: Literal["parallel"] = "parallel"


Node = Union[WorkNode, DecisionNode, SubworkflowNode, ParallelNode]


@dataclass(frozen=True)
class Workflow:
    """A parsed flow graph: nodes keyed by id, a start node, a per-node loop cap."""

    name: str
    start: str
    nodes: dict[str, Node]
    max_visits: int = 3                  # max times a single node may run (loop guard)


def _build_node(raw: dict[str, Any]) -> Node:
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise WorkflowError(f"node is missing a string 'id': {raw!r}")
    kind = raw.get("kind", "work")
    if kind == "work":
        context = raw.get("context", "own")
        if context not in ("own", "shared"):
            raise WorkflowError(
                f"node {node_id!r}: context must be 'own' or 'shared', got {context!r}"
            )
        tools = raw.get("tools", "none")
        if tools not in ("none", "default"):
            raise WorkflowError(
                f"node {node_id!r}: tools must be 'none' or 'default', got {tools!r}"
            )
        model = raw.get("model")
        if model is not None and not isinstance(model, str):
            raise WorkflowError(
                f"node {node_id!r}: model must be a string or omitted, got {model!r}"
            )
        return WorkNode(
            id=node_id,
            model=model,
            context=context,
            prompt=raw.get("prompt", ""),
            next=raw.get("next"),
            tools=tools,
        )
    if kind == "decision":
        command = raw.get("command", "")
        criteria = raw.get("criteria", "")
        if bool(command) == bool(criteria):
            raise WorkflowError(
                f"node {node_id!r}: a decision node needs exactly one of 'command' or 'criteria'"
            )
        return DecisionNode(
            id=node_id,
            command=command,
            criteria=criteria,
            judge_model=raw.get("judge_model"),
            on_pass=raw.get("on_pass"),
            on_fail=raw.get("on_fail"),
        )
    if kind == "subworkflow":
        workflow = raw.get("workflow", "")
        if not workflow or not isinstance(workflow, str):
            raise WorkflowError(
                f"node {node_id!r}: a subworkflow node needs a non-empty 'workflow' name"
            )
        return SubworkflowNode(id=node_id, workflow=workflow, next=raw.get("next"))
    if kind == "parallel":
        branches = raw.get("branches", [])
        if not isinstance(branches, list) or not branches:
            raise WorkflowError(
                f"node {node_id!r}: a parallel node needs a non-empty 'branches' list"
            )
        return ParallelNode(id=node_id, branches=tuple(branches), next=raw.get("next"))
    raise WorkflowError(f"node {node_id!r}: unknown kind {kind!r}")


def _edge_targets(node: Node) -> list[str | None]:
    if isinstance(node, WorkNode):
        return [node.next]
    if isinstance(node, SubworkflowNode):
        return [node.next]
    if isinstance(node, ParallelNode):
        return [*node.branches, node.next]
    return [node.on_pass, node.on_fail]


def parse_workflow(data: dict[str, Any]) -> Workflow:
    """Parse a workflow definition dict into a validated Workflow."""
    name = data.get("name", "")
    if not name or not isinstance(name, str):
        raise WorkflowError("workflow is missing a 'name'")

    start = data.get("start")
    raw_nodes = data.get("nodes", [])
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise WorkflowError("workflow must have a non-empty 'nodes' list")

    nodes: dict[str, Node] = {}
    for raw in raw_nodes:
        node = _build_node(raw)
        if node.id in nodes:
            raise WorkflowError(f"duplicate node id {node.id!r}")
        nodes[node.id] = node

    if start is None:
        raise WorkflowError("workflow is missing 'start'")

    if start not in nodes:
        raise WorkflowError(f"start node {start!r} is not a defined node")

    for node in nodes.values():
        for target in _edge_targets(node):
            if target is not None and target not in nodes:
                raise WorkflowError(
                    f"node {node.id!r} points to unknown node {target!r}"
                )

    for node in nodes.values():
        if isinstance(node, ParallelNode):
            for branch in node.branches:
                if not isinstance(nodes[branch], WorkNode):
                    raise WorkflowError(
                        f"node {node.id!r}: parallel branch {branch!r} must be a work node"
                    )

    max_visits = data.get("max_visits", 3)
    if isinstance(max_visits, bool) or not isinstance(max_visits, int) or max_visits < 1:
        raise WorkflowError(f"max_visits must be an int >= 1, got {max_visits!r}")
    return Workflow(name=name, start=start, nodes=nodes, max_visits=max_visits)
