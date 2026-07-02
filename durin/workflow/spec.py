"""The workflow definition: a flow graph of nodes, parsed from a JSON-style dict.

A workflow is NOT a linear pipeline. It is a graph the user draws: nodes do a task
and optionally route the flow on a pass/fail verdict. A node runs an agent turn and
produces an output. Routing is opt-in: set on_pass/on_fail to make a node emit a
verdict; omit them and the node uses a single next edge. The parsed form is plain
dataclasses the engine walks deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Union

from durin.workflow.verdict import normalize_label


# ---------------------------------------------------------------------------
# Human-readable node label helpers
# ---------------------------------------------------------------------------

def _first_sentence(prompt: str, *, max_chars: int = 80) -> str:
    s = (prompt or "").strip()
    if not s:
        return ""
    first = re.split(r"(?<=[.!?])\s|\n", s, maxsplit=1)[0].strip().rstrip(".")
    if len(first) > max_chars:
        first = first[:max_chars].rsplit(" ", 1)[0] + "…"
    return first


def _prettify_id(node_id: str) -> str:
    s = node_id.replace("_", " ").replace("-", " ").strip()
    return (s[:1].upper() + s[1:]) if s else node_id


def node_label(node: Any) -> str:
    """Human label for a node: author title, else the first sentence of its role
    prompt, else a prettified id. Used by the live progress emit and the tasks API."""
    title = (getattr(node, "title", "") or "").strip()
    if title:
        return title
    sent = _first_sentence(getattr(node, "prompt", "") or "")
    return sent or _prettify_id(node.id)

# Reserved multi-way routing target: a node whose matched case routes here ends the
# run asking the caller for more information (status "needs_input", the node's output
# carries the questions) instead of completing. It is NOT a node id — the human-in-the-
# loop lives in the agent that invoked the workflow, which asks the user and re-runs.
NEEDS_INPUT_TARGET = "__needs_input__"


class WorkflowError(ValueError):
    """Raised when a workflow definition is malformed."""


@dataclass(frozen=True)
class WorkNode:
    """A node that runs an agent turn and produces an output.

    Routing is optional. When on_pass or on_fail is set the node emits a verdict
    after executing its body: the agent's output is parsed for a PASS/FAIL line.
    Without routing the node follows next unconditionally.
    """

    id: str
    title: str = ""                       # optional human label (overrides prompt-derived label)
    model: str | None = None              # None = engine default
    persona: str | None = None            # named persona (xor model; None = no persona)
    context: Literal["own", "shared"] = "own"
    session: Literal["fresh", "persistent"] = "fresh"  # persistent = revisits resume the SAME session
    prompt: str = ""                      # agent system/role framing (empty = upstream context only)
    next: str | None = None              # next node id; None = end (mutually exclusive with on_pass/on_fail)
    mode: str = "build"                   # AgentMode name: build (full) / plan / explore / custom
    tools: Literal["none", "default"] = "none"   # "default" = standard tool set
    skills: tuple[str, ...] = ()          # named skills to inject into this node only
    mcps: tuple[str, ...] = ()            # MCP servers (already configured) whose tools this node may use
    on_pass: str | None = None           # routing: next node on pass; set => this node routes
    on_fail: str | None = None           # routing: next node on fail
    cases: dict[str, str | None] | None = None  # multi-way routing: label -> target node id (null = end)
    max_visits: int | None = None        # per-node loop cap (None = inherit workflow default)
    max_turns: int | None = None         # agentic tool-round budget for this node (None = global default)
    kind: Literal["work"] = "work"

    @property
    def routes(self) -> bool:
        """True when this node emits a verdict and branches: binary (on_pass/on_fail) or multi-way (cases)."""
        return self.on_pass is not None or self.on_fail is not None or self.cases is not None


@dataclass(frozen=True)
class SubworkflowNode:
    """A node that runs another workflow and uses its output."""

    id: str
    title: str = ""                  # optional human label (overrides prettified id)
    workflow: str = ""               # name of the workflow to run
    next: str | None = None          # next node id; None = end
    kind: Literal["subworkflow"] = "subworkflow"


@dataclass(frozen=True)
class ParallelNode:
    """A node that runs a set of work-node branches concurrently and merges their
    outputs. ``reconcile`` decides how their file writes come back together:
    'read' = read-only branches (no isolation, no writes applied); 'choose' = each
    branch writes in its own copy, a judge picks one to apply; 'union' = apply every
    branch's writes, failing on a same-file conflict.

    Static mode: ``branches`` is non-empty, ``worker``/``list_from`` are None.
    Dynamic mode: ``worker`` names a work node to run per item; ``list_from`` names
    the upstream node whose output is parsed as the runtime list; ``branches`` is
    empty.  Bounded by ``max_concurrency`` in both modes.
    """

    id: str
    title: str = ""                        # optional human label (overrides prettified id)
    branches: tuple[str, ...] = ()
    next: str | None = None
    reconcile: Literal["read", "choose", "union"] = "read"
    criteria: str = ""                   # for 'choose': how the judge picks the winner
    judge_model: str | None = None       # optional model for the 'choose' judge
    max_concurrency: int = 2             # max simultaneous branch/worker runners (>= 1)
    worker: str | None = None            # dynamic mode: worker-template node id
    list_from: str | None = None         # dynamic mode: node whose output is the runtime list
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
    improvement_mode: Literal["manual", "auto"] = "manual"
    input: dict | None = None            # workflow I/O descriptors (e.g. {text: bool, file: bool})
    output: dict | None = None
    description: str | None = None       # one-line "what it does + when to use it" (for discovery)


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
        session = raw.get("session", "fresh")
        if session not in ("fresh", "persistent"):
            raise WorkflowError(
                f"node {node_id!r}: session must be 'fresh' or 'persistent', got {session!r}"
            )
        # Persistent session and the shared buffer are two competing continuity
        # mechanisms; combining them would re-feed a node its own history twice.
        if session == "persistent" and context == "shared":
            raise WorkflowError(
                f"node {node_id!r}: session='persistent' requires context='own'"
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
        persona = raw.get("persona")
        if persona is not None and not isinstance(persona, str):
            raise WorkflowError(
                f"node {node_id!r}: persona must be a string or omitted, got {persona!r}"
            )
        if persona is not None and model is not None:
            raise WorkflowError(
                f"node {node_id!r}: persona and model are mutually exclusive — set one or neither"
            )
        skills = _str_list(raw.get("skills", []), node_id, "skills")
        mcps = _str_list(raw.get("mcps", []), node_id, "mcps")
        on_pass = raw.get("on_pass")
        on_fail = raw.get("on_fail")
        next_node = raw.get("next")
        cases_raw = raw.get("cases")
        # Parse and validate multi-way routing cases.
        cases: dict[str, str | None] | None = None
        if cases_raw is not None:
            if not isinstance(cases_raw, dict):
                raise WorkflowError(
                    f"node {node_id!r}: 'cases' must be a dict, got {cases_raw!r}"
                )
            if not cases_raw:
                raise WorkflowError(
                    f"node {node_id!r}: 'cases' must not be empty"
                )
            for label, target in cases_raw.items():
                if not isinstance(label, str) or not label:
                    raise WorkflowError(
                        f"node {node_id!r}: 'cases' keys must be non-empty strings, got {label!r}"
                    )
                if target is not None and not isinstance(target, str):
                    raise WorkflowError(
                        f"node {node_id!r}: 'cases' values must be a string node id or null, got {target!r}"
                    )
            cases = dict(cases_raw)
            # Reject labels that normalize to the same form — they would cause a
            # silent mis-route because parse_label uses the same normalization.
            seen_norms: dict[str, str] = {}
            for label in cases:
                norm = normalize_label(label)
                if norm in seen_norms:
                    raise WorkflowError(
                        f"node {node_id!r}: case labels {seen_norms[norm]!r} and "
                        f"{label!r} normalize to the same form and would mis-route"
                    )
                seen_norms[norm] = label
        # Mutual exclusivity: exactly one of next, on_pass/on_fail, or cases.
        binary_routing = on_pass is not None or on_fail is not None
        if cases is not None and binary_routing:
            raise WorkflowError(
                f"node {node_id!r}: 'cases' and 'on_pass'/'on_fail' are mutually exclusive"
            )
        if cases is not None and next_node is not None:
            raise WorkflowError(
                f"node {node_id!r}: 'cases' and 'next' are mutually exclusive"
            )
        if next_node is not None and binary_routing:
            raise WorkflowError(
                f"node {node_id!r}: 'next' and routing ('on_pass'/'on_fail') are mutually exclusive"
            )
        routes = binary_routing or cases is not None
        # A routing node emits an independent verdict on its own output; a shared
        # context buffer would feed it sibling conversations and bias that verdict,
        # so the two are mutually exclusive.
        if routes and context == "shared":
            raise WorkflowError(
                f"node {node_id!r}: a routing node ('on_pass'/'on_fail' or 'cases') cannot use "
                f"context='shared'"
            )
        mode_default = "explore" if routes else "build"
        mode = raw.get("mode", mode_default)
        if not isinstance(mode, str) or not mode:
            raise WorkflowError(f"node {node_id!r}: mode must be a non-empty string, got {mode!r}")
        node_max_visits = raw.get("max_visits")
        if node_max_visits is not None:
            if isinstance(node_max_visits, bool) or not isinstance(node_max_visits, int) or node_max_visits < 1:
                raise WorkflowError(
                    f"node {node_id!r}: max_visits must be an int >= 1, got {node_max_visits!r}"
                )
        node_max_turns = raw.get("max_turns")
        if node_max_turns is not None:
            if isinstance(node_max_turns, bool) or not isinstance(node_max_turns, int) or node_max_turns < 1:
                raise WorkflowError(
                    f"node {node_id!r}: max_turns must be an int >= 1, got {node_max_turns!r}"
                )
        return WorkNode(
            id=node_id,
            title=raw.get("title", ""),
            model=model,
            persona=persona,
            context=context,
            session=session,
            prompt=raw.get("prompt", ""),
            next=next_node,
            mode=mode,
            tools=tools,
            skills=skills,
            mcps=mcps,
            on_pass=on_pass,
            on_fail=on_fail,
            cases=cases,
            max_visits=node_max_visits,
            max_turns=node_max_turns,
        )
    if kind == "subworkflow":
        workflow = raw.get("workflow", "")
        if not workflow or not isinstance(workflow, str):
            raise WorkflowError(
                f"node {node_id!r}: a subworkflow node needs a non-empty 'workflow' name"
            )
        return SubworkflowNode(id=node_id, title=raw.get("title", ""), workflow=workflow, next=raw.get("next"))
    if kind == "parallel":
        worker = raw.get("worker")
        list_from = raw.get("list_from")
        branches_raw = raw.get("branches", [])
        is_dynamic = worker is not None

        if is_dynamic:
            if branches_raw:
                raise WorkflowError(
                    f"node {node_id!r}: a dynamic parallel node (worker set) must not also set 'branches'"
                )
            if list_from is None:
                raise WorkflowError(
                    f"node {node_id!r}: a dynamic parallel node needs 'list_from' alongside 'worker'"
                )
            if not isinstance(worker, str) or not worker:
                raise WorkflowError(
                    f"node {node_id!r}: worker must be a non-empty string, got {worker!r}"
                )
            if not isinstance(list_from, str) or not list_from:
                raise WorkflowError(
                    f"node {node_id!r}: list_from must be a non-empty string, got {list_from!r}"
                )
            branches: tuple[str, ...] = ()
        else:
            if not isinstance(branches_raw, list) or not branches_raw:
                raise WorkflowError(
                    f"node {node_id!r}: a parallel node needs a non-empty 'branches' list"
                )
            branches = tuple(branches_raw)

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
        max_concurrency = raw.get("max_concurrency", 2)
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise WorkflowError(
                f"node {node_id!r}: max_concurrency must be an int >= 1, got {max_concurrency!r}"
            )
        return ParallelNode(
            id=node_id, title=raw.get("title", ""), branches=branches, next=raw.get("next"),
            reconcile=reconcile, criteria=criteria, judge_model=raw.get("judge_model"),
            max_concurrency=max_concurrency, worker=worker, list_from=list_from,
        )
    raise WorkflowError(f"node {node_id!r}: unknown kind {kind!r}")


def _edge_targets(node: Node) -> list[str | None]:
    if isinstance(node, WorkNode):
        if node.cases is not None:
            return list(node.cases.values())
        if node.routes:
            return [node.on_pass, node.on_fail]
        return [node.next]
    if isinstance(node, SubworkflowNode):
        return [node.next]
    if isinstance(node, ParallelNode):
        targets: list[str | None] = [*node.branches, node.next]
        if node.worker is not None:
            targets.append(node.worker)
        if node.list_from is not None:
            targets.append(node.list_from)
        return targets
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
            if target is not None and target != NEEDS_INPUT_TARGET and target not in nodes:
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

    for node in nodes.values():
        if isinstance(node, ParallelNode):
            for ref in (*node.branches, node.worker):
                target = nodes.get(ref) if ref else None
                if isinstance(target, WorkNode) and target.session == "persistent":
                    raise WorkflowError(
                        f"node {node.id!r}: parallel unit {ref!r} cannot use session='persistent' "
                        f"(concurrent units have per-unit sessions)"
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
        if not (isinstance(j, WorkNode) and j.routes):
            continue
        for pred_id in predecessor_map[j.id]:
            if pred_id == j.id:
                continue
            p = nodes[pred_id]
            if isinstance(p, WorkNode):
                if (p.model, p.mode, p.prompt) == (j.model, j.mode, j.prompt):
                    raise WorkflowError(
                        f"node {j.id!r}: a routing node must not be structurally identical to its "
                        f"producer {p.id!r} (vary model, mode, or prompt for an independent verdict)"
                    )

    max_visits = data.get("max_visits", 3)
    if isinstance(max_visits, bool) or not isinstance(max_visits, int) or max_visits < 1:
        raise WorkflowError(f"max_visits must be an int >= 1, got {max_visits!r}")

    mode = data.get("improvement_mode", "manual")
    if mode not in ("manual", "auto"):
        raise WorkflowError(
            f"improvement_mode must be 'manual' or 'auto', got {mode!r}"
        )

    wf_input = data.get("input")
    if wf_input is not None and not isinstance(wf_input, dict):
        raise WorkflowError(f"workflow 'input' must be a dict or omitted, got {wf_input!r}")
    wf_output = data.get("output")
    if wf_output is not None and not isinstance(wf_output, dict):
        raise WorkflowError(f"workflow 'output' must be a dict or omitted, got {wf_output!r}")

    return Workflow(
        name=name, start=start, nodes=nodes, max_visits=max_visits, improvement_mode=mode,
        input=wf_input, output=wf_output,
        description=data.get("description"),
    )
