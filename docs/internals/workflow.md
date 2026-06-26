# Workflow engine — User-defined flow graphs

## 1. Purpose

The workflow engine lets a user define a multi-step process as a **flow graph** and
run a task through it. Instead of a single agent turn, a task moves through a graph
of **nodes** the user draws. A node does a piece of the task — a real agent turn with
its own model, tools, and session — and **optionally routes** the
flow (continue, branch, or loop back) on a pass/fail verdict. Routing is opt-in, not a
separate node type. The graph is a plain JSON
document under `<workspace>/workflows/<name>.json`, so it can be authored by a human,
a UI, or an agent; the agent runs one with the `run_workflow` tool.

The engine is **deterministic**: the user's graph drives routing; the LLM does the
work *inside* nodes, it does not decide the path. It runs *above* `AgentRunner` (the
core agent loop is untouched) and reuses the session-lineage primitive, so every
node's work is a persisted, searchable session rather than ephemeral state.

## 2. Mental model

**A workflow is a graph of nodes, not a fixed pipeline.** A `Workflow`
(`durin/workflow/spec.py`) is a set of nodes keyed by id, a `start` node id, and a
per-node visit cap (`max_visits`). There is **one node type** (`WorkNode`): it carries a
model (or the default), a context policy (`own` vs `shared` session), a tool set (`none`
vs `default`), a prompt, optional skills/MCP, and either a single `next` edge or — when it
**routes** — a pair of targets (`on_pass`, `on_fail`). A `None` target ends the run. The parser
validates that the start and every edge target name a real node, and that `next` and
routing are not both set. (`kind: "decision"` is accepted as a back-compat alias for a
routing node — `criteria` maps to the node's `prompt`.)

**Workflow I/O is first-class.** A `Workflow` carries optional `input` (`{text?, file?, description?}`)
and `output` descriptors — rendered as distinct **Input** and **Output** objects on the canvas. The text
input becomes the start node's task; input files are placed in an input folder the start node reads
as its "previous step's files" (reusing the artifact channel). Multiple input files pair naturally
with dynamic fan-out (a worker per file). The terminal node's text output and output folder are
exposed in the run result. Absent ⇒ today's text-task behavior. The optional free-text `description`
is a lightweight contract: the engine frames every node's task with the input description (what the
run received) and the output description (what it must deliver), so the agents are steered and the
interface is documented — it is a hint, not enforced.

**A node runs its body, then optionally routes.** `WorkflowEngine.run`
(`durin/workflow/engine.py`) walks the graph from `start`. For an agent node it calls a
`NodeRunner` — by default `AgentNodeRunner` (`durin/workflow/node_runner.py`), which
runs one `AgentRunner` turn with that node's model and tool registry, then persists
the node's conversation as a session keyed `workflow:<run_id>:<node_id>:<iteration>`
with lineage (`origin_type="workflow_node"`). A node is configured independently and
focused by default. Its **work mode** is an `AgentMode` (`durin/agent/agent_mode.py`) —
`build` (default, full access), `plan`/`explore` (read-only), or a registered custom
mode — that sets the node's posture (a prompt suffix) and filters its tool registry to
what the mode allows, so a read-only node literally cannot write regardless of what the
model attempts. Besides the mode, a node carries its model, context and built-in
`tools`, plus the **skills** to inject into its own prompt (loaded the same way the main
agent loads a skill) and the **MCP servers** whose tools it may use — a scoped subset of
the already-configured servers, reused from the gateway's live connections (no per-node
reconnect; the call is marshalled back to the gateway's event loop, where the MCP
session lives). Skills/MCP default empty, so a node sees only what its job needs. The node's output passes along the edge
as the next node's input. A `shared`-context node reads and extends a running
conversation buffer; an `own`-context node is isolated and receives only the upstream
output. **Output travels two channels.** The **text** of a node's output is the edge — it
becomes the next node's input (above). For **files**, each agent node with file tools is
also given a keyed **output folder** (`<workspace>/.workflow/<run>/<node>/<iteration>/`,
`durin/workflow/artifacts.py`) and is told both where to write its own files and where the
previous step's folder is, so a produced file can be handed onward without the path being
guessed. The folder is keyed by run/node/iteration, so it never collides across loop-back
re-iterations or concurrent/repeated invocations, and it follows the same edge-threading as
the text (a passing judge's empty folder never replaces the producer's). The `.workflow` tree
gitignores itself, is excluded from parallel-fork reconciliation, and is pruned to recent
runs. (Real deliverables a node writes into the workspace proper are the separate,
already-shared filesystem channel — unchanged.) **When a node routes**, the engine derives a verdict from what the node produced
and follows an edge. A node may route in one of two shapes:

