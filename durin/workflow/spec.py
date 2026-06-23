"""The workflow definition: a flow graph of nodes, parsed from a JSON-style dict.

A workflow is NOT a linear pipeline. It is a graph the user draws: nodes do a task
and optionally route the flow on a pass/fail verdict. A node has either an agent
body (runs a model turn) or a command body (runs a shell command, exit 0 = pass).
Routing is opt-in: set on_pass/on_fail to make a node emit a verdict; omit them
and the node uses a single next edge. The parsed form is plain dataclasses the
engine walks deterministically.

The ``kind: "decision"`` JSON field is a back-compat alias for a routing WorkNode:
``criteria`` maps to ``prompt``, ``judge_model`` maps to ``model`` (if model is
unset). No data migration required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union


class WorkflowError(ValueError):
    """Raised when a workflow definition is malformed."""


@dataclass(frozen=True)
class WorkNode:
    """A node that runs an agent turn (or a shell command) and produces an output.

    Routing is optional. When on_pass or on_fail is set the node emits a verdict
    after executing its body: an agent node's output is parsed for a PASS/FAIL
    line; a command node uses the exit code (0 = pass). Without routing the node
    follows next unconditionally.
    """

    id: str
    model: str | None = None              # None = engine default
    context: Literal["own", "shared"] = "own"
    prompt: str = ""                      # agent system/role framing (empty = upstream context only)
    next: str | None = None              # next node id; None = end (mutually exclusive with on_pass/on_fail)
    mode: str = "build"                   # AgentMode name: build (full) / plan / explore / custom
    tools: Literal["none", "default"] = "none"   # "default" = standard tool set
    skills: tuple[str, ...] = ()          # named skills to inject into this node only
    mcps: tuple[str, ...] = ()            # MCP servers (already configured) whose tools this node may use
    command: str = ""                     # non-empty => command body; the agent turn is skipped
    on_pass: str | None = None           # routing: next node on pass/exit-0; set => this node routes
    on_fail: str | None = None           # routing: next node on fail/non-zero exit
    kind: Literal["work"] = "work"

    @property
    def routes(self) -> bool:
        """True when this node emits a pass/fail verdict and branches on it."""
        return self.on_pass is not None or self.on_fail is not None

    @property
    def is_command(self) -> bool:
        """True when this node runs a shell command rather than an agent turn."""
        return bool(self.command)


@dataclass(frozen=True)
class SubworkflowNode:
    """A node that runs another workflow and uses its output."""

    id: str
    workflow: str = ""               # name of the workflow to run
    next: str | None = None          # next node id; None = end
    kind: Literal["subworkflow"] = "subworkflow"


@dataclass(frozen=True)
class ParallelNode:
    """A node that runs a set of work-node branches concurrently and merges their
    outputs. ``reconcile`` decides how their file writes come back together:
    'read' = read-only branches (no isolation, no writes applied); 'choose' = each
    branch writes in its own copy, a judge picks one to apply; 'union' = apply every
    branch's writes, failing on a same-file conflict."""

    id: str
    branches: tuple[str, ...] = ()
    next: str | None = None
    reconcile: Literal["read", "choose", "union"] = "read"
    criteria: str = ""                   # for 'choose': how the judge picks the winner
    judge_model: str | None = None       # optional model for the 'choose' judge
    kind: Literal["parallel"] = "parallel"


Node = Union[WorkNode, SubworkflowNode, ParallelNode]


@dataclass(frozen=True)
class Workflow:
    """A parsed flow graph: nodes keyed by id, a start node, a per-node loop cap."""

    name: str
    start: str
    nodes: dict[str, Node]
    max_visits: int = 3                  # max times a single node may run (loop guard)
    # dream-driven self-improvement: 'off' = never touched; 'manual' = dream leaves a
    # recommendation to review; 'auto' = dream applies edits directly (later slice).
    improvement_mode: Literal["off", "manual", "auto"] = "off"


