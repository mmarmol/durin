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
    kind: Literal["work"] = "work"


@dataclass(frozen=True)
class DecisionNode:
    """A node that routes the flow on a command's exit code (0 = pass)."""

    id: str
    command: str = ""
    on_pass: str | None = None           # next node on pass; None = end
    on_fail: str | None = None           # next node on fail (e.g. loop back)
    kind: Literal["decision"] = "decision"


Node = Union[WorkNode, DecisionNode]


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
        return WorkNode(
            id=node_id,
            model=raw.get("model"),
            context=context,
            prompt=raw.get("prompt", ""),
            next=raw.get("next"),
        )
    if kind == "decision":
        return DecisionNode(
            id=node_id,
            command=raw.get("command", ""),
            on_pass=raw.get("on_pass"),
            on_fail=raw.get("on_fail"),
        )
    raise WorkflowError(f"node {node_id!r}: unknown kind {kind!r}")


def _edge_targets(node: Node) -> list[str | None]:
    if isinstance(node, WorkNode):
        return [node.next]
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

    max_visits = data.get("max_visits", 3)
    return Workflow(name=name, start=start, nodes=nodes, max_visits=max_visits)
