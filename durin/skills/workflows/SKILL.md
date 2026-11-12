---
name: workflows
description: How durin's workflow engine works and — above all — when a workflow earns its place over a plain prompt. Load before reaching for the run_workflow tool, before authoring or composing a workflow (a JSON flow graph under the workspace), or when deciding whether a multi-step / fan-out / verification task should be a workflow at all. Covers the components (work / script / parallel / subworkflow nodes, routing and loops, the shared working folder, the needs-input escape), how to invoke and author them, the seed workflows that ship, and the value test — reach for a workflow only for coverage-at-scale, independent verification, or determinism; never for synthesis, judgment, discipline, or interactive flows, where a prompt or skill wins.
---

# Workflows — durin's flow-graph engine

A workflow is a user-defined **flow graph of nodes**, run on a task with the `run_workflow`
tool. A node is a real agent turn with its own model, tools, and persisted session — or a
**script node**: a deterministic subprocess (a command or a script file) that costs zero
tokens. The **graph — not the LLM — drives routing** (continue, branch, loop). It runs
*above* the normal agent loop, so every agent node's work is a searchable session.
Definitions are JSON under `<workspace>/workflows/<name>.json`, so a human, you, or the
web editor can author one.

## When a workflow earns its place — the value test

**Default to a single good prompt.** A workflow only beats one when the task has a
structural property that a single context handles badly — at least one of these:

- **Coverage / breadth beyond one context** — fan out a worker over many *independent*
  items (sources, documents, review lenses, plan steps). Each worker gets a fresh, focused
  context; a single turn dilutes its attention across all of them. This is the strongest reason.
- **Independent verification** — a separate node checks the producer's output. Producer ≠
  checker catches what self-review is blind to.
- **Determinism** — a step that must run identically every time, or a real command whose
  result a prompt could otherwise hallucinate (run the tests, validate the schema). Give
  such a step a **`script` node**, not an agent told to run a command: the exit code IS
  the verdict, it cannot drift, and it spends no tokens.

If none of these apply, a prompt — or a skill — does it better, faster, and cheaper.

## When NOT to use a workflow

- **Synthesis / judgment** — when the answer needs *combining or weighing* the pieces
  together (rank competing options on interdependent evidence, reach one coherent
  conclusion, diagnose a root cause). Subdividing into isolated workers **breaks** the
  reasoning, because each worker can't see what another holds. A holistic prompt wins.
  *Splitting independent things is fine; splitting reasoning that must stay whole is not.*
- **Discipline** — "do X, then Y, then verify." That is a sequence of instructions → a
  **skill** captures it, and a capable agent already follows it without a graph.
- **Interactive / conversational** flows — a workflow runs as a batch; its only way to ask
  the user is the `__needs_input__` terminal. Planning-with-the-user or exploring-with-the-user
  belongs in the normal conversation, not a workflow.
- **Small instances** — coverage value scales with the number of items. For a handful, a
  prompt matches the workflow without the orchestration overhead — especially since one
  turn can already fan tool calls out in parallel (batch `web_fetch`, parallel reads).
  Reach for a workflow fan-out when each item needs *fresh, focused context*, not merely
  parallel I/O; and inside a workflow, every node runs its own independent tool calls in
  parallel anyway.

## Components

- **Nodes** (`kind`): `work` (one agent turn); `script` (a deterministic subprocess — an
  inline `command` run via bash, or a `script` file under `<workspace>/workflows/scripts/`;
  the upstream text arrives on stdin, its stdout becomes the edge to the next node, and it
  works in the run's shared folder — zero tokens, no drift; a start-position script receives
  the run's task on stdin; a script that must **authenticate** declares the stored secrets
  it needs in `secrets: ["NAME"]` — injected as env vars when each allows the `exec` scope,
  with output redacted, so an authenticated `curl` stays a zero-token script step instead
  of becoming an agent turn); `parallel` (concurrent branches — a *static* `branches` list;
  *dynamic* fan-out: a `worker` template mapped over a runtime list named by `list_from` —
  a `script` list source must print one JSON array within the script output cap, or the run
  aborts before any worker starts; or
  *runtime-selected*: `branches_from` names the node — typically a routing script — whose
  output lists which declared work-node ids run this pass, as a JSON array or a
  comma-separated last line, so "run only the branches that apply" is ONE parallel node
  instead of one static block per combination; bounded by the global per-kind concurrency caps — script branches run wider than LLM branches — with `max_concurrency` as a uniform per-node override);
  `subworkflow` (run another named workflow as one step — this is how you compose
  pipelines; the child's terminal status propagates: a child that pauses for input pauses
  the parent resumably, a cancelled child cancels it, and a failed child aborts it naming
  the child — a sub-workflow can never silently read as "completed" text).
