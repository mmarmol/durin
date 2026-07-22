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
to satisfy the anti-Goodhart guard. `session: "persistent"` on the producer makes each
loop-back **resume its conversation** — it keeps its prior reasoning and receives only the
reviewer's feedback plus an automatic pass counter ("Pass 2 of 3"; the last allowed pass is
marked FINAL). Use it whenever the looping node does incremental work.

```json
{
  "name": "produce-then-verify",
  "start": "make",
  "max_visits": 3,
  "nodes": [
    { "id": "make",   "kind": "work", "mode": "build", "tools": "default",
      "session": "persistent",
      "prompt": "Implement the change.", "next": "check" },
    { "id": "check",  "kind": "work", "mode": "read",  "tools": "default",
      "prompt": "Run the tests. End with PASS on the first line if green, else FAIL and say what broke.",
      "on_pass": null, "on_fail": "make" }
  ]
}
```

## Script gate — a deterministic check routes the flow (`kind: "script"`)

The producer is an agent; the gate is a **subprocess**: exit 0 routes `on_pass`, non-zero
routes `on_fail` with the script's stderr threaded back as feedback. Zero tokens, immune to
drift, exempt from the anti-Goodhart guard. The gate reads the producer's text on stdin and
runs in the shared working folder, so it can check files the producer wrote. Prefer this
over an agent gate whenever the criterion is a real command (tests, linters, validators).

```json
{
  "name": "build-then-test",
  "start": "implement",
  "max_visits": 3,
  "nodes": [
    { "id": "implement", "kind": "work", "mode": "build", "tools": "default",
      "session": "persistent",
      "prompt": "Implement the change in the working folder.", "next": "gate" },
    { "id": "gate", "kind": "script", "command": "test -f result.md && grep -qi done result.md",
      "timeout": 120, "on_pass": null, "on_fail": "implement" }
  ]
}
```

## Script steps — deterministic transforms and fan-out lists

A linear script node transforms the edge (stdin → stdout); a non-zero exit aborts the run
(in a linear step it is an error, not a verdict). A script as the `list_from` source makes
the fan-out list deterministic — no more malformed JSON from a model. Use `script` (a file
under `<workspace>/workflows/scripts/`) instead of `command` when the logic outgrows one line.

An **authenticated** script step declares the stored secrets it needs — they arrive as env
vars (each must allow the `exec` scope), so a `curl` against a real API stays a
zero-token script node instead of becoming an agent turn:

```json
{
  "name": "fetch-ticket",
  "start": "fetch",
  "nodes": [
    { "id": "fetch", "kind": "script", "script": "zd-fetch-ticket.sh",
      "secrets": ["ZENDESK_API_TOKEN"], "timeout": 60, "next": "summarize" },
    { "id": "summarize", "kind": "work", "mode": "read", "tools": "default",
      "prompt": "Summarize the fetched ticket JSON in the working folder.", "next": null }
  ]
}
```

```json
{
  "name": "each-file-reviewed",
  "start": "list",
  "input": { "file": true, "description": "the files to review" },
  "nodes": [
    { "id": "list", "kind": "script",
      "command": "find . -maxdepth 1 -type f | sed 's|^./||' | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split()))'",
      "next": "fan" },
    { "id": "fan", "kind": "parallel", "worker": "review", "list_from": "list",
      "max_concurrency": 3, "next": "merge" },
    { "id": "review", "kind": "work", "mode": "read", "tools": "default",
      "prompt": "Review the one file you were given; report findings." },
    { "id": "merge", "kind": "work", "mode": "read",
      "prompt": "Combine the findings into one report.", "next": null }
  ]
}
```

## Multi-way routing + `__needs_input__` (`cases`)

A node ends with exactly one declared label; the engine follows that edge. `null` ends the
run; `__needs_input__` pauses the run to ask the caller for more information. To continue,
call `run_workflow` again with `resume_run_id=<run id>` and the answers as `task` — the run
resumes at the asking node with its working folder, sessions, and loop counters intact.

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
private copy of the run's shared working folder and a judge picks one (needs `criteria`).
Use `"union"` to apply all
(aborts on a same-path conflict) or `"read"` for analysis-only branches.

