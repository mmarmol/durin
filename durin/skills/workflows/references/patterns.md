# Patterns — annotated JSON per capability

One small, parseable snippet for each shape. Copy the shape, not the prose. For the full
field tables see [authoring.md](authoring.md); for complete end-to-end workflows read the
seeds in `<workspace>/workflows/*.json`.

## Sequential chain (`next`)

The baseline: each node hands its text output to the next; a `null` `next` ends the run.

```json
{
  "name": "sequential-example",
  "start": "draft",
  "nodes": [
    { "id": "draft",  "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Write a first draft from the input.", "next": "polish" },
    { "id": "polish", "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Tighten and finalize the draft.", "next": null }
  ]
}
```

## Binary routing with loop-back (`on_pass` / `on_fail`)

A reviewer node emits `PASS`/`FAIL`. `on_fail` loops back to the producer (its feedback is
threaded in), capped by `max_visits`. The reviewer differs from the producer (`mode`/`prompt`)
to satisfy the anti-Goodhart guard.

```json
{
  "name": "produce-then-verify",
  "start": "make",
  "max_visits": 3,
  "nodes": [
    { "id": "make",   "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Implement the change.", "next": "check" },
    { "id": "check",  "kind": "work", "mode": "read",  "tools": "default",
      "prompt": "Run the tests. End with PASS on the first line if green, else FAIL and say what broke.",
      "on_pass": null, "on_fail": "make" }
  ]
}
```

## Multi-way routing + `__needs_input__` (`cases`)

A node ends with exactly one declared label; the engine follows that edge. `null` ends the
run; `__needs_input__` pauses the run to ask the caller for more information.

```json
{
  "name": "intake-then-work",
  "start": "triage",
  "nodes": [
    { "id": "triage", "kind": "work", "mode": "read",
      "prompt": "If the brief is too thin to act on, end with NEED_INFO and list the questions. Otherwise end with READY.",
      "cases": { "READY": "build", "NEED_INFO": "__needs_input__" } },
    { "id": "build",  "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Carry out the brief.", "next": null }
  ]
}
```

## Dynamic fan-out (`worker` + `list_from`)

`plan` emits a JSON array; the `gather` parallel node runs one `search` worker per item, in
parallel, then merges. The worker node is defined like any `work` node and is named by
`worker`; it has no `next` (the parallel node owns the flow).

```json
{
  "name": "fan-out-example",
  "start": "plan",
  "nodes": [
    { "id": "plan",   "kind": "work", "mode": "read",
      "prompt": "Break the task into independent angles. Output ONLY a JSON array of strings.",
      "next": "gather" },
    { "id": "gather", "kind": "parallel", "worker": "search", "list_from": "plan",
      "max_concurrency": 3, "next": "merge" },
    { "id": "search", "kind": "work", "mode": "read", "tools": "default", "max_turns": 6,
      "prompt": "Investigate the one angle you were given; return concise, sourced findings." },
    { "id": "merge",  "kind": "work", "mode": "read",
      "prompt": "Combine the findings into one answer.", "next": null }
  ]
}
```

## Static parallel with reconcile (`branches` + `choose`)

Fixed branches all see the same input. With `reconcile: "choose"` each branch writes in a
private workspace copy and a judge picks one (needs `criteria`). Use `"union"` to apply all
(aborts on a same-path conflict) or `"read"` for analysis-only branches.

```json
{
  "name": "two-approaches",
  "start": "split",
  "nodes": [
    { "id": "split",  "kind": "work", "mode": "read",
      "prompt": "Restate the task for the implementers.", "next": "build" },
    { "id": "build",  "kind": "parallel", "branches": ["approach_a", "approach_b"],
      "reconcile": "choose", "criteria": "Pick the simpler implementation that passes the tests.",
      "next": null },
    { "id": "approach_a", "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Implement approach A (minimal change)." },
    { "id": "approach_b", "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Implement approach B (refactor first)." }
  ]
}
```

## Subworkflow composition (`kind: "subworkflow"`)

Run another named workflow as one step and use its output.

```json
{
  "name": "research-then-plan",
  "start": "research",
  "nodes": [
    { "id": "research", "kind": "subworkflow", "workflow": "research-to-answer", "next": "plan" },
    { "id": "plan",     "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Turn the research into a step-by-step plan.", "next": null }
  ]
}
```

## Per-node knobs (model / persona / skills / mcps)

Any `work` node can be tuned independently. `model` and `persona` are mutually exclusive.

```json
{
  "id": "review",
  "kind": "work",
  "mode": "read",
  "persona": "senior-reviewer",
  "skills": ["systematic-debugging"],
  "mcps": ["github"],
  "max_turns": 8,
  "prompt": "Review the diff for correctness.",
  "next": null
}
```

## Two output channels — text vs. the working folder

- **Text** is the edge: a node's reply becomes the next node's input.
- **Files** live in ONE shared working folder per run: every sequential node with file tools
  (`tools: "default"`, `mode: "build"`) reads earlier steps' files there and writes its own,
  so file-producing stages build on each other (a plan's code accumulates; a debug loop's
  reproduction, fix, and test live together). Parallel writing branches fork a private copy
  that is reconciled back (see the static-parallel pattern). You do not declare the folder —
  it exists per run; just have nodes read and write files normally.

## Input / Output descriptors (envelope)

Declare what the workflow consumes and delivers. Input text becomes the start node's task;
input files land in the shared working folder.

```json
{
  "name": "io-example",
  "start": "work",
  "input":  { "text": true, "file": true, "description": "a question plus reference files" },
  "output": { "text": true, "description": "a source-cited answer" },
  "nodes": [
    { "id": "work", "kind": "work", "mode": "read", "tools": "default",
      "prompt": "Answer the question using the provided files.", "next": null }
  ]
}
```