- **Named inputs & structured outputs**: `inputs_from` composes a node's input from the
  labeled outputs of named earlier nodes (plus the current edge as `[upstream]`) — no more
  courier files when a script chain sits between producer and consumer. `output_schema`
  (work nodes) forces a schema-validated delivery with immediate in-node retry on a
  malformed payload, and `output_file` has the ENGINE write the validated JSON into the
  working folder — prefer these over prose contracts plus downstream validation gates.
  A node with the file tools can also deliver a JSON draft from its working folder with
  `deliver_file(path)` (same schema check). A prompt that has it draft and check a file
  first should tell it to finish by calling `deliver_file`: a turn that ends without a
  delivery gets a forced one that asks for `deliver`, which means retyping the payload.
  A node with `output_file` may add `reuse: "if-unchanged"`: on a later run, if the node's
  definition and the runner's resolved model/provider/params all still match what actually
  produced that file, the engine skips the node entirely and reuses the artifact — free
  idempotence for expensive, rarely-changing steps.
- A work or script node may be **detached** (`detached: true`): launched off the critical
  path — the walk continues immediately, the edge passes through, its output never becomes
  the result, and its failure never sinks the run (side effects: persist, notify, archive).
- **Routing** is opt-in on a `work` or `script` node, never a separate node type: **binary**
  (`on_pass`/`on_fail`) or **multi-way** (`cases` — a map of labels to targets; a `null`
  target ends the run). The node ends its turn by calling `route` with its verdict (the
  runner tells it how) and the engine follows the matching edge. A fail / loop-back edge
  threads the node's feedback — the text it sent with the verdict — into the producer's
  next run so it knows what to fix; `max_visits` caps loops. Any case may route to the reserved
  **`__needs_input__`** terminal, which ends the run asking the caller for more information.
  **A script node routes deterministically**: exit code 0 = PASS / non-zero = FAIL (its
  stderr becomes the loop-back feedback), or its last stdout line as the case label — the
  ideal gate for "do the tests pass?"-style checks, where an agent gate would cost a turn
  and could be swayed.
- **Pass/fail is for checks that can fail; a normal branch is a `cases` route.** A check
  whose "no" is an expected path ("does `note.json` exist yet? no → go draft it") must
  print a label (`REUSE` / `DRAFT`) and route with `cases`, not exit 1 into `on_fail`. A
  FAIL is recorded as a failed gate: self-improvement counts every one as trouble, and a
  FAIL into a step that leads back to the gate (not by way of its `on_pass` step) reaches
  it as "Reviewer feedback (address this)". Converting a pass/fail check to `cases`
  changes its script too: it prints the label as its last stdout line and exits 0 (a
  non-zero exit on a `cases` node aborts the run).
- **Per node**: a `model` **or** a `persona` (a SOUL + its model); a work `mode` (`build` =
  may write files, `read` = read-only; unset, a routing node runs in `read` and any other in `build`); built-in `tools` (`none` / `default` — `default` is
  the background tool set: files, shell, search, web, memory search and memory writes,
  plus the vision/audio interpretation bridges when aux models are configured; interactive
  and orchestration tools — asking the user, messaging channels, spawning subagents,
  running workflows — are never in it); injected
  `skills`; a scoped subset of configured `mcps` (those tools exist only in a run the agent
  starts from a chat: a run started by an automation, the API or the dashboard runs the
  step without them); and a `session` policy — `"persistent"`
  makes a *looping* node resume its own conversation when the flow returns to it (it keeps
  its prior reasoning and only receives what is new), instead of restarting cold each pass.
  Each defaults to the minimum, so a node sees only what its job needs.
- **Loops converge deliberately** — on a revisit the engine tells the node which pass this
  is ("Pass X of Y") and marks the last allowed pass as FINAL; a gate whose FAIL would
  exhaust the loop is told its verdict is definitive. You get this for free — just set
  `max_visits` honestly.