**Binary routing** (`on_pass`/`on_fail`): a routing node ends its own reply with a `PASS`/`FAIL`
line the engine parses (`durin/workflow/verdict.py`) — so a routing node can *verify* (read
the diff, run the tests) before ruling, not just read text. The engine routes to `on_pass` or
`on_fail`; on a fail the node's feedback is threaded into the loop-back so the producer re-runs
knowing what to fix.

**Multi-way routing** (`cases`): an agent node declares a set of labeled outcomes
(`{"GROUNDED": null, "MISSING": "plan", "MISUSED": "synthesize"}`). It ends its reply with
exactly one label; the engine matches the last non-empty line of the output against the declared
labels (case-insensitive, surrounding punctuation tolerated), then follows the matching edge.
A `null` target ends the run; any other target is a node id. If the output matches no label the
engine tries a `"default"` key — if that is also absent, the run ends as `aborted` naming the
node and the sorted list of expected labels. Like binary fail-edges, the node's output is
threaded as reviewer feedback before routing to a non-terminal target. The matched label is
recorded in the `NodeRun` trace (`route_label`); `passed` is `None` (pass/fail does not apply).
Binary `on_pass`/`on_fail` is the 2-way special case of this pattern; `cases`, `on_pass`/`on_fail`,
and `next` are mutually exclusive. Routing nodes default to **explore** (read-only) mode.

**Independence is a graph rule, not a node type:** the
parser rejects a routing agent node that is *structurally identical* (same model, mode,
and prompt) to the producer feeding it, so a quality verdict comes from a genuinely
independent reviewer (the anti-Goodhart guard). A node can also be a **sub-workflow**
(`durin/workflow/subworkflow.py`): it runs another named workflow as a nested run
(reusing the same node and branch-pick runners, bounded by three recursion layers) and uses its output;
the nested run carries the same root session key, so its node sessions anchor to the
invoking conversation too. The three layers are: (1) the editor excludes cycle-creating
targets from the sub-workflow picker, so a cycle cannot be authored in the UI; (2) the
runner maintains a call-stack of workflow names currently executing — if a name is about
to reenter the chain, it stops immediately with a cycle error (`"Error: workflow cycle
detected: A -> B -> A"`) rather than recursing; (3) a `max_depth` counter is the backstop
for deep non-cyclic chains, returning an error at the limit.
**A node's "runs as" is a single choice:** either a specific model (or omitted ⇒ default) or
a **Persona** (a named SOUL + its model, mutually exclusive with `model`). Setting `persona`
on a node injects the SOUL body into the node's system prompt and selects the persona's model.
The persona is resolved via `durin/workflow/persona_resolve.py` (shared with the agent loop).

A **parallel** node runs branches concurrently and merges their text outputs into the next
node's input. It has two shapes — **static** (a fixed `branches` list of work-node ids, each
with its own prompt, all seeing the same input) and **dynamic** (a `worker` template node +
`list_from` pointing to the upstream node whose output is parsed as a runtime list: one worker
per item, each getting its own list item as input; the list is emitted by the upstream node as
a JSON array, with a newline-split fallback). **`max_concurrency`** (default 2) bounds both
shapes — at most this many runners execute simultaneously; excess items queue and run in
waves (anti-rate-limit backpressure). Fan-in collects all branch/worker text outputs into the
`next` node's input. For **static** branches, `reconcile` decides how branch *writes* come
back together (`durin/workflow/workspace_fork.py`): `read` = read/analysis branches, no
writes applied; `choose` = each branch writes in a private copy of the workspace and a judge
picks one to apply, discarding the rest; `union` = apply every branch's writes, aborting on a
genuine conflict (two branches wrote *different* content to the same path — identical
incidental files reconcile cleanly). **Dynamic fan-out workers share the workspace** directly
(no per-worker isolation in v1); they hand their output off to the merge node via text, so
`reconcile` has no effect on a dynamic parallel and is not shown in the editor for that mode. A per-node visit count bounds loop-backs across three tiers (the Airflow/Temporal
shape — a config default, a per-unit override, and a hard cap). Each node's budget is
`min(its own max_visits or the workflow's max_visits, workflow.max_node_visits)`: a
per-node `WorkNode.max_visits` overrides the per-workflow `max_visits` (default 3), and
both are clamped by the global config ceiling `workflow.max_node_visits` (default 25, in
settings) — the runaway backstop no node may exceed. Exceeding the budget ends the run
with status `exhausted` carrying `exhausted_node`; the `run_workflow` tool and the editor's
runner surface it gracefully (the node, its last FAIL reason, and the best partial), so the
caller learns it did not complete and why instead of treating a partial as done.