Branches may MIX kinds: a `script` branch runs its deterministic job (fetch, convert,
check) beside the agent branches — stdin is the parallel's input, stdout is its branch
output, cwd is the working folder (a private fork of it under choose/union), and a
non-zero exit fails only that branch while the others complete. Use a script branch to
overlap slow deterministic I/O with an LLM analysis instead of serializing them.

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

## Runtime-selected branches (`branches_from`)

When different runs need different branch SUBSETS — "run only the analyzers that apply to
this input" — a routing script emits the branch ids (a JSON array, or a comma-separated
last line) and ONE parallel node runs exactly those. Without it, every branch combination
needs its own static parallel block (2^N nodes for N optional analyzers). Every resolved
id must name a declared `work` or `script` node; an empty list is valid (nothing applies — the walk
continues to `next`).

```json
{
  "name": "typed-analysis",
  "start": "route",
  "nodes": [
    { "id": "route", "kind": "script", "script": "route-analyzers.py", "next": "fan" },
    { "id": "fan", "kind": "parallel", "branches_from": "route",
      "max_concurrency": 2, "next": "merge" },
    { "id": "analyze_logs", "kind": "work", "mode": "read", "tools": "default",
      "prompt": "Analyze only the log files in the working folder." },
    { "id": "analyze_images", "kind": "work", "mode": "read", "tools": "default",
      "prompt": "Analyze only the image files (interpret_image per file)." },
    { "id": "merge", "kind": "script", "script": "consolidate.py", "next": null }
  ]
}
```

`route-analyzers.py` inspects the working folder and prints e.g.
`analyze_logs, analyze_images` (or `["analyze_logs"]`, or `[]`) as its last line.

## Detached side-effect (`detached: true`)

A side-effect node (persist, notify, archive) launched off the critical path: the walk
continues immediately, the edge text passes through unchanged, and the node's failure is
recorded without sinking the run. The run still joins it before finishing, so the trace is
complete. Requires a linear `next`; its output never becomes the run result.

```json
{
  "name": "answer-then-persist",
  "start": "answer",
  "nodes": [
    { "id": "answer", "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Produce the deliverable.", "next": "persist" },
    { "id": "persist", "kind": "work", "mode": "build", "tools": "default", "detached": true,
      "prompt": "Read the deliverable in the working folder and upsert the entities it names into memory (memory_search first; never duplicate).",
      "next": "report" },
    { "id": "report", "kind": "work", "mode": "read",
      "prompt": "Summarize the deliverable for the caller.", "next": null }
  ]
}
```

## Subworkflow composition (`kind: "subworkflow"`)

Run another named workflow as one step and use its output. The nested run works in the
parent's shared working folder, so a file the parent produced is readable by the child's
nodes and vice versa — composition passes files, not just text. The child's terminal
status propagates: a child that pauses (`needs_input`) pauses the parent resumably at
this node, a cancelled child cancels it, and a failed child aborts it naming the child —
a pipeline never "completes" past a stage that did not actually run.

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
  reproduction, fix, and test live together). Subworkflows run in the parent's folder;
  parallel writing branches fork the folder and their writes
  reconcile back; read branches and dynamic workers are handed the folder directly. You do
  not declare the folder — it exists per run; just have nodes read and write files normally.
  To hand produced files back to the caller, declare `output: {"file": true}` on the
  envelope: the run result then reports the working-folder path AND the list of produced
  files. Copy out anything that must outlive the run — working folders are pruned once
  `workflow.keep_runs` newer runs accumulate.

## Input / Output descriptors (envelope)

Declare what the workflow consumes and delivers. Input text becomes the start node's task;
input files (declared with `"file": true`) are supplied at call time — `run_workflow`'s
`input_files` argument or the web editor's run bar — and land in the shared working folder.

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

A file-producing workflow can additionally **declare its artifacts** — the files it promises
to leave in the working folder. Every node sees the contract in its framing, and a completed
run that did not produce one reports it as a warning (in the result summary, the manifest,
and `tasks(status)`) — so a composed downstream stage learns immediately which promised file
is absent instead of failing confusingly later:

```json
{
  "name": "stage1-context",
  "start": "gather",
  "output": { "file": true, "artifacts": [
    { "path": "context.json", "description": "consolidated ticket context" },
    { "path": "evidence.json" }
  ]},
  "nodes": [
    { "id": "gather", "kind": "work", "mode": "build", "tools": "default",
      "prompt": "Investigate and write context.json and evidence.json.", "next": null }
  ]
}
```
