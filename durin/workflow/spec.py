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
from pathlib import Path
from typing import Any, Literal, Union

from durin.security.secrets import is_valid_secret_name
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
    """Short name for a node: author title, else its command, else its id.

    The id is the name the author already chose ('consolidate', 'resolve-org'),
    so it reads as a label. The prompt's first sentence does not — it is prose
    that happens to start a paragraph, and it wraps to three lines in a 288px
    panel. That sentence is still useful context: node_description returns it
    for hover text.
    """
    title = (getattr(node, "title", "") or "").strip()
    if title:
        return title
    cmd = (getattr(node, "command", "") or "").strip() or (getattr(node, "script", "") or "").strip()
    if cmd:
        return cmd if len(cmd) <= 80 else cmd[:80].rsplit(" ", 1)[0] + "…"
    return _prettify_id(node.id)


def node_description(node: Any) -> str:
    """The first sentence of a node's role prompt — hover text, not a label."""
    return _first_sentence(getattr(node, "prompt", "") or "")

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
    detached: bool = False               # launch and continue: side-effect node off the critical path
    inputs_from: tuple[str, ...] = ()    # named sources composed (labeled) into this node's input
    output_schema: dict | None = None    # JSON Schema the node's output must satisfy (forced deliver tool)
    output_file: str = ""                # engine-written file (in the working folder) holding the validated payload
    kind: Literal["work"] = "work"

    @property
    def routes(self) -> bool:
        """True when this node emits a verdict and branches: binary (on_pass/on_fail) or multi-way (cases)."""
        return self.on_pass is not None or self.on_fail is not None or self.cases is not None


_AGENT_ONLY_FIELDS = frozenset(
    {"model", "persona", "context", "session", "prompt", "mode", "tools", "skills", "mcps", "max_turns"}
)


@dataclass(frozen=True)
class ScriptNode:
    """A node that runs a deterministic subprocess instead of an agent turn.

    Exactly one of ``command`` (inline, run via bash -c) or ``script`` (a file under
    <workspace>/workflows/scripts/) is set. The upstream edge text arrives on stdin;
    stdout becomes the edge text to the next node. Routing mirrors WorkNode: binary
    (on_pass/on_fail) routes on the exit code (0 = PASS), multi-way (cases) on the
    last non-empty stdout line, and a linear node treats a non-zero exit as a node
    failure (aborting the run) rather than a verdict.
    """

    id: str
    title: str = ""
    command: str = ""
    script: str = ""
    timeout: int | None = None           # seconds; None = the workflow.script_timeout config default
    env: Literal["clean", "inherit"] = "clean"  # "clean" = minimal allowlist + DURIN_*; "inherit" = full gateway env
    secrets: tuple[str, ...] = ()        # stored secret names injected into the subprocess env (each must allow the 'exec' scope)
    next: str | None = None
    on_pass: str | None = None
    on_fail: str | None = None
    cases: dict[str, str | None] | None = None
    max_visits: int | None = None
    detached: bool = False               # launch and continue: side-effect node off the critical path
    inputs_from: tuple[str, ...] = ()    # named sources composed (labeled) onto this script's stdin
    kind: Literal["script"] = "script"

    @property
    def routes(self) -> bool:
        """True when this node emits a verdict and branches (binary or multi-way)."""
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

    Static mode: ``branches`` is non-empty, ``worker``/``list_from``/``branches_from``
    are None.
    Dynamic mode: ``worker`` names a work node to run per item; ``list_from`` names
    the upstream node whose output is parsed as the runtime list; ``branches`` is
    empty.
    Runtime-selected mode: ``branches_from`` names the node whose output lists the
    DECLARED work-node ids to run this pass (a routing script emitting a JSON array
    or comma-separated ids) — heterogeneous branches chosen per run, without one
    static parallel node per branch combination.
    Bounded by ``max_concurrency`` in every mode.
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
    branches_from: str | None = None     # runtime-selected mode: node whose output names the branch ids
    kind: Literal["parallel"] = "parallel"


Node = Union[WorkNode, ScriptNode, SubworkflowNode, ParallelNode]


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


def _parse_output_contract(raw: dict[str, Any], node_id: str, *, routes: bool) -> tuple[dict | None, str]:
    """Validate ``output_schema`` (a JSON Schema the runner enforces via a forced
    deliver tool) and ``output_file`` (an engine-written file holding the validated
    payload). A routing node's structured surface is its verdict tool — the two are
    mutually exclusive; a tool-parameter schema root must be an object."""
    schema = raw.get("output_schema")
    output_file = raw.get("output_file", "")
    if schema is not None:
        if not isinstance(schema, dict):
            raise WorkflowError(
                f"node {node_id!r}: output_schema must be a JSON Schema object, got {schema!r}"
            )
        if routes:
            raise WorkflowError(
                f"node {node_id!r}: output_schema cannot be combined with routing "
                "(a routing node's structured output is its verdict)"
            )
        try:
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001 - surface the schema error verbatim
            raise WorkflowError(
                f"node {node_id!r}: output_schema is not a valid JSON Schema: {exc}"
            ) from exc
    if output_file:
        if not isinstance(output_file, str):
            raise WorkflowError(
                f"node {node_id!r}: output_file must be a string, got {output_file!r}"
            )
        if schema is None:
            raise WorkflowError(
                f"node {node_id!r}: output_file requires output_schema (the engine writes "
                "the VALIDATED payload — without a schema there is nothing validated to write)"
            )
        pf = Path(output_file)
        if pf.is_absolute() or ".." in pf.parts:
            raise WorkflowError(
                f"node {node_id!r}: output_file must be a relative path inside the "
                f"working folder, got {output_file!r}"
            )
    return schema, output_file or ""