**`max_turns`** (distinct from `max_visits`) caps how many tool-use rounds the model gets
within a single node execution. When set on a `WorkNode`, the node runner (1) prepends a
budget note to the node's system prompt ("You
have up to N rounds of tool use. Gather efficiently, then give your final answer."),
(2) runs the agent with `max_iterations = max_turns` instead of the global default, and
(3) if the run ends because the budget was exhausted, makes a second call with no tools
and `max_iterations=1` asking the model to synthesize from what it gathered — so the node
always produces a real answer rather than a canned "max iterations" message. The second
call's messages are appended to the first run's messages and persisted together. If the
first run completes within budget, no second call is made and the path is byte-for-byte
identical to a node without `max_turns`.

**The engine is decoupled from the LLM and runs loop-safe.** The graph walk depends
only on an injected `NodeRunner` callable, so it is fully unit-testable with a mock.
The real runner drives the async `AgentRunner` synchronously per node, so the
`run_workflow` tool runs the whole (synchronous) engine via `asyncio.to_thread` — the
inner `asyncio.run` then executes in a worker thread with no active event loop, which
is valid even though the tool itself runs inside the agent's async tool loop.

## 3. Diagram

```mermaid
flowchart TD
    A([run_workflow name task]) --> B[load_workflow\nworkspace/workflows/name.json]
    B --> C[parse_workflow to Workflow]
    C --> D[WorkflowEngine.run\nvia asyncio.to_thread]
    D --> MANIFEST_START[start_run manifest\nstatus='running']
    MANIFEST_START --> E[execute node body (agent turn)]
    E --> UPDATE[update_run manifest\nper-node trace]
    UPDATE --> F{node routes?}
    F -->|no / next| G[output becomes\nnext upstream_output]
    G --> H{next is None?}
    H -->|no| E
    H -->|yes| Z[WorkflowResult completed\nfinalize_run manifest]
    F -->|binary on_pass/on_fail| I[parse_verdict\nfirst non-empty line]
    I --> J{passed?}
    J -->|yes| K[route on_pass]
    J -->|no| L[route on_fail / loop back\nthread reviewer feedback]
    K --> H
    L --> H
    F -->|multi-way cases| MW[parse_label\nlast matching line from end]
    MW --> MWT{label target?}
    MWT -->|node id| E
    MWT -->|null / none| Z
    MWT -->|no match + no default| ABORT[WorkflowResult aborted\nfinalize_run manifest]
    E -->|visits over budget| Y[WorkflowResult exhausted\nfinalize_run manifest]
    E -->|agent turn raises| FAIL[persist partial session\nNodeRun node_failed\nWorkflowResult aborted]
    E -->|parallel node| PAR[run branches/workers\nconcurrently]
    PAR --> |all workers fail| ABORT
    PAR --> MERGE[merge outputs\nper-worker NodeRuns]
    MERGE --> G
    E -->|subworkflow node| SUB[SubworkflowRunner\nnested run\ndepth-capped]
    SUB --> G
```

## 4. Run auditability

### 4a. The run manifest

Every run with a workspace produces a durable **run manifest** at
`<workspace>/workflows-runs/<name>/<run_id>.json`. The manifest is a live record, not
a post-run summary:

1. **Before the walk** — `start_run` writes `{status: "running", root_session_key, started_at, runs: []}`.
2. **After each node** — `update_run` rewrites the file with the accumulated per-node trace and `status: "running"`, so an in-flight run is observable by reading the file.
3. **On every exit path** (normal completion, exhaustion, abort, or config error) — `finalize_run` writes the terminal status (`completed`/`exhausted`/`aborted`), `finished_at`, and the full trace.

Each file is keyed by `run_id` and owned by a single writer, so full-file rewrites per update are safe with no RMW lock. Manifest writes are best-effort — a write failure is logged but never interrupts the run.

