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

Every work node (`"kind": "work"`, the default) has:

- **A prompt** — its role framing, e.g. "Review this diff for correctness
  bugs and report GRAVE / MINOR / NONE."
- **A model or persona.** Omit `model` to use your default; set `model` for
  a specific one, or `persona` to run the node as one of your configured
  Personas (a SOUL + its model). `model` and `persona` are mutually
  exclusive — pick one.
- **A tool set.** `tools: "none"` (the default) runs the node with no
  tools — it just reasons over its input and replies. `tools: "default"`
  gives it the background tool set (file read/write, shell, web, memory
  search, etc.) so it can actually do work. The interactive tools are left
  out: a node cannot ask you a question, send a channel message, spawn a
  sub-agent or run another workflow.
- **A turn budget.** `max_turns` caps how many rounds of tool use the node
  gets in one visit; when it runs out the node is asked to write its answer
  from what it has gathered. `max_reentries` (needs `max_turns`) lets it ask
  for that many fresh budgets first when essential work is still missing,
  and `reentry_prompt` (needs `max_reentries`) is your steer for those extra
  rounds.
- **Skills and MCP servers.** A node can name `skills` (skill docs injected
  into its own prompt) and `mcps` (a subset of your already-configured MCP
  servers) — scoped per node, so a node only sees what its job needs.
  A node's MCP tools reach it only when the agent starts the run from a
  chat. A run started by an automation, through the HTTP API or from the
  dashboard executes the node without its MCP tools, and nothing in the run
  says so.
