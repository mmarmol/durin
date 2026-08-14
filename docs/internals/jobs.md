---
title: "Jobs — the long-work registry"
---

# Jobs — the long-work registry

## 1. Purpose

The job registry is durin's home for work too long to run inside a single
turn — minutes to hours of CPU, running in its own process, with progress a
user can come back and check. It is generic over a `kind` column, but it has
exactly one client today: transcribing a scanned PDF with local OCR (see
[Memory: agent tools](memory/04_agent_tools.md#memory_ingest) for the coverage
measurement and inline-vs-job decision that leads here). The registry does not
know anything about OCR, pages, or PDFs — it only knows a job has a total unit
count and a per-unit result, which is enough to host a second, unrelated kind
of long-running work without changing the table or the surfaces that render
it.

Jobs sit next to two other things durin already had, and deliberately does
not replace: [cron](cron.md) is for work triggered on a *schedule*; a
[workflow](workflow.md) run is a *graph* of nodes. A job is neither — it is a
flat queue of identical units (pages, today) that one worker process drains
in order, with no branching and no timer. Something that needs scheduling or
routing stays a cron job or a workflow; something that is just "a lot of the
same small task, done once, started now" is a job.

## 2. Mental model

**Progress is persisted per unit, not per job.** `jobs` holds one row per job
(status, label, the kind-specific payload, how many units total and done);
`job_units` holds one row per finished unit, keyed `(job_id, unit)`. Two
things fall out of that split: a worker killed partway through resumes at the
first undone unit instead of starting over, and a gateway restart can requeue
an interrupted job without losing the units it already finished.

**The worker is a separate OS process, not a coroutine.** `spawn_ocr_job`
enqueues a row and starts `python -m durin.jobs.ocr_worker <job_id>` as a
detached subprocess. The gateway's event loop never runs the transcription
loop itself; it only reads the rows the worker writes.

**"Job" names two different things at two layers, on purpose.** The tray, the
`tasks` tool, and the HTTP route all merge three *categories* of background
work — sub-agents, workflow runs, and jobs — under a `kind` field whose value
is always the literal string `"job"` for anything sourced from this registry.
The registry's *own* `kind` column is one layer more specific: it names which
**client** of the job registry this row belongs to (`"ocr"` today). Reading
"job kind" in this doc always means the second one unless stated otherwise.

## 3. Diagram

```mermaid
flowchart TD
    ING["ingest_artifact\ndurin/memory/ingestion.py"] -->|NeedsOcrJob\nover the inline budget| SPAWN["spawn_ocr_job\ndurin/jobs/spawn.py"]
    SPAWN -->|enqueue| REG[(jobs.db\nJobRegistry)]
    SPAWN -->|Popen, detached| WORKER["ocr_worker.run_job\npython -m durin.jobs.ocr_worker &lt;id&gt;"]

    WORKER -->|claim: queued to running| REG
    WORKER -->|record_unit per page| REG
    WORKER -->|finish: done / failed| REG
    WORKER -->|assemble source.md| SIDECAR["ingested/&lt;id&gt;/source.md"]
    WORKER -->|index_ingested_entry| LIB["memory/references/&lt;slug&gt;.md\n+ FTS row + vector chunks"]

    GWSTART(["gateway start\nAgentLoop.run()"]) -->|reconcile: running rows\nwith a dead pid or too old| REG
    GWSTART -->|respawn per orphan| WORKER

    TASKS["tasks tool / GET /api/v1/tasks\ncollect_tasks()"] -->|read-only| REG
    TASKS --> TRAY["webui work panel\nWorkItemCard (kind=job)"]

    CANCEL["tasks(action=stop)"] -->|status=cancelled| REG
    WORKER -.checks status\nat each claim + page boundary.-> REG
```

## 4. How it works

### The table shape

Two tables, created in `JobRegistry.__init__` if missing:

```
jobs(
  id           TEXT PRIMARY KEY,   -- 12-char hex, uuid4-derived
  kind         TEXT NOT NULL,      -- "ocr" today
  status       TEXT NOT NULL,      -- queued | running | done | failed | cancelled
  label        TEXT NOT NULL,      -- human line for the tray; passed in by the caller, not
                                    -- derived from the payload path (that path is the entry's
                                    -- normalized copy, "source.<ext>" for every document)
  payload      TEXT NOT NULL,      -- kind-specific JSON: for OCR, {path, pages, sidecar_dir}
  session_key  TEXT,               -- who asked, for session-scoped listing
  units_total  INTEGER NOT NULL,
  units_done   INTEGER NOT NULL,
  pid          INTEGER,            -- claiming worker's pid; read by reconcile's liveness
                                    -- check, cleared on finish/cancel
  created_at, started_at, ended_at REAL,
  error        TEXT
)

job_units(
  job_id  TEXT NOT NULL,
  unit    INTEGER NOT NULL,        -- 1-based page number, for OCR
  text    TEXT NOT NULL,
  PRIMARY KEY (job_id, unit)
)
```

Both tables live in one SQLite database (`$DURIN_HOME/jobs/jobs.db`, resolved
by `durin.config.paths.jobs_db_path`), opened through
`durin.utils.sqlite_util` — the same WAL + `busy_timeout` + `BEGIN IMMEDIATE`
retry helpers the FTS5 index uses, so the gateway process and a worker
subprocess can both write it safely. See
[Concurrency](concurrency.md#sqlite-databases-with-cross-process-writers)
for the shared mechanism.

### Enqueueing and claiming

`spawn_ocr_job` (`durin/jobs/spawn.py`) writes the row with `status="queued"`
and immediately `Popen`s the worker with `start_new_session=True`, so the
worker outlives the request that launched it. If the `Popen` call itself
fails (e.g. the OS is out of processes), the row is left `queued` — the next
gateway start's reconcile sweep picks it up, since a `queued` job with no
`pid` reads the same as an orphaned one.

A worker claims its own job rather than trusting the caller: `JobRegistry.claim`
runs `UPDATE jobs SET status='running', pid=? WHERE id=? AND status='queued'`
and reports whether that specific call won. This closes the gap between
"the worker checked the status" and "the worker started acting on it" — a
`stop` request landing in that gap flips the row to `cancelled` first, the
conditional `UPDATE` then matches zero rows, and the worker sees it lost the
claim and returns without transcribing anything.

### Per-unit persistence and resumption

`record_unit` upserts one `job_units` row and then **recounts**
`units_done` from `COUNT(*)` rather than incrementing it. Re-recording a page
a resumed worker redoes must not inflate progress past the true count, which
an increment would risk. A worker's first act after claiming is to diff the
job's full unit list against `done_units()` and only work through what is
missing — so a worker restarted after crashing at page 380 of 400 transcribes
20 pages, not 400.

That unit list is widened before the diff. An OCR job's payload pages are a
floor rather than the whole set: the conversion path stops confirming empty
pages the moment a document is plainly over the inline budget (see
[the memory tools doc](memory/04_agent_tools.md)), and its cheap probe can miss
an empty page outright. So the worker runs the accurate per-page extraction
itself — seconds against the minutes of OCR that follow — and unions the empty
pages it finds with the payload's and with any page an earlier run already
recorded, which is what keeps the progress denominator from falling below its
numerator. The widened count is reported through `set_units_total`, a
`claim`-style guarded `UPDATE` that only writes while the row is still this
worker's running job, and only on a successful recompute.

That pass is **load-bearing, not best-effort**. Without it there is no way to
tell a floor from a finished list, so a worker that cannot read the document
fails the job with a recorded reason rather than transcribing the floor: doing
the latter would publish a forty-page book holding six pages and report it
`done`, with the pages left out decided by omission and nobody downstream able
to tell. The failure costs little — every path that enqueues one of these jobs
read the document with the same extractor first, and a document that stays
unreadable already fails this job at the sidecar step, which reads it the same
way. Failing at the check says so before spending the OCR, and leaves
`units_total` untouched rather than rewriting it down to the floor.

### Restart reconciliation

A gateway restart used to strand a `process_registry.py`-tracked background
`exec` forever (documented there as a known limitation); jobs fix this for
their own rows. On every gateway start, `AgentLoop.run()`
(`durin/agent/loop.py`) calls `JobRegistry.reconcile(alive=_pid_alive)` before
serving any traffic. `reconcile` finds every `running` row and requeues it
(clearing `pid`/`started_at`, keeping its finished units) when either:

- its `pid` is not alive (`os.kill(pid, 0)` raises), or
- it has been `running` longer than `RECONCILE_AGE_S` (six hours) regardless
  of what its `pid` looks like — a rebooted host restarts pid allocation from
  a low number, so a stale row's `pid` can coincidentally match a real,
  unrelated live process, and liveness alone would then misreport the job as
  fine.

Each requeued job is individually handed to `respawn()`, which re-`Popen`s
the right worker module for the job's `kind`. Both the reconcile call and each
individual `respawn` are wrapped so that one bad orphan (an unrecognized
`kind`, a `Popen` failure) cannot stop the rest from being respawned or take
the gateway down on the way up.

### Cancellation

`JobRegistry.cancel` sets `status='cancelled'`. There is no signal sent to the
worker process — cancellation is cooperative, and the worker only ever learns
of it by re-reading the row: at the top of every per-unit loop iteration, so a
cancel lands at the next page boundary at the latest, with a page already
being transcribed finishing first. The one case that loop never reaches — every
requested page already transcribed by an earlier run — is covered instead by
`claim`'s own conditional `UPDATE ... WHERE status='queued'` (see
[above](#enqueueing-and-claiming)): a cancel that lands between the worker's
pre-claim status read and the claim itself makes that `UPDATE` match zero
rows, so the worker still notices it lost the job rather than marking a
cancelled run "done".

The per-page loop is not the last thing a worker does, so its checks are not
the last word either: the sidecar write and the Library indexing that follow
chunk, index and embed a whole book, which is minutes with no cancellation
check in it. `finish` is therefore conditional the same way `claim` is —
`UPDATE ... WHERE id=? AND status='running' AND pid=?` — and returns whether
it wrote anything. A cancel landing in that window survives, `ended_at`
included, and the worker reports the row's real status instead of its own
result. The `pid` clause covers the other way two writers meet: the age
fallback above can requeue a job whose worker is genuinely alive, and the
superseded worker must not flip the outcome of the one that now owns the row.

Today the only caller is the agent's own `tasks` tool (`action="stop"`) — see
[Configuration and surfaces](#6-configuration-and-surfaces) below. There is no
webui button wired to it yet; the tray is read-only.

### The subprocess worker

`ocr_worker.run_job` (`python -m durin.jobs.ocr_worker <job_id>`) is a
subprocess rather than an in-process task for three reasons, in order of
weight: the OCR engine's ONNX runtime memory is released when the process
exits instead of sitting resident in the gateway for its whole uptime (the
gateway has already taken an OOM from resident model memory once); a
CPU-bound transcription loop never touches the gateway's asyncio event loop;
and progress needs no purpose-built IPC channel — the worker writes
`job_units` and `units_done` directly, and the gateway (or a CLI/API reader)
just reads the same database.

On success, a job whose payload carries a `sidecar_dir` (an ingested Library
entry) has the worker assemble `source.md` there: the document's per-page
text with every OCR'd page filled in, joined the same way the inline
conversion path joins pages — the same sidecar shape either way, just
produced later.

The worker then hands that entry back to the memory layer —
`index_ingested_entry` (`durin/memory/ingestion.py`) — which stores it as a
Library reference and indexes it, exactly as an inline ingest does at ingest
time. This is the step that makes a transcribed document *findable*, and it
belongs at the end of the job because this process is where the text first
exists: `memory_ingest` returned before any of it had been produced. The jobs
layer knows only that name; what a Library entry is and how it is indexed
stays behind it (see [Memory: agent
tools](memory/04_agent_tools.md#memory_ingest)).

A failure at any step — the completeness check, a page's transcription, the
sidecar assembly, or the Library indexing — is recorded as `status="failed"`
with an `error` string rather than left `running` forever; a resumed run that
finds nothing left to transcribe would otherwise hit the same broken step again
on every retry. The completeness check and the indexing both count as steps
that can fail the job on purpose: a job reported "done" whose document cannot
be found, or whose book was published with the pages nobody confirmed left out,
is the exact outcome each step exists to prevent. A pass that transcribed nothing at all fails there too — the inline
conversion path already refuses a document with no extractable text, and an
empty Library entry is no more useful for having arrived late.

### How a new job `kind` is added

The registry, `reconcile`, and the tray/tool rendering are already generic;
adding a second kind touches only the pieces that are inherently
kind-specific:

1. Define the payload shape that kind needs (OCR's is
   `{path, pages, sidecar_dir}`).
2. Write a worker entry point runnable as `python -m <module> <job_id>`,
   following `ocr_worker.run_job`'s claim → per-unit loop → `finish` shape.
3. Write a `spawn_<kind>_job` helper that enqueues and `Popen`s that worker —
   there is no shared generic "enqueue and launch" entry point today, only
   one worked example to copy (`spawn_ocr_job`); the registry's `enqueue`
   underneath it is already kind-agnostic.
4. Add a branch to `respawn()` (`durin/jobs/spawn.py`) so a restart can bring
   the new kind's worker back.

Nothing else needs to change to reach the tray: `collect_tasks` and
`TasksTool` render any registry row from its generic fields (label, status,
`units_total`/`units_done`) with no kind-specific branching, and the webui's
`WorkItemCard` already renders a job's progress as "N of M" — though its
current label text says "pages" specifically (`work.pages` in the i18n
catalog), so a kind whose units are not pages would want its own progress
copy there.

## 5. Key types and entry points

| Symbol | File | Role |
|---|---|---|
| `Job` | `durin/jobs/registry.py` | Frozen dataclass mirroring one `jobs` row. |
| `JobRegistry` | `durin/jobs/registry.py` | `enqueue`, `get`, `list_for_session`, `claim`, `set_units_total`, `record_unit`, `units`, `done_units`, `finish`, `cancel`, `reconcile`. |
| `RECONCILE_AGE_S` | `durin/jobs/registry.py` | Six hours — the pid-liveness-is-unreliable fallback age used by `reconcile`. |
| `spawn_ocr_job` | `durin/jobs/spawn.py` | Enqueues an OCR job and `Popen`s its worker; called from `ingest_artifact` when a document needs more OCR than the inline budget. |
| `respawn` | `durin/jobs/spawn.py` | Re-launches the worker for a job `reconcile` already requeued; dispatches on `job.kind`. |
| `run_job` | `durin/jobs/ocr_worker.py` | The OCR worker's whole lifecycle: claim, per-page transcribe loop, sidecar assembly, Library indexing, finish. |
| `transcribe_page` | `durin/memory/ocr.py` | Renders one PDF page (`pypdfium2`) and runs it through the lazily-constructed, process-local `RapidOCR` engine. |
| `index_ingested_entry` | `durin/memory/ingestion.py` | Turns a finished `ingested/<id>/` entry into an indexed Library reference; the one memory-layer call the worker makes on success. |
| `collect_tasks` | `durin/agent/background_tasks.py` | Merges sub-agents, workflow runs, and jobs into the one list the tray and `tasks` tool both read. |
| `TasksTool` | `durin/agent/tools/tasks_tool.py` | Agent-facing `tasks` tool: `list` / `status` / `stop`, across all three background-work categories. |
| `TasksService` | `durin/service/tasks.py` | `GET /api/v1/tasks` — read-only HTTP mirror of `collect_tasks`. |
| `WorkItemCard` | `webui/src/components/work/WorkItemCard.tsx` | Renders one tray row; a `kind="job"` item shows "N of M pages". |
| `jobs_db_path` | `durin/config/paths.py` | `$DURIN_HOME/jobs/jobs.db`. |

## 6. Configuration and surfaces

### Config keys

Job-triggering behavior is configured on the OCR client, not the registry
itself — the registry has no config of its own.

| Key | Default | Effect |
|---|---|---|
| `documents.ocr.enabled` | `false` | Whether scanned pages are transcribed at all; also gates installing the `[ocr]` extra. |
| `documents.ocr.inline_max_pages` | `5` | Pages needing OCR at or under this are transcribed inline; more than this enqueues a job instead. |

### Agent tool (`tasks`)

`action="list"` shows every sub-agent, workflow run, and job in the calling
session; `action="status"` with an id gives a job's page progress
(`done/total`) alongside its status and age, plus the recorded reason when it
failed; `action="stop"` cancels a job (cooperative — see above). The id namespace is shared and resolved
automatically across all three kinds, so the agent does not need to know
which one it is asking about.

### API routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/tasks?session=<key>` | Read-only, session-scoped list merging sub-agents, workflow runs, and jobs. No job-specific write route exists yet — cancellation is agent-tool-only. |

### Webui and TUI

The web dashboard's work panel (opened from the chat header, or its
collapsed work-strip form) renders a job exactly like a workflow run or
sub-agent card, with a job-specific footer line showing pages transcribed of
pages total, and — for a failed job — the reason its worker recorded, verbatim
and untranslated. It is read-only today: no cancel button is wired to a job
row.
The Textual TUI's WORK sidebar section tracks only workflow runs and
sub-agents; it does not read the job registry, so a running job is not
visible there.

## 7. Rationale

**Per-unit rows, not a per-job progress counter alone.** A single integer
column could report "38 of 400" just as well right up until a worker dies —
at which point nothing says *which* 38 pages are done, so a resume has to
guess or restart from zero. Keeping each finished unit's own row is the
difference between "resume" and "restart with extra steps."

**A SQLite table, not a per-job JSON manifest file.** Workflow runs and cron
use one file per run/store, rewritten atomically under a file lock — a good
fit when one process owns a run's file for its lifetime. A job's row is
written by two different processes across its life (the gateway that
enqueues it, the worker that claims and updates it, possibly a second worker
after a respawn), and read by a third (the tray) while the second is still
writing — exactly the concurrent-writer situation `sqlite_util`'s WAL +
`BEGIN IMMEDIATE` pattern exists for, already proven by the FTS5 index.

**A subprocess, not an `asyncio.Task` on the gateway loop.** See
[above](#the-subprocess-worker) — memory release on exit and event-loop
isolation are both really "a CPU/memory-heavy model should not live inside
the one process serving every other session," which is also why Dream runs
as its own subprocess rather than a coroutine.

**Generic `kind` from day one, with exactly one caller.** The alternative —
hardcoding "OCR job" throughout — would mean redesigning the table, the
tray rendering, and the `tasks` tool the moment a second kind of long work
shows up. A `kind` column plus a JSON `payload` is the minimum generality
that avoids that redesign; it is not a speculative plugin framework, since
nothing here discovers or registers kinds dynamically — `respawn`'s dispatch
is a plain `if`, extended by hand.