The per-node entries in the manifest's `runs` array carry:

| Field | Meaning |
|---|---|
| `node_id` | the node's id in the graph |
| `iteration` | how many times this node has executed (loop-back counting) |
| `session_key` | the persisted session containing the node's conversation (`workflow:<run_id>:<node_id>:<iteration>`, or with a `:<worker_index>` suffix for fan-out workers) |
| `worker_index` | fan-out worker index (0-based; `null` for non-fan-out nodes) |
| `branch_id` | static-parallel branch node id (`null` for non-branch nodes) |
| `status` | `"ok"` / `"persist_failed"` (save raised) / `"node_failed"` (agent turn raised) |
| `passed` | binary routing verdict (`true`/`false`/`null` for non-binary nodes) |
| `route_label` | matched case label for multi-way nodes (`null` otherwise) |

`read_runs_since` (used by the dream self-improvement pass) returns all records for a
workflow; callers that need only terminal runs should skip records whose `status` is
`"running"` or `"crashed"`.

**Crash reconciliation.** A `running` manifest whose `started_at` is older than a generous
threshold can only be a run whose process died before finalizing. The gateway's startup
sweep (`reconcile_running`) rewrites any such record's status to `"crashed"` (preserving
its partial trace) so an auditor sees a truthful status rather than a permanently stale
`running`. The threshold is deliberately generous; real runs finalize fast.

### 4b. The session invariant

The engine never shares the calling session with a node. Every unit of work — every
agent node, every fan-out worker, every static branch — runs in its own fresh session
keyed `workflow:<run_id>:<node_id>:<iteration>` (plus `:<worker_index>` for fan-out
workers), with exactly one recorded parent.

**Reverse lineage** (child → parent): each node session carries a `parent_session_id`
pointing to the calling session. **Forward reference** (caller → node sessions): the
run manifest records each node's `session_key`, so you can reach every session a run
produced directly from the manifest.

**No orphan sessions.** Service and cron runs have no calling session. Rather than
letting each node session self-root (producing orphans that `children_of` cannot
find), the engine synthesizes a per-run root: all node sessions of a headless run share
`parent_session_id = workflow:<run_id>:root`. That stub session (`origin_type="workflow_run"`) is
created once on first use, so `children_of("workflow:<run_id>:root")` returns every
node session of the run. The manifest's `root_session_key` records the same key, so
`runs_for_session` can find the run from either the calling session or the synthetic root.

### 4c. The failure model

**Failed node.** When a node's agent turn raises (provider/MCP/tool error), the node
runner persists whatever conversation messages existed under the node's session key with
status `node_failed`, then raises a typed `NodeExecutionError` carrying the `node_id`,
`iteration`, and persisted `session_key`. The engine catches this, appends a
`NodeRun(status="node_failed", session_key=..., error=...)` to the trace (so the
manifest captures it), then returns a `WorkflowResult(status="aborted")` that names the
failing node (`failed_node`, `failed_iteration`). The failed node's session remains
navigable in full.

**Parallel node failure isolation.** For dynamic fan-out workers and `read`-reconcile
static branches, a single worker/branch failure is isolated: the failing unit records
its own `node_failed` NodeRun (with its `session_key` and `error`), surviving units
complete normally, and their outputs are merged (failed units appear as `FAILED: …` in
the merged output). The run is only aborted when every unit failed. **`choose` and
`union` reconcile are deliberately not isolated**: these branches write to private
workspace forks and a half-failed fork has no coherent state to merge, so a branch
failure in those modes propagates and aborts the run.

