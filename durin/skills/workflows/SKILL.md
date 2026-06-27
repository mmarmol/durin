---
name: workflows
description: How durin's workflow engine works and — above all — when a workflow earns its place over a plain prompt. Load before reaching for the run_workflow tool, before authoring or composing a workflow (a JSON flow graph under the workspace), or when deciding whether a multi-step / fan-out / verification task should be a workflow at all. Covers the components (work / parallel / subworkflow nodes, routing and loops, the shared working folder, the needs-input escape), how to invoke and author them, the seed workflows that ship, and the value test — reach for a workflow only for coverage-at-scale, independent verification, or determinism; never for synthesis, judgment, discipline, or interactive flows, where a prompt or skill wins.
---

# Workflows — durin's flow-graph engine

A workflow is a user-defined **flow graph of nodes**, run on a task with the `run_workflow`
tool. Each node is a real agent turn with its own model, tools, and persisted session; the
**graph — not the LLM — drives routing** (continue, branch, loop). It runs *above* the
normal agent loop, so every node's work is a searchable session. Definitions are JSON under
`<workspace>/workflows/<name>.json`, so a human, you, or the web editor can author one.

## When a workflow earns its place — the value test

**Default to a single good prompt.** A workflow only beats one when the task has a
structural property that a single context handles badly — at least one of these:

- **Coverage / breadth beyond one context** — fan out a worker over many *independent*
  items (sources, documents, review lenses, plan steps). Each worker gets a fresh, focused
  context; a single turn dilutes its attention across all of them. This is the strongest reason.
- **Independent verification** — a separate node checks the producer's output. Producer ≠
  checker catches what self-review is blind to.
- **Determinism** — a step that must run identically every time, or a real command whose
  result a prompt could otherwise hallucinate (run the tests, validate the schema).

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
  prompt matches the workflow without the orchestration overhead.

## Components

- **Nodes** (`kind`): `work` (one agent turn); `parallel` (concurrent branches — a *static*
  `branches` list, or *dynamic* fan-out: a `worker` template mapped over a runtime list named
  by `list_from`, bounded by `max_concurrency`); `subworkflow` (run another named workflow as
  one step — this is how you compose pipelines).
- **Routing** is opt-in on a `work` node, never a separate node type: **binary**
  (`on_pass`/`on_fail`) or **multi-way** (`cases` — a map of labels to targets; a `null`
  target ends the run). The node ends with its verdict and the engine follows the matching
  edge. A fail / loop-back edge threads the node's feedback into the producer's next run so
  it knows what to fix; `max_visits` caps loops. Any case may route to the reserved
  **`__needs_input__`** terminal, which ends the run asking the caller for more information.
- **Per node**: a `model` **or** a `persona` (a SOUL + its model); a work `mode` (`build` =
  may write files, `read` = read-only); built-in `tools` (`none` / `default`); injected
  `skills`; and a scoped subset of configured `mcps`. Each defaults to the minimum, so a node
  sees only what its job needs.
- **Shared working folder** — every sequential node with file tools reads and writes ONE
  folder per run, so file-producing steps build on each other (a plan's code accumulates; a
  debug loop's reproduction, fix, and test live together) instead of copying a fileset down a chain.
- **Input / Output** — optional descriptors (text and/or files, plus a free-text contract).
  A per-call `output_format` overrides the delivery shape for one run.

## How to invoke and author

- **Run:** `run_workflow(name, task)` — optionally `output_format` to shape this call's
  result. It loads the named JSON, runs the graph, and returns a per-node trace. **If the
  result says it needs input**, the workflow did not fail — it paused with questions; ask the
  user those questions, then call `run_workflow` again with the SAME task plus their answers appended.
- **Author:** write the JSON under `<workspace>/workflows/<name>.json` (you can write a graph
  on demand for a user's repeatable process), or use the web editor. Compose larger processes
  from `subworkflow` nodes — but only where each stage genuinely needs the structure above.

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