def _parse_detached(raw: dict[str, Any], node_id: str, *, routes: bool) -> bool:
    """Validate the shared ``detached`` flag of a work/script node. A detached node
    is launched and the walk continues immediately — its output never rides an edge,
    so a verdict from it could never route: detached and routing are incompatible."""
    detached = raw.get("detached", False)
    if not isinstance(detached, bool):
        raise WorkflowError(
            f"node {node_id!r}: detached must be true or false, got {detached!r}"
        )
    if detached and routes:
        raise WorkflowError(
            f"node {node_id!r}: a detached node cannot route (its verdict never "
            "reaches the walk) — drop 'on_pass'/'on_fail'/'cases' or 'detached'"
        )
    return detached


def _parse_routing(raw: dict[str, Any], node_id: str) -> tuple[str | None, str | None, str | None, dict[str, str | None] | None]:
    """Validate the shared routing fields of a work/script node: returns
    (next, on_pass, on_fail, cases) after enforcing cases well-formedness,
    label-normalization uniqueness, and next/binary/cases mutual exclusivity."""
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
    return next_node, on_pass, on_fail, cases


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
        inputs_from = _str_list(raw.get("inputs_from", []), node_id, "inputs_from")
        next_node, on_pass, on_fail, cases = _parse_routing(raw, node_id)
        routes = on_pass is not None or on_fail is not None or cases is not None
        detached = _parse_detached(raw, node_id, routes=routes)
        output_schema, output_file = _parse_output_contract(raw, node_id, routes=routes)
        # A detached node runs beside the walk; the shared buffer is a sequential
        # continuity mechanism and a concurrent writer would race it.
        if detached and context == "shared":
            raise WorkflowError(
                f"node {node_id!r}: a detached node cannot use context='shared'"
            )
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
            detached=detached,
            inputs_from=inputs_from,
            output_schema=output_schema,
            output_file=output_file,
        )
    if kind == "script":
        command = raw.get("command", "")
        script = raw.get("script", "")
        if not isinstance(command, str) or not isinstance(script, str):
            raise WorkflowError(f"node {node_id!r}: 'command' and 'script' must be strings")
        if bool(command.strip()) == bool(script.strip()):
            raise WorkflowError(
                f"node {node_id!r}: a script node needs exactly one of 'command' (inline) or 'script' (file)"
            )
        script = script.strip()
        if script and (Path(script).is_absolute() or ".." in Path(script).parts):
            raise WorkflowError(
                f"node {node_id!r}: 'script' must be a relative path inside workflows/scripts (no '..')"
            )
        if script:
            # Normalize path aliases ("./x.sh", "a/./b") to one canonical form, so
            # every consumer comparing script references by string ("which nodes run
            # this file?") cannot be evaded by an equivalent spelling.
            from pathlib import PurePosixPath
            script = str(PurePosixPath(script))
        rejected = sorted(_AGENT_ONLY_FIELDS & raw.keys())
        if rejected:
            raise WorkflowError(
                f"node {node_id!r}: field(s) {', '.join(rejected)} do not apply to a script node"
            )
        timeout = raw.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
                raise WorkflowError(f"node {node_id!r}: timeout must be an int >= 1, got {timeout!r}")
        env = raw.get("env", "clean")
        if env not in ("clean", "inherit"):
            raise WorkflowError(f"node {node_id!r}: env must be 'clean' or 'inherit', got {env!r}")
        secrets = _str_list(raw.get("secrets", []), node_id, "secrets")
        bad = [s for s in secrets if not is_valid_secret_name(s)]
        if bad:
            raise WorkflowError(
                f"node {node_id!r}: secrets must be env-var-safe names "
                f"(A-Z, 0-9, _; starting with a letter), got: {', '.join(bad)}"
            )
        node_max_visits = raw.get("max_visits")
        if node_max_visits is not None:
            if isinstance(node_max_visits, bool) or not isinstance(node_max_visits, int) or node_max_visits < 1:
                raise WorkflowError(
                    f"node {node_id!r}: max_visits must be an int >= 1, got {node_max_visits!r}"
                )
        next_node, on_pass, on_fail, cases = _parse_routing(raw, node_id)
        script_routes = on_pass is not None or on_fail is not None or cases is not None
        detached = _parse_detached(raw, node_id, routes=script_routes)
        script_inputs_from = _str_list(raw.get("inputs_from", []), node_id, "inputs_from")
        return ScriptNode(
            id=node_id, title=raw.get("title", ""), command=command.strip(), script=script,
            timeout=timeout, env=env, secrets=secrets, next=next_node, on_pass=on_pass,
            on_fail=on_fail, cases=cases, max_visits=node_max_visits, detached=detached,
            inputs_from=script_inputs_from,
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
        branches_from = raw.get("branches_from")
        branches_raw = raw.get("branches", [])
        is_dynamic = worker is not None

        if branches_from is not None:
            # Runtime-selected branches: exactly one mode per parallel node.
            if branches_raw:
                raise WorkflowError(
                    f"node {node_id!r}: 'branches_from' must not also set static 'branches'"
                )
            if is_dynamic or list_from is not None:
                raise WorkflowError(
                    f"node {node_id!r}: 'branches_from' must not be combined with "
                    "'worker'/'list_from' (pick one parallel mode)"
                )
            if not isinstance(branches_from, str) or not branches_from:
                raise WorkflowError(
                    f"node {node_id!r}: branches_from must be a non-empty string, got {branches_from!r}"
                )
            branches: tuple[str, ...] = ()
        elif is_dynamic:
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
            branches = ()
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
            branches_from=branches_from,
        )
    raise WorkflowError(f"node {node_id!r}: unknown kind {kind!r}")