`NodeRun.status` values: `"ok"` (node persisted), `"persist_failed"` (session save raised
but the run continued), `"node_failed"` (the node's agent turn raised).

### 4d. Verdict asymmetry

Binary and multi-way routing read the agent's output differently:

- **`parse_verdict` (binary)** reads the **first non-empty line** of the output and returns
  `True` iff it starts with `PASS` (case-insensitive). Default is `False` (FAIL) — an
  empty output or an unparseable answer loops back, never silently passes.
- **`parse_label` (multi-way)** scans lines **from the end** for a line whose full
  stripped, de-punctuated text equals one of the declared case labels (case-insensitive).
  It returns the **last** matching line.

**Practical implication:** a binary routing node must put its `PASS`/`FAIL` verdict on
the very first line of its reply. Placing it at the end of a long response causes
`parse_verdict` to read an earlier content line and default to `FAIL`, looping back
unexpectedly. Multi-way `parse_label` has the opposite convention — it reads from the
end — so a label buried mid-reply is ignored in favour of the last matching line.

### 4e. Context vs. session

`context` controls what a node **sees**, not where its work is stored.

- **`own` (default):** the node receives only the upstream edge output as its user
  message. Its work is always its own fresh session regardless.
- **`shared`:** the node additionally receives a running buffer of all preceding
  `shared` nodes' conversation turns as extra context before its user message, and its
  own turns are appended to that buffer for subsequent `shared` nodes. The buffer is
  capped to bound prompt growth on long shared chains.

**`shared` is sequential-only.** Parallel workers are always isolated (each gets its
own session; there is no shared buffer across concurrent threads), and only their text
outputs are merged (`[0] … [1] …`). A routing node may not use `context="shared"`;
the spec rejects that combination at parse time because a routing node reads its own
output to derive a verdict, not the shared buffer.

### 4f. HTTP surface for run data

The `WorkflowsService` exposes read routes for run manifests:

| Route | What it returns |
|---|---|
| `GET /api/v1/workflows/{name}/runs/{run_id}` | one run's manifest (live or terminal) |
| `GET /api/v1/workflows/runs?session=<key>` | all run manifests whose `root_session_key` matches, newest-first |

The `POST /api/v1/workflows/{name}/run` response carries `run_id` and a per-node trace
with `session_key`, `worker_index`, `status`, and `route_label` for each entry.

## 5. How it works

End-to-end for a single `run_workflow` call:

1. **Load.** `RunWorkflowTool.execute` (`durin/agent/tools/run_workflow.py`) loads the
   named definition with `load_workflow` (`durin/workflow/loader.py`), which reads
   `<workspace>/workflows/<name>.json` and parses it with `parse_workflow`. A missing
   file returns an error string (it does not raise).
2. **Wire.** It resolves the user's default model preset
   (`DurinConfig.resolve_default_preset`), builds the provider (`make_provider`), and
   wires `AgentRunner` → `AgentNodeRunner` (passing the user's real `cfg.tools`), an
   `AgentJudgeRunner` (used only to **pick** a winner for parallel `choose`), and a
   `SubworkflowRunner` (for sub-workflow nodes) into the `WorkflowEngine`.
3. **Run.** The engine runs under `asyncio.to_thread`. It walks the graph: a node runs
   its body (an agent turn — persisting a lineage'd node session) and its output threads
   to the next node; a **routing** node branches on the `PASS`/`FAIL` verdict in its own
   agent output (threading the feedback into the loop-back on fail); a sub-workflow node
   runs a nested workflow; a failed gate loops back, re-running the target node as the
   next iteration (a sibling node session), capped by `max_visits`.
4. **Return.** The run produces a typed `WorkflowResult` (status + final output +
   per-node trace, including each node's `session_key`). The engine finalizes the run
   manifest before returning; neither the tool nor the service writes an additional
   record. The tool formats the result into a short summary (each node's session key is
   included for agent and routing nodes). The node sessions persist on disk, so the
   run's work is navigable, searchable, and visible to the dream memory passes — the
   same way subagent and cron per-run sessions are (see [cron.md](cron.md),
   [memory/00_overview.md](memory/00_overview.md)). See §4 for how to navigate the run
   from its manifest.

## 6. Key types & entry points

| Symbol | File | Role |
|---|---|---|
| `Workflow`, `WorkNode`, `SubworkflowNode`, `ParallelNode`, `parse_workflow` | `durin/workflow/spec.py` | The flow-graph definition and its JSON parser/validator (one node type; routing optional; `kind:"decision"` back-compat alias; structural-equivalence guard). |
| `parse_verdict`, `parse_label` | `durin/workflow/verdict.py` | The verdict contracts: `parse_verdict` returns the binary `PASS`/`FAIL` from a routing agent node's output (default `FAIL`); `parse_label` matches the last non-empty line of a multi-way node's output against the declared case labels (case-insensitive, punctuation-tolerant). |
| `artifact_dir`, `prune_runs` | `durin/workflow/artifacts.py` | The per-node file hand-off folder keyed by run/node/iteration (self-gitignored, pruned to recent runs). |
| `AgentJudgeRunner` | `durin/workflow/judge.py` | The branch-pick reviewer: `pick` chooses the best of N outputs for a parallel `choose` reconcile. |
| `fork`, `diff`, `conflicts`, `apply` | `durin/workflow/workspace_fork.py` | Per-branch workspace isolation + reconciliation (choose/union) for writing-in-parallel. |
| `WorkflowVersionStore` | `durin/workflow/version_store.py` | Git versioning of workflow definitions: each run snapshots them; `history` reads the timeline. |
| `SubworkflowRunner` | `durin/workflow/subworkflow.py` | Runs a named workflow as a nested run (depth-capped) for a sub-workflow node. |
| `WorkflowEngine` | `durin/workflow/engine.py` | The graph executor: routing, loop-back with a visit cap, own/shared context, output threading, and concurrent parallel branches. |
| `AgentNodeRunner` | `durin/workflow/node_runner.py` | The default node runner: one real `AgentRunner` turn per agent node (adds a verdict instruction when the node routes), persisted as a lineage'd node session. |
| `seed_workflows` | `durin/utils/helpers.py` | Copy bundled seed JSONs into a workspace's `workflows/` dir (idempotent, called from `sync_workspace_templates`). |
| `load_workflow` | `durin/workflow/loader.py` | Load and parse a workflow by name from the workspace. |
| `WorkflowResult`, `NodeRun` | `durin/workflow/result.py` | The typed run outcome and per-node trace. |
| `RunWorkflowTool` | `durin/agent/tools/run_workflow.py` | The `run_workflow` LLM tool (core scope) that loads, runs, summarizes a workflow, and records its run. |
| `start_run`, `update_run`, `finalize_run`, `read_manifest`, `runs_for_session`, `reconcile_running`, `read_runs_since` | `durin/workflow/run_log.py` | The live run manifest (running→terminal), per-run diagnostic records, crash reconciliation, and the self-improvement signal source. `read_runs_since` callers that need terminal runs should skip records with `status in {"running","crashed"}`. |
| `NodeExecutionError` | `durin/workflow/engine.py` | Typed error raised by the node runner when an agent turn fails; carries `node_id`, `iteration`, and `session_key` so the engine can record an attributable `NodeRun` before aborting. |
| `compute_diagnostics` | `durin/workflow/diagnostics.py` | Reduces run records to recurring per-node trouble (loop-backs, gate fails) → improvement candidates. |
| `run_workflow_improve_pass` | `durin/workflow/workflow_improve_dream.py` | The dream pass: observes manual-mode workflows, proposes one scoped edit, records a recommendation. |
| `log_recommendation`, `open_recommendations` | `durin/workflow/workflow_recommendations.py` | The per-workflow recommendation queue (manual mode). |

## 7. Configuration & surfaces

- **Definitions** live as JSON under `<workspace>/workflows/<name>.json`. That
  directory is a small local git repo (`durin/workflow/version_store.py`, via the
  shared `GitRepo`); every run snapshots the current definitions, so there is a
  navigable version history of how each workflow changed and which version a run used.
- **Surface:** the `run_workflow(name, task)` LLM tool — auto-discovered into the
  agent's tool registry at core scope (see [tools.md](tools.md)). A node with
  `tools: "default"` receives the user's configured tool set; `tools: "none"` (the
  default) runs the node without tools. A node may also name `skills` (injected into
  its prompt) and `mcps` (a subset of the configured MCP servers, reused live).
- **Management API:** `WorkflowsService` (`durin/service/workflows.py`) exposes, over HTTP
  at `/api/v1/workflows[/{name}]` and in the OpenAPI contract: list / load / save / delete
  (save validates via `parse_workflow` and writes atomically under the version lock — the
  same lock target the version store snapshots under, beside the dir, so a write and a
  snapshot never interleave); **run** (`…/{name}/run` — executes the workflow on a task and
  returns the per-node trace); and the **recommendations** queue (`…/recommendations`,
  `…/recommendations/{id}/apply`). This is the surface the webui visual editor uses.
- **Lineage:** node sessions reuse the lineage metadata on the open session document
  (`durin/session/lineage.py`), so no schema migration is involved.
- **Self-improvement** (per-workflow `improvement_mode`: `off` default / `manual` /
  `auto`). Each run writes a diagnostic record (`run_log.py`, beside `workflows/`). A
  dream pass (`run_workflow_improve_pass`, wired into the `memory_dream` cron) reduces
  those to recurring trouble (a node that loops, a gate that keeps failing —
  `diagnostics.py`), shows a model the definition + that diagnostic + the change history
  (so it never re-proposes a reverted edit), and proposes one scoped edit (a node's
  `prompt` — which doubles as a routing node's criteria; structural edits rejected). In **manual** mode the
  proposal is recorded as a recommendation (`workflow_recommendations.py`); the user
  reviews and applies it — from the webui Workflows pane (a recommendations banner with an
  apply button) or the `durin workflow` CLI (`recommendations` lists open ones,
  `apply <name> <id>`) — which writes the proposed text into the node, versions the edit
  with its reason, and marks it applied; the anti-Goodhart anchor is the human.
  **auto** mode (apply directly, gated by an external validation signal so it can't win
  by loosening gates) is the next slice; the apply step + seam are in place.
- **Seeds.** Starter workflows ship bundled under `durin/templates/workflows/` and
  are copied into a fresh workspace's `workflows/` directory by
  `seed_workflows(workspace)` (called from `sync_workspace_templates`) — idempotent,
  never overwrites a user-edited file.

  | Seed | Pattern |
  |---|---|
  | `research-to-answer` | plan → dynamic fan-out (search × N) → synthesize → verify, a **tolerant** per-claim grounding gate (multi-way: GROUNDED ends — a summary intro and minor gaps are acceptable / MISSING → re-plan / MISUSED → re-synthesize) |
  | `brainstorming` | clarify gate (routes `NEED_INFO` to the reserved `__needs_input__` terminal → asks the caller for info) → frame angles → parallel explore → synthesize a design **spec** |
  | `writing-plans` | intake gate (`__needs_input__` on a thin brief) → draft a step-by-step plan → parallel critique (gaps / risks / verifiability / scope) → revise → a tolerant verifiability gate (GAPS loops back) → a `/build`-ready plan with a `## Verification` section |

  Each is a deliberately small, live-verified exemplar — orchestrator-workers +
  evaluator-optimizer, plus the consultative `needs_input` shape (see "Current scope").
  More seeds are added one trustworthy example at a time, never as stubs.

- **Current scope.** Today: sequential execution with **concurrent parallel** branches —
  static (fixed `branches` list) or **dynamic** (`worker` template mapped over a runtime
  list, bounded by `max_concurrency` default 2); read-only or **writing** with `choose` /
  `union` reconciliation (private copy per branch + content-aware conflict detection);
  per-node **work mode** (build/plan/explore) / **model or persona** (SOUL + model,
  mutually exclusive) / context / tools; **optional routing** in two shapes — **binary**
  (`on_pass`/`on_fail`: `PASS`/`FAIL` verdict from the agent, feedback-threaded loop-back)
  and **multi-way** (`cases`: agent emits one of N declared labels, last-line match,
  `default` fallback, aborts clearly when no label matched), with an anti-Goodhart guard
  that a routing node not be structurally identical to its producer; a multi-way case may
  route to the reserved **`__needs_input__`** terminal, ending the run with status
  `needs_input` and the node's output carrying the questions — the human-in-the-loop lives
  in the agent that invoked the workflow (it asks the user and re-runs with the answers),
  so the engine never blocks for input; **sub-workflow** composition (depth-capped);
  runs **anchored to the invoking session**; **git-versioned definitions** (each run
  snapshots them); **dream-driven self-improvement in manual mode** (recommendations from
  recurring run diagnostics); a **webui Workflows pane** (React Flow) with an editor that
  has clickable Input/Output canvas objects (toggle text and/or files plus a free-text
  description; file input is supplied as paths in the run bar), a palette that adds
  work / parallel / subflow nodes (a routing node is a work node — shown by its pass/fail
  edges, never a separate type), draggable nodes with a persisted layout, a **"runs as"**
  picker (model or persona), body/mode/context/routing config, static and dynamic fan-out
  authoring with a concurrency cap, a subflow target picker that excludes cycle-creating
  workflows, and a recommendations banner. Not yet built — see
  [roadmap.md](../roadmap.md) for direction — auto-mode self-improvement (apply +
  validation anchor) and auto-merge of conflicting parallel writes.
- **Security.** Definitions are local files the user authored, so running their
  commands and tools is equivalent to the user running them directly; importing remote
  or third-party definitions is not supported in this scope (see [security.md](security.md)).