- **Shared working folder** — every sequential node with file tools reads and writes ONE
  folder per run, so file-producing steps build on each other (a plan's code accumulates; a
  debug loop's reproduction, fix, and test live together) instead of copying a fileset down
  a chain. A `subworkflow` runs in its **parent's** folder, so files flow through composition;
  parallel writing branches fork the folder and their writes reconcile back. A node's file
  tools resolve a relative path in the folder (one under `workflows/`, `memory/` or `skills/`
  resolves from the workspace root, and a read into another root folder — a repository kept
  there — falls back to the root when the folder lacks the path); `exec` starts in the
  workspace root, so a prompt whose commands work on the folder's files should `cd` there or
  use full paths.
- **Input / Output** — optional descriptors (text and/or files, plus a free-text contract).
  A per-call `output_format` overrides the delivery shape for one run. A file-producing
  workflow can also **declare its artifacts** (`output.artifacts`: the paths it promises to
  produce) — every node sees the contract, and promised files missing after completion are
  reported as a warning, so a composed downstream stage learns the gap immediately.

## How to invoke and author

- **Discover:** call `list_workflows` to see what this workspace offers — each entry carries
  its description and I/O — before picking one to run (or to confirm one already exists).
- **Run:** `run_workflow(name, task)` — its parameters and replies say how to hand it
  files, run it in the background, read its result, and answer or retry a run that stopped.
  **`completed` does not mean every worker succeeded**: a fan-out completes when one worker
  does, so read the failed count and the failures before you use or report the output.
  **Resume** with `resume_run_id=<run id>`: a `needs_input` run (the answers go in `task`),
  an `aborted` run (its failed step is retried) or a `crashed` run, whose process died (it
  is entered again at the step it was in). Finished steps do not run again; the step the run
  stopped in runs again from its start — a parallel step with all its branches or workers, a
  sub-workflow step with its whole child — so what it already did outside durin (a sent
  message, a posted comment) can happen twice, and after a crash a command an agent step started
  with the shell tool may still be running. Before you resume, check whether that step
  already acted; tell the person when you cannot. When you resume a run another
  conversation started, that conversation may not be told: if its person must know, tell
  your user.
  **When a resume is refused, or a reply (a run summary, a `tasks` reply, a launch's
  `Note: run <id> … was cut short`) names a run cut short (status `crashed`) or a file, folder or pid in a run's
  way**, tell the person the reason and every run id, file, folder and pid it names. Do not,
  on your own, resume a cut run, end a process, or remove, move or change a file or folder:
  resume a cut run only when the person asks, after stopping the run your launch started
  (`tasks(action="stop", id=<run id>)`) if it still runs, and once it shows as cancelled.
  Before you start a new run in place of a crashed one whose reply names a script, or says
  its record is incomplete, tell the person a script of the crashed run may still be working.
- **Author:** call `workflow_write(name, definition, rationale)` — it validates the graph
  before anything lands (schema errors come back verbatim), refuses to overwrite an existing
  name, and records the change in the workflow version history. A successful save may still
  return `warnings` — e.g. a node `mode` that is not a registered mode (at run time it
  silently falls back to `build`, full access) or a mode allowlist naming tools that never
  load in a node. Fix what a warning describes rather than shipping it. Use it instead of writing the
  JSON file by hand; the web editor remains the human path. Compose larger processes from
  `subworkflow` nodes — but only where each stage genuinely needs the structure above.
  **Before authoring, read the references below** so the definition is complete and valid the
  first time; do not write a graph from memory of this overview alone.
- **Edit:** call `workflow_edit(name, definition, rationale)` for a user-requested change to
  an EXISTING workflow — load the current JSON, apply the change, pass the FULL replacement
  definition. Same validation and version history as authoring; it refuses a name that does
  not exist yet. The rationale matters: the self-improvement pass reads the version history
  to avoid re-proposing edits the user reverted.
- **Script files:** call `workflow_script_write(name, content, rationale)` to create or
  replace the file a `script` node runs; reference it from the node by that file name. The
  generic file tools refuse any path under `workflows/` — like `skills/`, this registry is
  write-guarded so nothing lands unvalidated or outside the version history.

## Authoring references — read on demand

This overview is the *when/why*. The concrete *how* lives in two reference files (open them
with `read_file` only when you are actually authoring a graph):

- **[references/authoring.md](references/authoring.md)** — the full JSON schema: every
  envelope and node field, its type, default, and the validation rules the parser enforces.
- **[references/patterns.md](references/patterns.md)** — one small, parse-verified JSON
  snippet per capability (sequential, binary/multi-way routing, `__needs_input__`, dynamic
  fan-out, static parallel + reconcile, subworkflow, per-node knobs, the working folder, I/O).
- **The seeds** in `<workspace>/workflows/*.json` — full end-to-end exemplars; read one whose
  shape matches your task before writing your own.

## Bundling a workflow inside a skill

A skill and a workflow are not either/or — a skill whose core mechanism is an
orchestration can **bundle** the workflow, the same way it bundles a script. Ship the
definition as `<skill>/workflow.json` and install-on-first-use:

1. If `<workspace>/workflows/<name>.json` does not exist, copy the skill's bundled
   `workflow.json` there — `run_workflow` only loads from the workspace's `workflows/` dir.
2. Then `run_workflow(<name>, task)`.

The skill carries the **trigger and the knowledge** (when and why to run it); the bundled
workflow carries the **orchestration** (the fan-out, the gates). Reach for this only when
the skill's real mechanism is a multi-step graph — not for a prose-only skill.

A workflow is also the **body of an automation**: when the same graph should fire on
standing triggers (a schedule, matching messages, webhooks, another automation's outcome)
and each run should deliver its result — optionally pursuing a verifiable life condition
until achieved — wrap it in an automation instead of re-running it by hand — read the
`automations` skill.

## Example — `research-to-answer` (a coverage workflow)

`plan` (break the question into a few *independent* search angles) → `gather` (dynamic
fan-out: one `search` worker per angle, in parallel) → `synthesize` (one coherent,
source-cited answer) → `verify` (a tolerant per-claim grounding gate: GROUNDED ends /
MISSING re-plans / MISUSED re-synthesizes). The fan-out is the whole point: N angles searched
in parallel, each with fresh context, gathering more sources than one turn could hold.

## Seeds that ship

`research-to-answer`, `brainstorming`, `writing-plans`, `build-specs`, `execute-plan`,
`debug`, `review-changes` — run any with `run_workflow(<name>, <task>)`. The first are
knowledge work; the rest are development. They are small, live-verified exemplars: read a
seed's JSON to see a pattern before authoring your own.
