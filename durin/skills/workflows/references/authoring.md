# Authoring reference — the workflow JSON schema

The complete field surface of a workflow definition. A workflow is one JSON object
(`<workspace>/workflows/<name>.json`) with an **envelope** plus a list of **nodes**.
This file is the field-by-field contract; for worked JSON per capability see
[patterns.md](patterns.md), and for full end-to-end examples read the seeds in
`<workspace>/workflows/*.json`. The authoritative source is the parser
(`durin/workflow/spec.py`) — when this doc and the parser disagree, the parser wins.

## Anatomy

```json
{
  "name": "my-workflow",
  "description": "one line — what it does and when to use it",
  "start": "first-node-id",
  "nodes": [ { "id": "first-node-id", "kind": "work", "...": "..." } ]
}
```

The engine starts at `start` and walks edges (`next`, or routing) until a node's edge
target is `null` (end of run).

## Envelope fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | — | **Required.** Non-empty. |
| `start` | string | — | **Required.** Must be the `id` of a defined node. |
| `nodes` | array | — | **Required.** Non-empty list of node objects (see below). |
| `description` | string | none | One-line discovery hint surfaced by `list_workflows`. Optional but recommended. |
| `input` | object | none | I/O descriptor: `{ "text": bool, "file": bool, "description": str }`. Text input becomes the start node's task; input files land in the run's shared working folder. Provided files are validated before anything runs (missing or same-named files abort with a clear message); declaring `"file": true` and passing none ends the run immediately as `needs_input`, before any node spends a turn. |
| `output` | object | none | Same shape as `input`, plus optional `"artifacts"`: a list of `{ "path": str, "description": str? }` — the files (relative to the run's working folder) the run promises to produce. The paths ride in every node's framing, and after a completed run the engine reports missing ones as a **warning** (the run still completes) in the result, manifest, and `tasks(status)`. The free-text `description` stays a soft contract that frames every node's task — a hint, not enforced. |
| `max_visits` | int ≥ 1 | `3` | Per-node loop cap (a node may run at most this many times across loop-backs). Clamped by the global `workflow.max_node_visits` config ceiling. |
| `improvement_mode` | `"manual"` \| `"auto"` | `"manual"` | Dream self-improvement: `manual` leaves a recommendation to review; `auto` (later slice) applies edits directly. |

## Node kinds

Every node has an `id` (required, unique, non-empty string) and an optional `title`
(human label; falls back to the first sentence of `prompt`, then a prettified id). The
`kind` field selects the node type (default `"work"`).

### `work` — one agent turn (the default kind)

Runs a single agent turn with its own model, tools, and persisted session, then either
follows `next` or **routes** on a verdict.

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | `""` | The node's role/instructions. Empty = act on upstream context only. |
| `model` | string \| null | engine default | A model id, served by the SAME provider the run's engine uses — a bare name never switches provider. For a model on another provider, use `persona`. **Mutually exclusive with `persona`.** |
| `persona` | string \| null | none | A named persona (a SOUL + its model, provider-paired — the way to run a node on a different provider). **Mutually exclusive with `model`.** |
| `mode` | string | `"build"` (`"explore"` if it routes) | AgentMode. Use **`build`** (may write files) or **`read`** (read-only) for nodes. `plan`/`explore` exist but carry interactive framing meant for the main loop and derail a node — avoid them; the seeds use `build`/`read`. A `read` node *cannot* write regardless of what the model attempts. |
| `tools` | `"none"` \| `"default"` | `"none"` | `default` = the background tool set: file read/write/edit (notebooks included), `exec`, `grep`, web search/fetch, memory search + memory writes (`memory_upsert_entity`, `memory_ingest`), `convert_to_markdown`, a bounded `sleep`, and the `interpret_image`/`interpret_audio` bridges when the user has aux models configured. Interactive and orchestration tools (asking the user, channel messages, `spawn`, `run_workflow`) are never included. `none` = no tools — right for pure-reasoning nodes over upstream text. `mode` then subtracts from this set (`read` removes all writes); MCP tools come separately via `mcps`. |
| `context` | `"own"` \| `"shared"` | `"own"` | `own` = sees only the upstream edge output. `shared` = also sees a running buffer of preceding `shared` nodes' turns. **A routing node may not be `shared`.** |
| `session` | `"fresh"` \| `"persistent"` | `"fresh"` | `persistent` = when a loop returns to this node it **resumes its own conversation** (one session across passes; the revisit turn carries only what is new — loop feedback and the pass counter). Use it on looping producers that do incremental work. **Requires `context: "own"`; rejected on parallel branches/workers.** |
| `skills` | array of strings | `[]` | Named skills injected into this node's prompt only. |
| `mcps` | array of strings | `[]` | MCP servers (a subset of already-configured ones) whose tools this node may use. |
| `next` | string \| null | none | Next node id; `null` ends the run. **Mutually exclusive with routing.** |
| `on_pass` / `on_fail` | string \| null | none | **Binary routing.** Setting either makes the node emit a `PASS`/`FAIL` verdict; the engine follows the matching edge. A `null` target ends the run. |
| `cases` | object | none | **Multi-way routing.** `{ "LABEL": "target-node-id-or-null", ... }`. The node ends with one label; the engine follows that edge. |
| `max_visits` | int ≥ 1 \| null | inherit envelope | Per-node loop cap override. |
| `max_turns` | int ≥ 1 \| null | global default | Tool-use rounds budget *within* one execution of this node (distinct from `max_visits`). |
| `max_reentries` | int ≥ 0 | `0` | **Self re-entry.** When the node exhausts `max_turns`, the runner asks it (forced one-call `assess` tool) whether essential work is missing; if so it re-enters its OWN conversation with a fresh `max_turns` budget, up to this many times, before the no-tools synthesis runs. Use on evidence-heavy nodes whose workload varies per run. **Requires `max_turns`.** |
| `reentry_prompt` | string | `""` | Author steering appended to each re-entry turn (e.g. "stop gathering; verify pending claims, then deliver"). Empty = generic engine steer. **Requires `max_reentries` ≥ 1.** |
| `detached` | bool | `false` | **Launch and continue.** The walk starts this node and moves on along `next` immediately; the edge text passes through unchanged, the node's output never becomes the run result, and its failure records `node_failed` without sinking the run. For side effects (persist, notify, archive) off the critical path. Requires linear `next` (no routing, no `context: "shared"`); may not be a parallel unit or a routing target. |
| `inputs_from` | array of strings | `[]` | **Named inputs.** The node's input becomes one labeled block `[source-id]` per named node (its LAST recorded output this run), ALWAYS followed by an `[upstream]` block with the walk's current edge text — loop-back feedback is never lost. A source that has not run composes as `(no output recorded)`. Sources must exist, not be the node itself, not be detached. |
| `output_schema` | object \| null | none | **Structured output.** A JSON Schema (root must be an object; wrap arrays in a single-property object). The runner forces a `deliver` tool call with this schema as its parameters, validates the payload, and retries IMMEDIATELY inside the node with the exact validation error; after the attempts the node fails (failure-resume applies). The node's output is the validated payload as JSON. Mutually exclusive with routing. |
| `output_file` | string | `""` | **Engine-written file** (relative path in the working folder) holding the validated payload — the model never types the file, so it cannot be malformed. Requires `output_schema`. |

**Edge exclusivity — pick exactly one shape:** `next` **xor** `on_pass`/`on_fail` **xor**
`cases`. Setting more than one is a parse error.

**Routing details:**
- A routing node's verdict is a forced tool call (an enum of its own labels), so it is
  deterministic; the node's text is parsed only as a fallback. For that fallback, a binary
  node's `PASS`/`FAIL` should be its first line and a multi-way label its last line.
- On a fail / loop-back edge, the node's feedback is threaded into the target's next run so
  the producer knows what to fix.
- **Loop awareness is automatic:** on a revisit the engine tells the node "Pass X of Y" and
  marks the last allowed pass as FINAL (deliver, don't iterate); a binary gate whose FAIL
  would exhaust the producer's budget is told its verdict is definitive. Set `max_visits` to
  the number of genuine attempts the step deserves — convergence framing comes for free.
- **A terminal routing node contributes its output:** when a gate ends the run, whatever it
  wrote besides the verdict/label line becomes the run's final output (a bare `PASS` leaves
  the producer's output in place).
- **`__needs_input__`** is a reserved `cases` target (not a node id): routing there ends the
  run with status `needs_input`, the node's output carrying questions for the caller. To
  continue, call `run_workflow` again with `resume_run_id=<the run id from the summary>` and
  the answers as `task` — the run resumes at the asking node with the same working folder,
  node sessions, and visit counts.
- **Case labels must be distinct after normalization** (case- and punctuation-insensitive):
  `"PASS"` and `"pass."` collide and are rejected.

### `script` — one deterministic subprocess (zero tokens)

Runs a command or a script file instead of an agent turn. The upstream edge text arrives
on **stdin** (a start-position node receives the run's task instead — the workflow input is
the start node's incoming edge; an upstream that printed nothing yields empty stdin); its
**stdout** (capped) becomes the edge text to the next node; **stderr** is diagnostics only.
It executes with **cwd = the run's shared working folder**, so it reads earlier steps'
files and its writes are visible downstream, and it gets `DURIN_TASK` (capped),
`DURIN_RUN_ID`, `DURIN_NODE_ID`, `DURIN_ITERATION`, `DURIN_WORK_DIR` plus the rest of
its environment per the `env` field below (default: a minimal allowlist, not the full
gateway environment). A script node has no session and never reads the shared-context
buffer (the buffer passes through it untouched).

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"script"` | — | Required to select this node type. |
| `command` | string | `""` | Inline command, run via `bash -c` (pipes and redirects work). **Exactly one of `command` / `script`.** |
| `script` | string | `""` | A file under `<workspace>/workflows/scripts/` (relative path, no `..`): `.py` runs with durin's Python, `.sh` with bash, anything else must be executable with a shebang. Missing file = the run aborts pre-flight, before any node runs. |
| `timeout` | int ≥ 1 \| null | `workflow.script_timeout` config (300s) | On expiry the whole process group is killed and the node fails — a timeout is an error, never a FAIL verdict. |
| `env` | `"clean"` \| `"inherit"` | `"clean"` | `"clean"` = a minimal allowlist (`PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `DURIN_HOME`, only those present) plus `DURIN_*`. `"inherit"` = the full gateway process environment (opt in only if the script needs an ambient var durin doesn't forward). **Neither mode carries stored secrets** — they live in the secret store, not the gateway environment; declare them with `secrets` instead. |
| `secrets` | list of names | `[]` | Stored secrets to inject as env vars (e.g. `["ZENDESK_API_TOKEN"]`). Each must exist in the secret store **and allow the `exec` scope** — an unknown or denied name aborts the run pre-flight, naming the node. Values are redacted out of the node's stdout/stderr before they enter run records. |
| `next` / `on_pass`-`on_fail` / `cases` | — | — | Same three edge shapes and exclusivity as a `work` node. |
| `max_visits` | int ≥ 1 \| null | inherit envelope | Per-node loop cap override. |
| `detached` | bool | `false` | Same launch-and-continue contract as a work node's `detached` (see above); the script's exit code still lands in its trace record. |
| `inputs_from` | array of strings | `[]` | Same named-inputs contract as a work node's `inputs_from` (see above); the composed blocks arrive on the script's stdin. |

**Routing semantics (all deterministic):**
- **Binary** (`on_pass`/`on_fail`): **exit 0 = PASS, non-zero = FAIL**; on FAIL the loop-back
  feedback is the script's output plus its stderr tail and exit code, so the producer knows
  what to fix. `command: "run-my-tests"` as a gate is the canonical use.
- **Multi-way** (`cases`): requires exit 0; the **last non-empty stdout line** is the label.
  A non-zero exit on a `cases` node is a node failure, not a route.
- **Linear** (`next`): exit 0 continues with stdout as the edge; **non-zero aborts the run**
  naming the node and exit code — in a gate a non-zero exit is a verdict, in a linear step
  it is an error.
- Agent-only fields (`model`, `persona`, `prompt`, `mode`, `tools`, `context`, `session`,
  `skills`, `mcps`, `max_turns`, `max_reentries`, `reentry_prompt`) are **rejected** on a script node.

### `parallel` — concurrent branches

Runs branches concurrently and merges their text outputs into the `next` node's input. Three
shapes:

- **Static:** a fixed `branches` list of `work`-node ids, all seeing the same input.
- **Dynamic fan-out:** a `worker` (a `work`-node template) mapped over a runtime list named
  by `list_from` (an upstream node whose output is a JSON array, newline-split as fallback);
  one worker per item.
- **Runtime-selected:** `branches_from` names the node — typically a routing script — whose
  output lists which DECLARED `work`-node ids run this pass (a JSON array like
  `["search-history", "analyze-images"]`, or a comma-separated LAST stdout line like
  `search-history, analyze-images`). Use it when different runs need different branch
  subsets: one parallel node replaces one static block per combination. An id that names an
  undeclared/non-`work`/persistent-session node aborts the run (fix the emitting node); an
  empty list is valid — the node records empty output and the walk continues to `next`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"parallel"` | — | Required to select this node type. |
| `branches` | array of strings | `()` | **Static only.** Non-empty list of branch node ids. Each must be a `work` or `script` node — a deterministic script (fetch, convert, check) may run BESIDE agent branches; its stdin is the parallel's input, its stdout is its branch output, and a non-zero exit fails only that branch. |
| `worker` | string \| null | none | **Dynamic only.** Id of the `work` node used as the per-item template. |
| `list_from` | string \| null | none | **Dynamic only.** Id of the upstream node whose output is the runtime list. Required when `worker` is set. |
| `branches_from` | string \| null | none | **Runtime-selected only.** Id of the node whose output names the branch ids to run this pass. Must reference a declared node. Declare `branches` alongside as the candidate POOL (recommended): resolved ids are validated against it, and the editor draws the candidates connected instead of as floating nodes. |
| `max_concurrency` | int ≥ 1 | unset | Unset (recommended): the GLOBAL per-kind caps govern — LLM branches (work/subworkflow) default 2, script branches default 4, both configurable in Settings → Concurrency (`workflow.parallel_llm_concurrency` / `parallel_script_concurrency`). Script branches never queue behind LLM branches. An explicit int is a uniform override for every branch kind. |
| `reconcile` | `"read"` \| `"choose"` \| `"union"` | `"read"` | How **static** branch *file writes* merge: `read` = no writes applied; `choose` = each branch writes a private copy, a judge picks one; `union` = apply all, abort on a same-path content conflict. Writing branches fork the run's shared working folder (and only it — never the surrounding durin workspace) — a branch starts from the folder's current files and its writes reconcile back into it. (Dynamic workers and `read` branches are handed the shared folder directly; `reconcile` has no effect on dynamic fan-out.) |
| `criteria` | string | `""` | **Required when `reconcile` is `choose`** — how the judge picks the winner. |
| `judge_model` | string \| null | none | Optional model for the `choose` judge. |
| `next` | string \| null | none | Where the merged output flows. |

`worker`/`list_from` and `branches_from` are mutually exclusive; `branches` is the static
mode on its own, and alongside `branches_from` it declares the candidate pool — otherwise one
parallel mode per node.

### `subworkflow` — run another workflow as one step

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `"subworkflow"` | — | Required. |
| `workflow` | string | — | **Required.** Name of the workflow to run as a nested run (depth-capped; cycles rejected). The nested run works in the **parent run's shared working folder**, so files flow through composition in both directions. |
| `next` | string \| null | none | Where the nested run's output flows. |

The child's terminal status **propagates**: `completed` threads its output to `next`;
`needs_input` pauses the parent run resumably at this node (resume re-runs the child with
the answers as its task — its prior files persist in the shared folder); `cancelled`
cancels the parent; `aborted`/`exhausted` (and cycle / depth / missing-name errors) abort
the parent naming the child. A composed pipeline therefore never "completes" past a stage
that did not actually run.

## Validation rules that bite

The parser rejects a definition (with a clear message) when:

- `name`, `start`, or a non-empty `nodes` list is missing; `start` or any edge target names
  a node that does not exist (`__needs_input__` excepted).
- A node sets more than one edge shape (`next` / `on_pass`-`on_fail` / `cases`).
- `model` and `persona` are both set on one node.
- A routing node uses `context: "shared"`.
- `session: "persistent"` is combined with `context: "shared"` (two competing continuity
  mechanisms), or set on a node referenced as a parallel `branches` member or `worker`.
- Two `cases` labels normalize to the same form.
- A `detached` node routes, uses `context: "shared"`, is a parallel branch/worker, or is
  the target of a routing edge (only a linear `next` may reach it).
- An `inputs_from` entry names an unknown node, the node itself, or a detached node; an
  `output_schema` is set on a routing node or is not a valid JSON Schema; an
  `output_file` is absolute / `..`-escaping or set without `output_schema`.
- A `parallel` branch id points to a node that is neither `work` nor `script` (parallel
  and subworkflow nodes cannot be branches), or the dynamic `worker` id points to a
  non-`work` node (the worker template stays agent-only on purpose: a script iterates
  over a list *inside* one execution — `for`, `xargs -P` — so mapping one subprocess per
  item adds nothing; a script node CAN be a static branch beside agent branches, the
  `branches_from` router, or the `list_from` source).
- A `script` node sets both or neither of `command`/`script`, uses an absolute or
  `..`-escaping `script` path, sets any agent-only field, or declares a `secrets` name
  that is not env-var-safe (`A-Z`, `0-9`, `_`, starting with a letter). (Whether each
  secret exists and allows the `exec` scope is checked pre-flight at RUN time, not parse
  time — the store may change between authoring and running.)
- `output.artifacts` is not a list of `{path, description?}` objects, a path is absolute
  or `..`-escaping, or two artifacts declare the same path.
- A `choose`-reconcile parallel node has no `criteria`.
- **Anti-Goodhart guard:** a routing node is *structurally identical* (same `model`, `mode`,
  and `prompt`) to a producer that feeds it. Vary at least one — the verdict must come from a
  genuinely independent reviewer. (Routing nodes default to a read-only mode and producers to
  `build`, so this only fires when you make them identical on purpose. Script gates are
  naturally exempt: an exit code cannot be sycophantic.)
