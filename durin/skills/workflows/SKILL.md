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
  of becoming an agent turn); `parallel` (concurrent branches — a *static* `branches` list, or
  *dynamic* fan-out: a `worker` template mapped over a runtime list named by `list_from`,
  bounded by `max_concurrency`); `subworkflow` (run another named workflow as one step —
  this is how you compose pipelines).
- **Routing** is opt-in on a `work` or `script` node, never a separate node type: **binary**
  (`on_pass`/`on_fail`) or **multi-way** (`cases` — a map of labels to targets; a `null`
  target ends the run). The node ends with its verdict and the engine follows the matching
  edge. A fail / loop-back edge threads the node's feedback into the producer's next run so
  it knows what to fix; `max_visits` caps loops. Any case may route to the reserved
  **`__needs_input__`** terminal, which ends the run asking the caller for more information.
  **A script node routes deterministically**: exit code 0 = PASS / non-zero = FAIL (its
  stderr becomes the loop-back feedback), or its last stdout line as the case label — the
  ideal gate for "do the tests pass?"-style checks, where an agent gate would cost a turn
  and could be swayed.
- **Per node**: a `model` **or** a `persona` (a SOUL + its model); a work `mode` (`build` =
  may write files, `read` = read-only); built-in `tools` (`none` / `default`); injected
  `skills`; a scoped subset of configured `mcps`; and a `session` policy — `"persistent"`
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
  parallel writing branches fork the folder and their writes reconcile back.
- **Input / Output** — optional descriptors (text and/or files, plus a free-text contract).
  A per-call `output_format` overrides the delivery shape for one run. A file-producing
  workflow can also **declare its artifacts** (`output.artifacts`: the paths it promises to
  produce) — every node sees the contract, and promised files missing after completion are
  reported as a warning, so a composed downstream stage learns the gap immediately.

## How to invoke and author

- **Discover:** call `list_workflows` to see what this workspace offers — each entry carries
  its description and I/O — before picking one to run (or to confirm one already exists).
- **Run:** `run_workflow(name, task)` — optionally `output_format` to shape this call's
  result, and `input_files` (a list of absolute paths) to hand the workflow files to work on:
  each is seeded into the run's shared working folder before the start node runs, so every node
  (including a dynamic fan-out of one worker per file) reads them there — use this instead of
  pasting file contents into `task`. Provided paths are validated before anything runs: a
  missing file or two files with the same name abort with a clear message, and a workflow
  that declares `input: {"file": true}` given no files pauses immediately asking for them.
  **By default the run goes to the background:** the call returns immediately with a run id,
  and the result — a per-node trace plus the final text output — is **delivered to you
  automatically as a follow-up message** when it finishes. Do NOT wait for it with
  sleep+status polling: tell the user the workflow is running and **end your turn** — the
  follow-up wakes you, and the user watches live per-node progress in the Work panel.
  Reach for `tasks(action="status", id=<run id>)` only when the user asks for an update or
  you need a mid-run look at the run's work dir and files (status reports both, plus
  per-node durations); `tasks(action="stop", ...)` cancels. Pass `background=false` ONLY
  when you need the result to keep reasoning in the same turn (the call then blocks and
  returns the result directly).
  **Getting files back:** when the workflow declares it outputs files
  (`output: {"file": true}`), the summary reports the run's working-folder path AND lists the
  produced files — read them there, and copy out anything that must outlive the run (working
  folders are pruned after `workflow.keep_runs` newer runs). If the workflow declares
  `output.artifacts`, the summary also WARNS about any promised file the completed run did
  not produce — act on that gap (re-run, repair, or tell the user) before consuming the rest. **If the result says it needs
  input**, the workflow did not fail — it paused with questions and the summary carries the
  run id; answer them (from your own context when you can, otherwise ask the user), then call
  `run_workflow` again with `resume_run_id=<that id>` and the answers as `task`. That resumes
  the SAME run — same working folder, node sessions, and loop counters — at the node that
  asked, instead of restarting from scratch.
- **Author:** call `workflow_write(name, definition, rationale)` — it validates the graph
  before anything lands (schema errors come back verbatim), refuses to overwrite an existing
  name, and records the change in the workflow version history. Use it instead of writing the
  JSON file by hand; the web editor remains the human path. Compose larger processes from
  `subworkflow` nodes — but only where each stage genuinely needs the structure above.
  **Before authoring, read the references below** so the definition is complete and valid the
  first time; do not write a graph from memory of this overview alone.
- **Edit:** call `workflow_edit(name, definition, rationale)` for a user-requested change to
  an EXISTING workflow — load the current JSON, apply the change, pass the FULL replacement
  definition. Same validation and version history as authoring; it refuses a name that does
  not exist yet. The rationale matters: the self-improvement pass reads the version history
  to avoid re-proposing edits the user reverted.

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

A workflow is also the **body of a loop**: when the same graph should fire on standing
triggers (matching messages, webhooks, a schedule) and each run must verify a goal, park
for replies, and wake on new information, wrap it in a loop instead of re-running it by
hand — read the `loops` skill.

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