- **A mode.** Non-routing work nodes default to `mode: "build"` (full
  access, neutral posture); routing nodes (those with `on_pass`/`on_fail` or
  `cases`) default to `mode: "read"` (read-only, neutral posture) — a
  deliberate gate that can inspect but not modify, and still delivers its
  verdict. Setting `mode` explicitly overrides either default and is always
  honoured exactly: use `build` for a step that writes, `read` for any
  read-only step; `explore` and `plan` carry interactive framing (e.g. "the
  parent should /build") that can derail a step running unattended. Neither
  `read` nor `explore` can run commands, so a gate that runs a check needs `build`
  (as `debug`'s `verify` does) or a script node.

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
  `.py` under the interpreter durin runs on, `.sh` under `bash`, anything
  else must be directly executable (a shebang line). Use this for anything
  longer than a one-liner.

The I/O contract is plain Unix:

- **stdin** is the previous node's output (the upstream edge text). If the
  script node is the workflow's first node, stdin is the run's task instead,
  exactly as it was passed — the workflow's input/output descriptions are
  framing for agent steps and never reach a script.
- **stdout** becomes the edge text for the next node — capped
  (`workflow.script_output_max_chars`, default 16000 characters; excess is
  truncated with a notice). A script that feeds a dynamic fan-out its list
  must print a JSON array that fits under this cap (see Parallel nodes below).
- **stderr** is for diagnostics only — it never becomes the edge text, though a
  failing gate folds a tail of it into the loop-back feedback so whoever
  re-runs sees why it failed.
- **The exit code decides pass/fail.** On a routing script node
  (`on_pass`/`on_fail`), exit `0` is `PASS`; anything else is `FAIL`. On a
  `cases` (multi-way) script node, the **last line of stdout that equals one
  of its case labels** picks the case, but the process must still exit `0` —
  a non-zero exit there (or on a plain, non-routing script node) ends the run
  as a failure instead of a verdict, since a script that crashed mid-way has
  nothing trustworthy to say.
- **Work dir.** The script's current directory is the run's shared working
  folder — the same folder an agent step with `tools: "default"` reads and
  writes, so a script step can read files an earlier step produced, or leave
  files for a later step.
- **Environment.** A handful of `DURIN_*` variables carry run metadata:
  `DURIN_TASK` (the run's task as passed, capped), `DURIN_RUN_ID`, `DURIN_NODE_ID`,
  `DURIN_ITERATION` (the step's attempt number: one more each time it runs,
  a retry after an error included), and
  `DURIN_WORK_DIR` (the shared working folder's path). The rest of the
  subprocess environment is controlled by `env`: `"clean"` (the default) gives
  the script a minimal allowlist (`PATH`, `HOME`, `USER`, `SHELL`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `DURIN_HOME`) plus those `DURIN_*` vars; `"inherit"`
  gives it the environment your durin gateway runs in, minus any variable that
  holds a stored secret (such as a provider key durin loaded for its model) —
  opt into it only when the script genuinely needs an ambient variable durin
  doesn't forward.
- **Secrets.** Neither `env` mode carries your stored secrets. A script that
  needs a credential names it in `secrets` (e.g. `["GITHUB_TOKEN"]`); each
  must exist in the secret store with the `exec` scope
  (`durin secret grant GITHUB_TOKEN --to exec` adds it) and is injected as an
  env var of that name. A missing secret, or one without that scope, fails
  the node instead of running the script without it.
- **Timeout.** A script node has its own `timeout` in seconds, or falls back to
  `workflow.script_timeout` (default 300s). A script that runs past it is
  killed and the run aborts at that node (retryable with `resume_run_id`) —
  even on a gate, a timeout does not loop back.

A script node takes none of a work node's agent fields (`model`, `persona`,
`prompt`, `tools`, `mode`, `context`, `session`, `skills`, `mcps`, `approval`,
…) — the workflow parser rejects them. It can run as a parallel branch beside
agent branches, but not as a dynamic fan-out `worker`, which must be a work
node.

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

The same swap upgrades the `debug` seed: when you copy it into your own
workflow, replace its agent `verify` gate with a script node running *your*
project's real check (`pytest -q`, `npm test`, `cargo test`) — the seed ships
with an agent gate only because it can't know your command. `execute-plan` has
no separate gate; add a script gate between `implement`'s `DONE` case and
`review`.

## Routing: deciding what happens next

A node can just hand off to the next node (`"next": "other_node"`), or it
can **route** — end its own reply with a verdict that decides where the flow
goes:

- **Binary** (`on_pass` / `on_fail`): the node ends its reply with
  `PASS` or `FAIL`. A `FAIL` can loop back to an earlier node (see Loops
  below), carrying the node's own output as feedback so the step that
  produced the work knows what to fix. Use it for checks that can
  genuinely fail: a check whose "no" is a normal path ("does the note
  exist yet? no → go draft it") belongs in `cases`, because every `FAIL`
  counts as a failed gate when durin looks for workflows to improve.
- **Multi-way** (`cases`): the node declares a set of named outcomes, e.g.
  `{"GROUNDED": null, "MISSING": "plan", "MISUSED": "synthesize"}`, and
  ends its reply with exactly one of those labels. `null` ends the run;
  any other value is the id of the node to go to next. A case named
  `default` catches a reply that matches no label; without one, the run
  aborts.

The verdict is elicited from the model as a **tool call** (not just hoped
for in free text), so a routing node's output is reliable — a pass/fail or
label that cannot be derailed by a stray sentence. durin tells the node to
finish its answer and call its `route` tool with the verdict in the same
reply; that call ends the node's turn, and the text sent with it is the
node's output — for a `FAIL`, the feedback the next step works from. If the
node finishes without calling it, durin asks for the verdict with one more,
forced call. If no call arrives at all, a text-parse fallback applies: a
binary gate passes only when **every line that states a verdict says
`PASS`** (a `FAIL` anywhere wins; checklists, code and quotes don't count),
and a multi-way node takes the **last line that equals one of its case
labels**.

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
nested run: `{"id": "draft", "kind": "subworkflow", "workflow": "<name>",
"next": ...}`. A subworkflow node runs in the **same working folder as the
parent** — it reads and extends the same set of files that earlier nodes
created, so the nested workflow's steps collaborate on the parent's
evolving fileset just as if they were sequential steps in the parent workflow.
The nested run's session traces anchor to the invoking conversation, so its
work is navigable as part of the parent.

## Loops

A `FAIL` (or a case that targets an earlier node) can send the flow back to
redo a step. Loops are bounded so they can't run forever:

- **`max_visits`** on the workflow (default 3) or a specific node caps how
  many passes that node may complete over the whole run, clamped by a hard
  global ceiling (`workflow.max_node_visits`, default 25) no node can exceed
  regardless of what the definition says — a node in a nested sub-workflow
  included. If a node's budget runs out, the run ends with status `exhausted`
  rather than looping silently forever. A step that stopped with an error
  (not a `FAIL` verdict, which is a pass like any other) did not complete a
  pass, so resuming the run retries it even when it failed on its last
  allowed pass.
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

The `execute-plan`, `debug`, `writing-plans`, and `build-specs` seed workflows
ship with persistent sessions on their looping steps (`implement`,
`diagnose`+`fix`, `revise`, and `assemble` respectively) — each carries context
across iterations as the node refines its work in response to loop feedback.

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
  needs to judge its own output, not a shared narrative.) When a run is
  resumed — a failed step retried, or a paused run answered — the shared
  steps from there on still see the discussion the earlier steps had: it
  is read back from the saved conversation of the last shared step that
  finished. If that saved conversation is gone, the run still resumes,
  starting the shared discussion afresh.
- **The shared working folder.** Any node with `tools: "default"` reads
  and writes files in one folder shared by the whole run — so a "write the
  code" step, a "write the test" step, and a "fix the bug" step all see
  each other's files without you wiring that up explicitly. A relative path
  in a node's file tools (`read_file("ticket.json")`) resolves in that
  folder, except one under `workflows/`, `memory/`, `skills/` or another of
  durin's own folders, which resolves from the workspace root; a path into
  another folder at the workspace root, such as a repository checked out
  there, is read from the root when the working folder doesn't have it,
  but an edit or a write there needs the full path.
  `execute_code` scripts run in the folder, but shell commands start in the
  workspace root, so a command that works on the folder's files changes
  into it first or uses full paths. This is what
  lets `execute-plan` and `debug` collaborate on an evolving fileset across
  steps (and across a loop's revisits) instead of handing copies down a
  chain. That folder is normally fresh every run; passing a `work_key` (a
  ticket id, a thread id — anything that names the recurring subject) picks
  a STABLE folder shared by every run *of that workflow* with the same key
  instead, so a node declaring `reuse: "if-unchanged"` (which needs
  `output_schema` and `output_file`) can find and skip work an earlier run on
  the same subject already did. Runs sharing a `work_key` also take turns:
  if one is still going when another with the same key starts, the second
  waits for the first to finish (and then, usually, gets to reuse what it
  just produced) instead of both writing into the same folder at once; after
  30 minutes of waiting it fails. A keyed folder is not pruned along with
  ordinary run folders — it expires on its own after 30 days of no activity
  instead, so a recurring subject you keep coming back to keeps its folder,
  but an abandoned one does not linger forever. An automation's own automatic
  `work_key` (from a channel or webhook trigger) is narrower still: it only
  applies when the trigger's `correlate` pattern captured this key from the
  message — a plain thread reply never becomes a `work_key` on its own.
- **Declared input/output.** A workflow can declare an `input` (optional
  `text` and/or `file`, plus a free-text `description`) and `output`
  descriptor. Declaring `file: true` input means the workflow expects
  files to work on — pass them via `input_files` (absolute paths) when you
  run it, and they land in the shared working folder before the first node
  runs. The `description` fields are framing hints given to every agent
  node (what the run received, what it must deliver) — not enforced, just
  steering. Script nodes never see them: a script gets the task exactly as
  it was passed, so an example in a description can't be parsed as a value.
- **Reading back what a run produced.** A completed run reports its
  `output_dir` (the shared working folder) and the list of files inside it
  (`output_files`, relative paths) — so you know exactly what was created
  or changed and where to find it.

## Parallel nodes

A parallel node runs several branches at once and merges their text
outputs into the next node's input:

- **Static** — `branches` lists the nodes to run, each its own work or script
  node, all seeing the same input (e.g. "review this diff for security /
  performance / readability" as three parallel reviewers).
- **Dynamic** — `worker` names a single work node mapped over a runtime list,
  and `list_from` names the node whose output is that list (e.g. one search
  worker per query the plan step produced). The list holds at most 50 items;
  any beyond that are dropped. When a work node produces it, the list is a
  JSON array (prose or a code fence around it is fine), or newline-separated
  text as a fallback. When a script node produces it, the script's whole
  stdout must be one JSON array (`[]` for an empty list) and must fit under
  the script output cap: a script that prints anything else, or a list larger
  than the cap, stops the run at the parallel node with an error naming both
  nodes, and no worker starts. Each worker receives its item and nothing else — not the
  run's task, not the workflow's input/output descriptions — so keep items
  short (an id or a file name) and put the instructions in the worker's
  prompt.
- **Runtime-selected** — `branches_from` names a node whose output lists which
  declared work or script nodes to run this pass (a JSON array, or
  comma-separated ids on its last line); an optional `branches` list declares
  the pool it may pick from.

`max_concurrency` bounds how many branches or workers run at once; extra ones
queue and run in later waves. Without it, a parallel node runs at most
`workflow.parallel_llm_concurrency` (default 2) agent branches or workers at
once, and at most `workflow.parallel_script_concurrency` (default 4) script
branches; a node's own value replaces both.

For **writing** branches — ones that create or edit files — `reconcile`
decides how their work comes back together:

- `read` (the default) — branches run in the shared folder with no private
  copy; give them a read-only mode, since a writing `read` branch lands its
  changes unchecked.
- `choose` — each branch writes into its own private copy of the run's
  working folder; a judge picks the best one to keep, discarding the rest. A
  `choose` node requires a `criteria` string (how the judge should pick
  the winner).
- `union` — every branch's writes are applied; a genuine conflict (two
  branches wrote *different* content to the same file) aborts the run
  rather than silently picking one.

The private copies live inside the run's own folder (under `.workflow/`, beside
its working folder) and are deleted when the step ends. If the process dies in
the middle of the step, they are removed when the dead run is marked as
crashed, or at the latest when the resumed step starts again; the step never
works with copies an earlier attempt left. Deleting the run's folder deletes
them too.

The kept result is not written when it would turn a file into a folder or a
folder into a file (for example one branch writes `p` and another `p/q`, or a
branch replaces a folder with a file), when it would write or delete a link or
go through one, when a path it writes is not a regular file (a named pipe, say),
when a path lies on another volume mounted inside the working folder, or when a
path cannot be examined (a folder that lost its search permission while the
branches ran, say). The run
then stops at the parallel step the way a `union` conflict does, naming the path:
`parallel node '<id>': the branches' result cannot be written back: '<path>'
would turn a file into a folder or a folder into a file` (or `… is reached
through a link`, `… is a link`, `… is not a regular file`, `… is on another
volume`, `… cannot be examined: <error>`). Nothing is applied.

The same happens when the run's working folder is itself a link, or lies on
another volume than the run's own folder that holds it: `parallel node '<id>':
the branches' result cannot be written back: the run's working folder is a
link`, or `… the run's working folder is on another volume than the run's
area`. Steps that are not a `choose` or `union` step still run in such a folder.

Otherwise the result is written file by file — first the files a branch deleted
(a file that is not there is skipped), then the files a branch wrote — with an
undo record (`pending-apply/` beside the working folder) holding what each
changed file held before. Each file is prepared inside `pending-apply/` and
renamed into place. If writing fails while the run is going — a read-only
folder, a full disk — the undo runs at once and the run stops at the parallel
step with `writing the branches' result back failed: <error>`; retry it once the
cause in the error is gone. When the undo itself cannot finish for the same
cause — on a full disk, after a file was already deleted or changed — the message
goes on with the "could not be undone yet" text below, and the retry first puts
the folder back. If the process dies while the result is being written, the run
undoes the writing when it is resumed, when the next run on the same work key
starts, or when a `choose` or `union` step uses the folder next, so the repeated
step starts from what its first attempt started from.

The undo only touches a file that still holds what the writing left there: it
removes a file the writing created and puts back a file it changed only while
that file holds the content written there with the mode the writing gave it — the
earlier file's mode without its setuid and setgid bits, or, for a file it
created, what the umask allows — and puts back a file it deleted only while
nothing is at that path. A file someone
else changed or made in between — other content, another mode, a folder in its
place, a deleted file the writing had changed — is left as it is and named
in a warning in the log. A change that cannot be told from the writing's own is
undone like it: a file someone wrote with exactly the content and mode the
writing gives it, and a file someone deleted that the writing was deleting too. Folders the writing made are
removed when empty; one that now holds another file — one a detached step wrote
there, say — stays.

When the folder cannot be put back, nothing more is changed and the run stops:

- `the working folder holds changes of an earlier parallel step that could not
  be undone (<path>). It can run once a person has checked the folder and
  removed that file.` — the undo record itself cannot be used, or it lies beside
  a working folder that has been replaced by a link; the run can be retried once
  a person has done that.
- `the working folder holds changes of an earlier parallel step that could not
  be undone yet (<path>): <what stopped it>. A retry undoes them once the cause
  is gone.` — putting a changed file back stopped, for example on a full disk, a
  folder replaced by a link, or a folder whose permissions no longer let its
  files be examined; the run can be retried once the cause is gone.

A run that stops this way before its first step cannot be retried, and its
message says so: `… A new run can start once a person has checked the folder and
removed that file.` or `… A new run undoes them once the cause is gone.` Start a
new run once that is done; it copies its input files as any new run does.

Dynamic fan-out workers always share the folder directly (no per-worker
isolation), so `reconcile` only applies to static and runtime-selected
branches.

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
folder, same node sessions, and the same loop passes already spent —
rather than repeating everything from the start. `writing-plans`,
`build-specs`, `execute-plan`, and `brainstorming` all use this pattern for
their intake step. `resume_run_id` also retries a run that ended `aborted` at
a named node: the failed node re-runs with its exact input, in the same
folder — even when it failed on its last allowed pass, because a step that
stopped with an error used up no pass. When what failed was one branch of a parallel step that merges or
picks between its branches' files, the whole parallel step re-runs. A resume that can't get going — a script the workflow names has gone
missing, say — leaves the run paused as it was, so you can answer it again
once that's fixed. A sub-workflow step's own run is never resumed on its own,
paused or failed: resume the run that called it, which runs the sub-workflow
step again in its own folder. A resume of the sub-workflow's run is refused
with the id of that caller.

**A run cut by a crash or a restart.** When the process running a workflow
dies — the gateway crashed, was restarted, or was killed — the run is marked
`crashed`, and it can be resumed the same way: ask the agent to call
`run_workflow` with `resume_run_id`, or send the run's id as `resume_run_id`
to the run route of the API (no answers needed). The run goes on at the step
that was running when the process died, with the input that step had, in the
same folder. Steps that had finished do not run again; the step that was
running runs again **from its start**, whatever it had done before the
process died. A parallel step runs all its branches or workers again, and a
sub-workflow step starts its child workflow again from its first step.

So a step that acts outside durin — sends a message, posts a comment, writes
to another system — should be safe to run twice. The general way is a script
that checks before it acts (has this comment already been posted?) and does
nothing when it has. Only a script step's own processes are tracked and ended
when a run is found dead (below). A command an agent step started through its
shell tool runs in a session of its own and is neither tracked nor ended: when
that agent step runs again, it may start the command a second time while the
first one is still running. `reuse: "if-unchanged"` does not skip the step that was
cut, since a step entered again is never reused, but it does let the steps a
restarted sub-workflow had already finished skip their work when their input
is unchanged.

A script that was running when the process died is ended before its step runs
again. If something of it may still run (a process durin may not signal, or one
it cannot prove is still the script's, such as a process left in the script's
former process group after the script itself is gone), the resume is refused
and names the process ids that are left. The agent never ends them itself. Check
that they are the script's before ending them (after a restart, a process id can
belong to something else), then resume. The resume is refused the same way when
the run was marked interrupted while the process that ran the workflow still
seems alive, while one of its sub-workflow runs is still recorded as running, or
while a record of one of its scripts cannot be read. That reason names the file:
the agent never deletes it itself and tells you which file it is; remove it once
you have made sure nothing of that script is left, then resume. A resume can also
be refused because someone else resumed the run, or is acting on it, at the same
moment, or because its record was removed; the reason says which. A crashed run is also refused, with
the reason, when it was recorded by an older
version that did not keep the step it was in, when that step is no longer in
the workflow, when its working folder has been pruned, or when a later run
with the same `work_key` has worked in its folder since. A crashed run the
agent started is resumed only through the agent's `run_workflow` when its
workflow, or a sub-workflow it calls, has a step with `mcps`: the dashboard
and the API refuse it, because only the agent can give those steps their MCP
tools. This applies to crashed runs only. A paused run (a question or an
approval) and a run that aborted at a step can be answered or retried from the
dashboard or the API; when the agent started such a run, its later `mcps`
steps then run without their MCP tools, as in any run started from the
dashboard.

**Where a resumed run's result goes.** A run the agent started from a chat
reports to that chat how a resume made elsewhere came out, with the limits
listed below. Resumed from the dashboard or the API, the result arrives there
as a message the agent reads and summarizes, saying the run was resumed from
the dashboard or the API; the dashboard or API caller also gets the result. Resumed by the agent
from another chat, the starting chat gets the same message, saying the run was
resumed from another conversation, and the chat that resumed it gets the result
as usual. Resumed from the chat that started it, the result arrives once, as for
any run. Only a resume that actually ran reports: one that was refused, or that
another resume beat, reports nothing. Answering an approval from the dashboard
counts: the chat receives the result of the steps that ran after it, and a
rejection, or an approval of the last step, is reported as the run's end. Runs
started by an automation, the dashboard or the API report nothing to a chat this
way.

A crashed run an [automation](automations.md) started is not resumed by hand:
the agent's `run_workflow`, the dashboard and the API all refuse it. The
automation records its own run as interrupted, does not resume the crashed
one, and its next run starts anew. Only a crashed run is refused on this
account.

A crashed run whose record lost what the run was started with (its folder,
its task, where it was started from) is refused as incomplete on every
surface; start a new run instead. That refusal does not say whether a script
of the old run is still working, so check before the new run repeats its
work.

Known limits: files that a `read` parallel step's branches or a fan-out's
workers wrote in the shared folder before the process died are still there when
the step runs again. A process that dies right after a `choose` or `union` step
finished writing its result, before the run moved on, runs the step again on a
folder that already holds that result. A run started from a chat is not
reported to that chat when whoever resumed it stops waiting for it before it
ends, such as an agent turn stopped while it waited on a resumed run. Nor is it
reported there when it was started from a chat the gateway serves and resumed
from the terminal UI: its result is in the run's record and in the
conversation that resumed it.

**Approval gates.** Set `"approval": true` on a work node to pause the run
after it for a person's sign-off: the run ends `needs_input` with the node's
output as the proposal. A reply of `approve`, `yes` or `ok` continues past the
node, `reject` or `no` cancels the run, and any other reply is revision
feedback — the node re-runs with it and pauses again with a new proposal. The
agent's `run_workflow` cannot resume an approval pause; a person answers it
from the dashboard or through the API. An approval node can't route, be
`detached`, use `context: "shared"`, or run as a parallel branch.

A stale pause with no good answer left — the environment it needs is gone,
the person who'd know has moved on — doesn't have to sit there forever:
cancel it instead, from the Runs pane or the Pending page (below), next to
the resume form. Cancelling finalizes the run as `cancelled` in place — its
trace and working folder stay for the record — and it can no longer be
resumed, so the dashboard asks you to confirm before it goes through.

A run that ended `aborted` because a step failed shows a **Retry the failed step**
button in its detail, in the Runs pane and in the editor's test-run panel. It
resumes the run with no new answer, which re-runs the failed step as described
above. A run an [automation](automations.md) started has no such button: the
dashboard offers no retry for it, and running the automation again starts a new
run. A sub-workflow step's own run has none either: retry the run that called it.

A run cut short because the process running it died (status `crashed`) says so
in its detail: the banner reads "Run interrupted" and the step that was running is
labelled "interrupted" rather than "failed". The detail offers **Resume at the
interrupted step**, which picks the run up at that step. The button is left out
for an automation's run, for a sub-workflow step's own run (resume the run that
called it), and for a run recorded without its step. If the run cannot be resumed
(for example, a script the dead process started is still running), the dashboard
shows the reason and the run stays as it is.

If a workflow declares `input: {file: true}` and you run it with no
`input_files`, the run ends `needs_input` immediately (before any node
runs) asking for the files — and if you do pass files but one is missing
or two collide on the same filename, the run is rejected outright
(`aborted`, naming the problem) before anything is created. Both checks
happen before the run is even recorded, so a bad call leaves no trace to
clean up.

## Where things live

- **Definitions:** `<workspace>/workflows/<name>.json` — a small
  git-versioned directory: edits from the editor or the agent's workflow
  tools are committed as they happen, and a run the agent starts first
  snapshots any hand edits, so you can see how a workflow evolved over time.
- **Run records:** each run writes a manifest under
  `<workspace>/workflows-runs/<name>/<run_id>.json` with the outcome and a
  per-node trace (status, verdict, and the session each node produced). A
  run started by a subworkflow node records its caller's run id, so the
  dashboard can mark it as a sub-run of the parent. The web dashboard's
  Workflows pane reads these to show run history. The manifest appears when
  the run starts executing: durin executes a bounded number of runs at once,
  so in a burst (many API launches or background runs together) the extra
  ones wait their turn and start, in launch order, as others finish; the
  gateway log notes each run that has to wait. A run a chat is waiting on
  (run in the foreground) and a scheduled automation don't count against
  that bound and never wait: each starts at once. Neither does a run paused
  on your input.
- **Node sessions:** every node's conversation is a normal, searchable
  durin session (`workflow:<run_id>:<node_id>:...`), so a node's reasoning
  is navigable after the fact the same way a sub-agent's is.
- **Retention:** run working folders
  (`<workspace>/.workflow/<run_id>/work/`, gitignored) are pruned each time
  a run starts, keeping the `workflow.keep_runs` (default 20) most recent
  across all workflows; run manifests are pruned to that many per workflow.
  A running run, or one waiting on your input, is never pruned — resume or
  cancel a paused one to let it retire like any other finished run. Copy out
  any deliverable you need to keep before its run ages out.

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

Renaming a workflow moves its run history, the workflows that call it and the
automations that run it to the new name. A workflow with a run in progress
cannot be renamed: the editor shows an error saying that the workflow has a
run in progress and can be renamed once that run has ended. A run whose
process died does not hold the rename.

The Workflows section's **Runs** pane is where to look across *all* workflows
and origins (sessions, HTTP, cron, the editor) at once: a global, filterable
run feed, with any run stranded on `needs_input` surfaced in a tray up top —
questions, a resume form, and a Cancel control included — so you can find and
answer (or give up on) a paused run without knowing which session or workflow
started it. A badge on the sidebar's **Workflows** button shows the count of
stranded runs even while you're elsewhere in the app. A run an
[automation](automations.md) paused is excluded from this tray and badge — it
belongs to the Automations section's own "Needs you" inbox instead, so the
same paused run never asks for attention in two places at once; when one
exists, a small link under the tray takes you there. The same stranded runs,
with their resume and Cancel controls, also show on the dashboard's
**Pending** page, next to everything else that waits on you.

Opening an `aborted` run in the Runs pane offers **Retry the failed step**,
which re-runs the step that failed in the same run. A run an automation started
has no retry: running the automation again starts a new run. A sub-workflow
step's own run has no retry either; retry the run that called it.

A `crashed` run shows an interrupted banner and offers **Resume at the interrupted
step** under the same conditions; see above.

Each workflow also has a self-improvement mode (`"improvement_mode"`,
`manual` by default, or `auto`): a
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
