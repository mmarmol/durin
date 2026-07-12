# Workflows

A **workflow** is a multi-step process you define once and run many times: a
graph of **nodes**, where each node is a real agent turn (its own model,
tools, and session) and can route the flow onward, back to an earlier node
(a loop), or to a stop. Where a normal chat is one continuous conversation,
a workflow breaks a task into focused steps — a plan step, a write step, a
review step — each with only the context and tools it needs.

Workflows are plain JSON files under `<workspace>/workflows/<name>.json`, so
you can hand-edit them, build them in the visual **Workflows** pane of the
web dashboard, or ask the agent to draft one. The agent runs a workflow with
the `run_workflow` tool, and discovers which ones exist (and what they do)
with `list_workflows` — you don't need to remember exact names, just ask
"run the debug workflow on this failing test" and the agent finds it.

durin ships several ready-to-run workflows (`research-to-answer`,
`brainstorming`, `writing-plans`, `build-specs`, `execute-plan`, `debug`,
`review-changes`) as starting points and examples — see them under
`<workspace>/workflows/` after your first run, or read on for what a few of
them look like.

## Authoring a node

Every node has:

- **A prompt** — its role framing, e.g. "Review this diff for correctness
  bugs and report GRAVE / MINOR / NONE."
- **A model or persona.** Omit `model` to use your default; set `model` for
  a specific one, or `persona` to run the node as one of your configured
  Personas (a SOUL + its model). `model` and `persona` are mutually
  exclusive — pick one.
- **A tool set.** `tools: "none"` (the default) runs the node with no
  tools — it just reasons over its input and replies. `tools: "default"`
  gives it your normal tool set (file read/write, shell, web, etc.) so it
  can actually do work.
- **Skills and MCP servers.** A node can name `skills` (skill docs injected
  into its own prompt) and `mcps` (a subset of your already-configured MCP
  servers) — scoped per node, so a node only sees what its job needs.
- **A mode.** Non-routing work nodes default to `mode: "build"` (full
  access, neutral posture); routing nodes (those with `on_pass`/`on_fail` or
  `cases`) default to `mode: "explore"` (read-only) — a deliberate gate
  that can inspect but not modify. You can override either default by
  setting `mode` explicitly. When you do, prefer the neutral `read` mode
  for a read-only work step and avoid `plan`: the interactive modes carry
  conversational framing (e.g. "the parent should /build") that can derail
  a step running unattended.

## Script nodes: deterministic steps with no agent turn

Not every step needs a model. A **script node** runs a command or a script file
instead of an agent turn — no tokens, no drift, the same answer every time. Reach
for one when a step is really a check or a transform: running a test suite as a
pass/fail gate, linting, computing a deterministic fan-out list, reformatting
text, calling a small local tool. An agent step is for judgment; a script step is
for "the same rule, every time."

A script node has two forms — pick exactly one:

- **`command`** — an inline shell command, run via `bash -c`, e.g.
  `"command": "npm test"`.
- **`script`** — a file under `<workspace>/workflows/scripts/`, run by extension:
  `.py` under Python, `.sh` under `bash`, anything else must be directly
  executable (a shebang line). Use this for anything longer than a one-liner.

The I/O contract is plain Unix:

- **stdin** is the previous node's output (the upstream edge text). If the
  script node is the workflow's first node, stdin is the run's task instead.
- **stdout** becomes the edge text for the next node — capped
  (`workflow.script_output_max_chars`, default 16000 characters; excess is
  truncated with a notice).
- **stderr** is for diagnostics only — it never becomes the edge text, though a
  failing gate folds a tail of it into the loop-back feedback so whoever
  re-runs sees why it failed.
- **The exit code decides pass/fail.** On a routing script node
  (`on_pass`/`on_fail`), exit `0` is `PASS`; anything else is `FAIL`. On a
  `cases` (multi-way) script node, the **last non-empty line of stdout** picks
  the case, but the process must still exit `0` — a non-zero exit there (or on
  a plain, non-routing script node) ends the run as a failure instead of a
  verdict, since a script that crashed mid-way has nothing trustworthy to say.
- **Work dir.** The script's current directory is the run's shared working
  folder — the same folder an agent step with `tools: "default"` reads and
  writes, so a script step can read files an earlier step produced, or leave
  files for a later step.