def _str_list(value: Any, node_id: str, field: str) -> tuple[str, ...]:
    """Validate an optional list-of-strings node field; default to empty."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise WorkflowError(
            f"node {node_id!r}: {field} must be a list of strings, got {value!r}"
        )
    return tuple(value)


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
        skills = _str_list(raw.get("skills", []), node_id, "skills")
        mcps = _str_list(raw.get("mcps", []), node_id, "mcps")
        on_pass = raw.get("on_pass")
        on_fail = raw.get("on_fail")
        next_node = raw.get("next")
        if next_node is not None and (on_pass is not None or on_fail is not None):
            raise WorkflowError(
                f"node {node_id!r}: 'next' and routing ('on_pass'/'on_fail') are mutually exclusive"
            )
        routes = on_pass is not None or on_fail is not None
        mode_default = "explore" if routes else "build"
        mode = raw.get("mode", mode_default)
        if not isinstance(mode, str) or not mode:
            raise WorkflowError(f"node {node_id!r}: mode must be a non-empty string, got {mode!r}")
        return WorkNode(
            id=node_id,
            model=model,
            context=context,
            prompt=raw.get("prompt", ""),
            next=next_node,
            mode=mode,
            tools=tools,
            skills=skills,
            mcps=mcps,
            command=raw.get("command", ""),
            on_pass=on_pass,
            on_fail=on_fail,
        )
    if kind == "decision":
        # Back-compat alias: kind=decision maps to a routing WorkNode.
        # 'criteria' maps to 'prompt'; 'judge_model' maps to 'model' when model is unset.
        command = raw.get("command", "")
        criteria = raw.get("criteria", "")
        if command and criteria:
            raise WorkflowError(
                f"node {node_id!r}: a decision node needs exactly one of 'command' or 'criteria'"
            )
        model = raw.get("model")
        if model is None:                       # map judge_model only when model is unset
            model = raw.get("judge_model")
        on_pass = raw.get("on_pass")
        on_fail = raw.get("on_fail")
        if raw.get("next") is not None and (on_pass is not None or on_fail is not None):
            raise WorkflowError(
                f"node {node_id!r}: 'next' and routing ('on_pass'/'on_fail') are mutually exclusive"
            )
        # Routing agent nodes default to explore mode (read-only) for independence.
        mode = raw.get("mode", "explore") if not command else raw.get("mode", "build")
        return WorkNode(
            id=node_id,
            model=model,
            context=raw.get("context", "own"),
            prompt=criteria,
            next=raw.get("next"),
            mode=mode,
            tools=raw.get("tools", "none"),
            skills=_str_list(raw.get("skills", []), node_id, "skills"),
            mcps=_str_list(raw.get("mcps", []), node_id, "mcps"),
            command=command,
            on_pass=on_pass,
            on_fail=on_fail,
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
        reconcile = raw.get("reconcile", "read")
        if reconcile not in ("read", "choose", "union"):
            raise WorkflowError(
                f"node {node_id!r}: reconcile must be 'read', 'choose' or 'union', got {reconcile!r}"
            )
        criteria = raw.get("criteria", "")
        if reconcile == "choose" and not criteria:
            raise WorkflowError(
                f"node {node_id!r}: a 'choose' parallel node needs 'criteria' for the judge"
            )
        return ParallelNode(
            id=node_id, branches=tuple(branches), next=raw.get("next"),
            reconcile=reconcile, criteria=criteria, judge_model=raw.get("judge_model"),
        )
    raise WorkflowError(f"node {node_id!r}: unknown kind {kind!r}")


def _edge_targets(node: Node) -> list[str | None]:
    if isinstance(node, WorkNode):
        if node.routes:
            return [node.on_pass, node.on_fail]
        return [node.next]
    if isinstance(node, SubworkflowNode):
        return [node.next]
    if isinstance(node, ParallelNode):
        return [*node.branches, node.next]
    return []  # unreachable with the current Node union


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

    # Anti-Goodhart guard: a routing agent node must not be structurally identical
    # to its producer. If a predecessor P (agent WorkNode, P.id != J.id) shares the
    # same model, mode, and prompt as routing agent node J, the graph is rejected.
    # A self-loop (on_fail == J.id) is exempt — we only compare distinct node pairs.
    # Routing nodes default to mode="explore" while producers default to mode="build",
    # so this fires only when a user explicitly makes the judge identical to its producer.
    predecessor_map: dict[str, list[str]] = {n: [] for n in nodes}
    for src_node in nodes.values():
        for target in _edge_targets(src_node):
            if target is not None and target in predecessor_map:
                predecessor_map[target].append(src_node.id)
    for j in nodes.values():
        if not (isinstance(j, WorkNode) and j.routes and not j.is_command):
            continue
        for pred_id in predecessor_map[j.id]:
            if pred_id == j.id:
                continue
            p = nodes[pred_id]
            if isinstance(p, WorkNode) and not p.is_command:
                if (p.model, p.mode, p.prompt) == (j.model, j.mode, j.prompt):
                    raise WorkflowError(
                        f"node {j.id!r}: a routing node must not be structurally identical to its "
                        f"producer {p.id!r} (vary model, mode, or prompt for an independent verdict)"
                    )

    max_visits = data.get("max_visits", 3)
    if isinstance(max_visits, bool) or not isinstance(max_visits, int) or max_visits < 1:
        raise WorkflowError(f"max_visits must be an int >= 1, got {max_visits!r}")

    mode = data.get("improvement_mode", "off")
    if mode not in ("off", "manual", "auto"):
        raise WorkflowError(
            f"improvement_mode must be 'off', 'manual' or 'auto', got {mode!r}"
        )
    return Workflow(
        name=name, start=start, nodes=nodes, max_visits=max_visits, improvement_mode=mode
    )