def _edge_targets(node: Node) -> list[str | None]:
    if isinstance(node, (WorkNode, ScriptNode)):
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


def _validate_artifacts(raw: Any) -> None:
    """Validate ``output.artifacts`` — the workflow's declared file contract: a list
    of ``{path, description?}``. Paths are relative to the run's working folder; the
    engine checks them post-run and reports the missing ones as a warning (never a
    failure), so a composed stage learns immediately which promised file is absent
    instead of failing confusingly downstream."""
    if not isinstance(raw, list):
        raise WorkflowError(f"output 'artifacts' must be a list, got {raw!r}")
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowError(f"each output artifact must be a dict, got {item!r}")
        path = item.get("path")
        if not path or not isinstance(path, str):
            raise WorkflowError(f"output artifact needs a non-empty string 'path', got {item!r}")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise WorkflowError(
                f"output artifact path must be relative inside the working folder (no '..'), got {path!r}")
        desc = item.get("description")
        if desc is not None and not isinstance(desc, str):
            raise WorkflowError(f"output artifact 'description' must be a string, got {desc!r}")
        if path in seen:
            raise WorkflowError(f"duplicate output artifact path {path!r}")
        seen.add(path)


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
                # Work AND script nodes may run as branches — a deterministic fetch
                # beside an LLM analysis is the whole point of mixing them. Parallel
                # and subworkflow nodes stay rejected (a branch is one execution unit).
                if not isinstance(nodes[branch], (WorkNode, ScriptNode)):
                    raise WorkflowError(
                        f"node {node.id!r}: parallel branch {branch!r} must be a work or script node"
                    )
            if node.worker is not None and not isinstance(nodes[node.worker], WorkNode):
                raise WorkflowError(
                    f"node {node.id!r}: parallel worker {node.worker!r} must be a work node"
                )
            if node.branches_from is not None and node.branches_from not in nodes:
                raise WorkflowError(
                    f"node {node.id!r}: branches_from references unknown node {node.branches_from!r}"
                )
            for ref in (*node.branches, node.worker):
                target = nodes.get(ref) if ref else None
                if target is not None and getattr(target, "detached", False):
                    raise WorkflowError(
                        f"node {node.id!r}: parallel unit {ref!r} cannot be detached "
                        "(branches already run concurrently and their outputs merge)"
                    )

    # inputs_from sources must be real nodes, not the node itself, and not detached
    # (a detached node's output never rides an edge — there is nothing to compose).
    for node in nodes.values():
        for src in getattr(node, "inputs_from", ()):
            if src == node.id:
                raise WorkflowError(
                    f"node {node.id!r}: inputs_from cannot reference itself"
                )
            target = nodes.get(src)
            if target is None:
                raise WorkflowError(
                    f"node {node.id!r}: inputs_from references unknown node {src!r}"
                )
            if getattr(target, "detached", False):
                raise WorkflowError(
                    f"node {node.id!r}: inputs_from references detached node {src!r} "
                    "(a detached node's output never rides an edge)"
                )

    # A detached node may only be reached by a linear `next` edge: a routing edge
    # into one is undefined (a loop-back could re-enter a node that is still
    # running from the previous launch).
    for node in nodes.values():
        if isinstance(node, (WorkNode, ScriptNode)) and node.routes:
            targets = list(node.cases.values()) if node.cases is not None else [node.on_pass, node.on_fail]
            for target in targets:
                t = nodes.get(target) if target else None
                if t is not None and getattr(t, "detached", False):
                    raise WorkflowError(
                        f"node {node.id!r}: routing target {target!r} is detached — "
                        "a detached node may only be reached by a linear 'next' edge"
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
    if wf_output is not None and wf_output.get("artifacts") is not None:
        _validate_artifacts(wf_output["artifacts"])

    return Workflow(
        name=name, start=start, nodes=nodes, max_visits=max_visits, improvement_mode=mode,
        input=wf_input, output=wf_output,
        description=data.get("description"),
    )