- **Environment.** A handful of `DURIN_*` variables carry run metadata:
  `DURIN_TASK` (the run's task, capped), `DURIN_RUN_ID`, `DURIN_NODE_ID`,
  `DURIN_ITERATION` (which pass this is, for a looping node), and
  `DURIN_WORK_DIR` (the shared working folder's path). The rest of the
  subprocess environment is controlled by `env`: `"clean"` (the default) gives
  the script a minimal allowlist (`PATH`, `HOME`, `USER`, `SHELL`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `DURIN_HOME`) plus those `DURIN_*` vars; `"inherit"`
  gives it the full environment your durin gateway runs in — opt into it only
  when the script genuinely needs an ambient variable durin doesn't forward.
- **Timeout.** A script node has its own `timeout` in seconds, or falls back to
  `workflow.script_timeout` (default 300s). A script that runs past it is
  killed and the node fails.

A script node has no `model`, `prompt`, `tools`, `mode`, or session — those
fields don't apply and the workflow parser rejects them if you set one. It can't
be a parallel branch or fan-out worker either: a script already does its own
iteration internally, so wrapping it in the engine's fan-out would be redundant.

### Example: implement, then gate on the real test suite

An agent step implements a change; a script step runs the actual test suite as
the gate — no model asked to eyeball test output, just the tests' own exit code
deciding pass or fail. A failing run loops back to the implement step with the
test output as feedback.

```json
{
  "name": "implement-and-test",
  "description": "Implement a change, then run the test suite as a deterministic gate.",
  "start": "implement",
  "nodes": [
    {
      "id": "implement",
      "kind": "work",
      "prompt": "Implement the requested change in the shared working folder.",
      "tools": "default",
      "next": "test"
    },
    {
      "id": "test",
      "kind": "script",
      "command": "npm test",
      "timeout": 120,
      "on_fail": "implement"
    }
  ]
}
```

`test` has no `on_pass` target, so a passing run (exit `0`) ends the workflow
right there; a failing run (any other exit code) routes back to `implement` with
the test output as loop-back feedback, bounded by the workflow's usual
`max_visits` loop guard.

The same swap upgrades the development seeds: when you copy `debug` or
`execute-plan` into your own workflow, replace their agent verify gate with a
script node running *your* project's real check (`pytest -q`, `npm test`,
`cargo test`) — the seeds ship with an agent gate only because they can't know
your command.

## Routing: deciding what happens next

A node can just hand off to the next node (`"next": "other_node"`), or it
can **route** — end its own reply with a verdict that decides where the flow
goes:

- **Binary** (`on_pass` / `on_fail`): the node ends its reply with
  `PASS` or `FAIL`. A `FAIL` can loop back to an earlier node (see Loops
  below), carrying the node's own output as feedback so the step that
  produced the work knows what to fix.
- **Multi-way** (`cases`): the node declares a set of named outcomes, e.g.
  `{"GROUNDED": null, "MISSING": "plan", "MISUSED": "synthesize"}`, and
  ends its reply with exactly one of those labels. `null` ends the run;
  any other value is the id of the node to go to next.

The verdict is elicited from the model as a **forced tool call**
(not just hoped for in free text), so a routing node's output is
reliable — a pass/fail or label that cannot be derailed by a stray sentence.
If the forced call is unavailable, a text-parse fallback applies: a binary
gate reads its verdict from the **first non-empty line** (PASS/FAIL), and a
multi-way node from the **last non-empty line** (a matched case label).

One special multi-way target is reserved: `__needs_input__`. Routing there
ends the run with status `needs_input` and the node's own output (its
questions) as the result — see **Asking for more information**, below.

durin's own seed workflows show both shapes: `research-to-answer`'s verify
step is multi-way (`GROUNDED` ends the run, `MISSING` loops back to
re-plan, `MISUSED` loops back to re-synthesize); `build-specs`'s `gate`
step is binary (`on_pass`/`on_fail`) — a script node whose exit code ends
the run on pass or loops back to re-assemble on fail.

## Nested workflows (subflows)

Instead of a work node, you can have a node that runs another workflow as a
nested run. A subworkflow node runs in the **same working folder as the
parent** — it reads and extends the same set of files that earlier nodes
created, so the nested workflow's steps collaborate on the parent's
evolving fileset just as if they were sequential steps in the parent workflow.
The nested run's session traces anchor to the invoking conversation, so its
work is navigable as part of the parent.

## Loops

A `FAIL` (or a case that targets an earlier node) can send the flow back to
redo a step. Loops are bounded so they can't run forever:

- **`max_visits`** on the workflow (default 3) or a specific node caps how
  many times that node may run, clamped by a hard global ceiling
  (`workflow.max_node_visits`, default 25) no node can exceed regardless of
  what the definition says. If a node's budget runs out, the run ends with
  status `exhausted` rather than looping silently forever.
- **Pass awareness.** On a revisit, the node is told which pass it's on
  ("Pass 2 of 3"), and on its last allowed pass it's told explicitly that
  no further iteration will happen — so it delivers a final, complete
  result instead of another half-finished increment.
- **Definitive last-round verdicts.** If a binary gate's `FAIL` would use
  up the last remaining visit of the step it loops back to, the gate is
  told its verdict is final: `PASS` with any caveats noted, or `FAIL` with
  a clear closing summary — not another "please fix X" that will never get
  another pass to act on.

### Persistent sessions across a loop

By default, every visit to a node — including a revisit inside a loop — is
a fresh session: the node sees only the new input, not its own prior
reasoning. For a node that does substantial iterative work (e.g. an
"implement" step in a build-review loop that edits a growing set of
files), that means re-deriving context on every pass.

Set `"session": "persistent"` on a node to change that: its visits share
ONE session, so a revisit resumes the node's own prior conversation
(reasoning, decisions, and what it already knows about the files it
touched) and receives only the new input — the loop feedback and the pass
counter — as a short revisit turn, instead of rebuilding everything from
scratch. This requires `context: "own"` (the default) and is rejected on
parallel branches or fan-out workers, which always get their own per-unit
session.

Use persistent sessions for a node that is doing real incremental work
across passes (an implement step reacting to review feedback); leave the
default (`"fresh"`) for a node whose job is a clean look each time (an
independent reviewer, for instance — persistence would just accumulate its
own bias).

The `execute-plan`, `debug`, and `writing-plans` seed workflows ship with
persistent sessions on their looping steps (`implement`, `diagnose`+`fix`, and
`revise` respectively) — each carries context across iterations as the node
refines its work in response to loop feedback.

## Passing work between steps

- **Text edges.** A node's reply becomes the next node's input, by
  default in isolation (`context: "own"`) — the node sees only that
  upstream text plus its own prompt.
- **Shared context.** Set `context: "shared"` and a node also receives a
  running buffer of every earlier `shared` node's own conversation turns,
  so a chain of shared nodes builds a genuinely continuous discussion
  rather than a relay of isolated summaries. (Only sequential nodes can
  share context; parallel branches and fan-out workers are always
  isolated, and a routing node can't use `context: "shared"` since it
  needs to judge its own output, not a shared narrative.)
- **The shared working folder.** Any node with `tools: "default"` reads
  and writes files in one folder shared by the whole run — so a "write the
  code" step, a "write the test" step, and a "fix the bug" step all see
  each other's files without you wiring that up explicitly. This is what
  lets `execute-plan` and `debug` collaborate on an evolving fileset across
  steps (and across a loop's revisits) instead of handing copies down a
  chain.
- **Declared input/output.** A workflow can declare an `input` (optional
  `text` and/or `file`, plus a free-text `description`) and `output`
  descriptor. Declaring `file: true` input means the workflow expects
  files to work on — pass them via `input_files` (absolute paths) when you
  run it, and they land in the shared working folder before the first node
  runs. The `description` fields are framing hints given to every node
  (what the run received, what it must deliver) — not enforced, just
  steering.
- **Reading back what a run produced.** A completed run reports its
  `output_dir` (the shared working folder) and the list of files inside it
  (`output_files`, relative paths) — so you know exactly what was created
  or changed and where to find it.

## Parallel nodes

A parallel node runs several branches at once and merges their text
outputs into the next node's input:

- **Static** — a fixed list of branches, each its own node with its own
  prompt, all seeing the same input (e.g. "review this diff for security /
  performance / readability" as three parallel reviewers).
- **Dynamic** — a single worker template mapped over a runtime list (e.g.
  one search worker per query the plan step produced). The upstream node
  emits the list as a JSON array (or newline-separated text as a
  fallback).

`max_concurrency` (default 2) bounds how many branches or workers run at
once; extra ones queue and run in later waves.

For **writing** branches — ones that create or edit files — `reconcile`
decides how their work comes back together:

- `read` — read-only branches; nothing is written back (the default, for
  analysis/review branches).
- `choose` — each branch writes into its own private copy of the run's
  files; a judge picks the best one to keep, discarding the rest. A
  `choose` node requires a `criteria` string (how the judge should pick
  the winner).
- `union` — every branch's writes are applied; a genuine conflict (two
  branches wrote *different* content to the same file) aborts the run
  rather than silently picking one.

Dynamic fan-out workers always share the folder directly (no per-worker
isolation), so `reconcile` only applies to static branches.

## Asking for more information

Sometimes a workflow can't proceed without more from the user — an
underspecified brief, a missing detail. Route to the reserved
`__needs_input__` target (from a multi-way `cases` node, or have the
pre-flight file check trigger it — see below) and the run ends with status
`needs_input` instead of failing or guessing. The agent that invoked the
workflow owns the conversation: it asks the user the questions in the
node's output, then re-runs.

To continue instead of starting over, re-run with `resume_run_id` set to
the paused run's id and the user's answers as the task. The run re-enters
the graph **at the node that asked** — same run id, same shared working
folder, same node sessions, and the same visit counts already spent —
rather than repeating everything from the start. `writing-plans`,
`build-specs`, `execute-plan`, and `brainstorming` all use this pattern for
their intake step.

If a workflow declares `input: {file: true}` and you run it with no
`input_files`, the run ends `needs_input` immediately (before any node
runs) asking for the files — and if you do pass files but one is missing
or two collide on the same filename, the run is rejected outright
(`aborted`, naming the problem) before anything is created. Both checks
happen before the run is even recorded, so a bad call leaves no trace to
clean up.

## Where things live

- **Definitions:** `<workspace>/workflows/<name>.json` — a small
  git-versioned directory; every run snapshots the definitions it used, so
  you can see how a workflow evolved over time.
- **Run records:** each run writes a manifest under
  `<workspace>/workflows-runs/<name>/<run_id>.json` with the outcome and a
  per-node trace (status, verdict, and the session each node produced). A
  run started by a subworkflow node records its caller's run id, so the
  dashboard can mark it as a sub-run of the parent. The web dashboard's
  Workflows pane reads these to show run history.
- **Node sessions:** every node's conversation is a normal, searchable
  durin session (`workflow:<run_id>:<node_id>:...`), so a node's reasoning
  is navigable after the fact the same way a sub-agent's is.
- **Retention:** both the shared working folder
  (`<workspace>/.workflow/<run_id>/work/`, gitignored) and the run
  manifests are pruned automatically, keeping the most recent runs per
  workflow. `workflow.keep_runs` (default 20) controls how many are kept;
  a run still waiting on your input is never pruned, and completed runs
  keep their history entry until they age out — copy out any deliverable
  you need to keep before then.

## Editing visually

The web dashboard's **Workflows** pane is a visual graph editor (built on
React Flow): drag nodes onto a canvas, wire edges, configure each node's
prompt/model-or-persona/mode/context/tools/session in a side panel, and add
static or dynamic parallel branches with a concurrency cap. The palette also
adds script nodes — an inline command or a picker over the files under
`workflows/scripts/`, with a timeout and the same pass/fail or `cases` routing
config as a work node. The file picker's "New script…" and "Edit" actions let
you create or edit those script files from the same panel, without leaving the
browser. Input and
Output are clickable canvas objects where you toggle text/file and write
the free-text description. Runs launched from the editor show live,
per-node progress as the graph executes.

The Workflows section's **Runs** pane is where to look across *all* workflows
and origins (sessions, HTTP, cron, the editor) at once: a global, filterable
run feed, with any run stranded on `needs_input` surfaced in a tray up top —
questions and a resume form included — so you can find and answer a paused run
without knowing which session or workflow started it. A badge on the sidebar's
**Workflows** button shows the count of stranded runs even while you're
elsewhere in the app.

Each workflow also has a self-improvement mode (`manual` by default): a
background pass looks at recurring trouble (a node that keeps looping, a
gate that keeps failing, a script node that keeps crashing) and proposes one
scoped edit — a prompt rewrite, a fix to a script node's command, or a fix to
a script file, the latter two only when the failure recurs and only after an
automatic check (syntax, security, a quick trial run) passes — shown as a
recommendation you review and apply from the Workflows pane's recommendations
banner, or `durin workflow recommendations` / `durin workflow apply <name>
<id>` on the command line. A fix to a routing/gate script always waits for
your review, even in auto mode — the dream never weakens a check on its own.
Applying always versions the change, so you can see exactly what changed and
why.

## See also

[Workflow engine internals](../internals/workflow.md) covers the
architecture — the graph model, the manifest, session lineage, and how the
engine drives each node.
