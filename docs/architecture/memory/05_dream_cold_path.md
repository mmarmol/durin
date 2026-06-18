---
title: Dream — cold-path consolidation
version: 0.2
status: current — describes the shipped system (post-migration, 2026-06-06)
last_updated: 2026-06-06
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md, 02_indexing.md
related: 03_search_pipeline.md, 04_agent_tools.md
---

# Dream — cold-path consolidation

This document specifies **Dream**, the cold-path process that turns raw
experience into structured knowledge: it reads each **session's** conversation
turns and extracts canonical entity pages (`memory/entities/<type>/<slug>.md`),
dedups duplicate entities, mines reusable procedures into skills, and curates
the always-on pinned guidance. Every pass is asynchronous and may take seconds
to minutes.

**Invariant:** nothing Dream does blocks the hot path. The agent's search and
tool calls run against whatever state Dream has produced so far. Dream improves
the state over time.

> **Migration note.** This doc was rewritten after the legacy
> `DreamConsolidator` / `DreamRunner` cluster was deleted. The old model
> consolidated `memory/episodic/` *fragments* into pages via a JSON-Patch apply
> pipeline, a per-entity `dream_processed_through` cursor, an
> episodic→archive lifecycle, and a per-write threshold trigger. **All of that
> is gone.** The settled model has five passes — in run order **extract /
> derived_from / skill / refine / always_on** — that read from **sessions**, write through `memory_writer`'s CAS
> commit path, and treat fragments as a separate raw track.

---

## 0. The two-track model — entities vs fragments

durin keeps **two disjoint memory tracks**. Dream operates on one of them.

| | Entity track (Dream's domain) | Fragment track (raw, NOT consolidated) |
|---|---|---|
| Storage | `memory/entities/<type>/<slug>.md` | `memory/episodic/*.md` (+ references in `memory/references/`) |
| Producer | The agent authors name/aliases/relations/body via `memory_upsert_entity`; **Dream extracts attributes** from sessions | `/remember` (user-authored facts) + session-close summaries |
| Consolidated by Dream? | **Yes** — extract / refine / always_on / skill passes | **No.** Fragments are never folded into pages |
| Lifecycle | Pages evolve via CAS writes; duplicates merged via absorb | Append-only; archived only by an explicit `memory_forget` / webui action |

**Why fragments are not consolidated (N3 + N4).** The only live fragment
producers are `/remember` (episodic facts the user explicitly wrote — "the
curator must never touch") and session-close summaries. They are raw user
material, not candidates for graduation into entity pages. The new extract
dream builds entities from **sessions** (the conversation transcript), not from
fragments. Consequently:

- **There is no per-entity `dream_processed_through` cursor.** It was removed
  (N3) — nothing advanced it and nothing should. The extract pass tracks
  progress with a **per-session** cursor instead (§3.2).
- **There is no episodic→archive consolidate-and-archive lifecycle** (N4).
  `archive_episodic` is a manual operation only (`memory_forget` / webui). If
  episodic volume ever matters, a size/age **cap** is the lever — not
  auto-archive.

Both tracks remain fully searchable from write time via FTS + vector + grep
(`02_indexing.md`, `03_search_pipeline.md`). Dream improves the *entity* track;
it does not gate recall of either track.

---

## 1. Scope and non-scope

### In scope

- The five dream passes, in run order: **extract**, **derived_from**,
  **skill-extract**, **refine**, **always_on**.
- Their triggers: the daily CRON + the two REACTIVE triggers
  (post-compaction / session-close), serialized by `ReactiveDreamGate`.
- The config block (`memory.dream.*` + nested `auto_absorb`).
- The per-session extract cursor (idempotency).
- Entity deduplication via the absorb-judge LLM step (refine pass).
- Telemetry emitted by the passes.

### Out of scope

- The hot-path read of entity pages — see `03_search_pipeline.md` and
  `04_agent_tools.md`.
- The indexer re-deriving LanceDB/FTS5 rows after a write — see
  `02_indexing.md`.
- The schema of entity pages, per-field author precedence, and the CAS write
  path — see `01_data_and_entities.md` and `memory_writer` (`02_indexing.md`).
- The fragment track (`/remember`, session summaries) — see §0 and
  `04_agent_tools.md`.

---

## 2. The five passes

All entry points live in `durin/memory/dream_passes.py`:
`run_extract_pass`, `run_derived_from_pass`, `run_refine_pass`,
`run_skill_extract_pass`, and `run_always_on_pass` (the always_on pass is
implemented in `durin/memory/always_on_dream.py` and re-exported through that
module).

| Pass | Entry point | Reads | Writes | Cadence |
|---|---|---|---|---|
| **extract** | `run_extract_pass` | each session's new turns | entity attributes (author `dream`) via `memory_writer` | frequent (cron + reactive) |
| **derived_from** | `run_derived_from_pass` | each session (entities authored + references ingested) | `derived_from` links (author `dream`) via `memory_writer` | daily cron |
| **skill-extract** | `run_skill_extract_pass` | recent sessions | `skills/<name>/SKILL.md` via `skill_write` | daily cron |
| **refine** | `run_refine_pass` | entity pages (alias overlap) | merges duplicate pages (absorb) | daily cron |
| **always_on** | `run_always_on_pass` | feedback entities (stance/practice/feedback) | flips `always_on` flags | daily cron |

The **extract** pass is the only one wired to the reactive triggers;
derived_from, refine, skill-extract, and always_on run on the daily cron (and on
manual `durin memory dream`). See §6 for trigger wiring.

The **derived_from** pass is the catch/repair for entity→source-document links:
`memory_upsert_entity(derived_from=...)` is the primary write-time path
(`04_agent_tools.md` §3); this pass links entities the agent authored without a
source by reasoning over the session (which ingested document each entity was
distilled from). It is idempotent and cheap — a session whose authored entities
are already linked, or that ingested no references, is skipped with no LLM call
(`durin/memory/derived_from_dream.py`).

### 2.1 Extract pass — sessions → entity attributes

`run_extract_pass(workspace, *, llm_invoke=None, model=None, max_seconds=0,
discover=True)` iterates every `memory/sessions/*.jsonl`. For each session it
calls `run_extract_for_session` (`durin/memory/extract_runner.py`):

```
1. Load the session jsonl → (metadata, messages). messages[i] is turn i+1.
2. Read the per-session extract_cursor (§3.2). If total turns <= cursor, skip
   (no new turns).
3. Take the new turns (messages[cursor:]) and render them as text.
4. STAGE 1 (extract) — the entities the agent AUTHORED in those turns:
   entity_refs_in_messages() scans the tool_calls for memory_upsert_entity and
   collects each call's `ref` argument.
5. For each such ref, call extract_entity(...) (§2.2).
6. STAGE 2 (discover, when discover=True) — discover_entities(...) scans the
   SAME turns for durable facts about entities the agent did NOT upsert and
   creates/updates them as dream-authored pages, skipping refs handled in
   stage 1 and tombstoned refs.
7. Advance the extract_cursor to `total` (per-batch).
```

**Stage 1 is precise**: it only enriches entities the agent explicitly upserted
via `memory_upsert_entity` in the new turns; it never creates an entity from
scratch (which is why the extract prompt omits an `existing_uris` block — audit
A5).

**Stage 2 (mention-based discovery)** closes the gap stage 1 leaves: durable
facts the agent *mentioned* but never upserted used to never become entities, so
the graph grew only by agent initiative (a knowledge refinery, not a builder).
`discover_entities()` (`durin/memory/extract_dream.py`) makes one LLM call over
the new turns and writes proposals as `author="dream"` (so user/agent values
keep precedence). The discovered `name` is set via `write_entity(name=...)`,
which is **last-writer-wins, not precedence-arbitrated** — a later explicit
agent/user correction simply overwrites a discovered guess. Precision lives in
the discovery prompt (durable identity-class facts only — identity, roles,
relationships, commitments, life events; exclude ephemeral chatter), and
`memory.dream.discover` telemetry measures it. It is **ON by default**
(`memory.dream.discover_enabled`): the failure mode is additive (a low-signal
page, overridable + `git revert`), milder than a destructive merge. Residual
duplicate risk (a discovered slug colliding with the agent's later canonical
ref) is dedup's job — handled author-time by `memory_search` and write-time by
the refine pass — not stage 2's.

Per-session cursors make the pass **idempotent**: a session with no new turns
is skipped; a re-run re-processes nothing already seen. One bad session never
aborts the whole pass — exceptions are collected into `out["errors"]` and the
loop continues.

`max_seconds` (0 = unbounded) is a hard wall-clock cap. When elapsed time
crosses it the pass yields **after the current session** (it emits
`memory.dream.max_seconds_reached` and breaks); the per-session cursor resumes
the remainder on the next trigger. The cron and reactive callers pass
`memory.dream.max_seconds_per_run` here (default 600s).

### 2.2 `extract_entity` — the core extractor

`extract_entity(workspace, entity_ref, turns, *, llm_invoke, model, source_ref)`
(`durin/memory/extract_dream.py`) is the per-entity unit:

```
1. Honour deletion: if is_deleted(workspace, entity_ref) (§2.13 tombstone),
   return without re-creating the entity. The user overrides by explicitly
   re-authoring.
2. Load the existing page (or a fresh EntityPage if none).
3. Build the extract prompt (build_extract_prompt): the entity ref + name, its
   EXISTING attribute keys (for key reuse), its body, and the turns.
4. Invoke the LLM. parse_attributes() tolerantly parses the JSON object
   (strips code fences, runs json_repair) and keeps ONLY scalar / list-of-
   scalar values — prose blobs and nested dicts are dropped.
5. If no attributes parsed, return a no-op.
6. Build one FieldPatch(kind="attribute", author="dream", source_ref=...) per
   attribute and apply via memory_writer.write_entity(..., create=True).
7. Emit memory.dream.patch_applied (one per entity).
```

The extract dream owns the **attribute schema** (author `dream`); the agent owns
name / aliases / relations / body (design §2.6/§2.7, decision b). Per-field
precedence (user > dream > agent) means a user-set attribute is never
overwritten by extraction. The prompt's rules are conservative: only facts
explicitly stated in the turns, reuse an existing key when the meaning matches,
scalars only, JSON-only output.

`source_ref` is the session-turn marker
`[[sessions/<stem>.md#turn-<total>]]`, recorded in the field provenance so a
later reader can trace an attribute back to its origin turn.

### 2.3 Skill-extract pass — sessions → reusable procedures

`run_skill_extract_pass(workspace, *, provider=None, model=None,
max_sessions=3)` mines the newest sessions for a **reusable procedure** and
writes it as a skill (design §2.7). Unlike the other passes it is **agentic**:
it spins up an `AgentRunner` with `ReadFileTool`, `EditFileTool`, and
`SkillWriteTool`, and a system prompt instructing it to call `skill_write` only
when a recurring step-by-step procedure appears (reuse/extend an existing skill
instead of duplicating; do nothing on a one-off). It is a sync wrapper over the
async runner so the cron can call it in a thread. Telemetry:
`memory.dream.skill_extract` with `skills_touched`.

### 2.4 Refine pass — dedup duplicate entities

`run_refine_pass(workspace, *, llm_invoke=None, model="glm-5.1", enabled=True,
confidence_threshold=95, min_age_hours=0)` is the periodic graph-hygiene pass.
It delegates to `run_refine` (`durin/memory/refine_dream.py`), which reuses the
absorb machinery (`EntityAbsorption.find_candidates` + `absorb` +
`absorb_judge.judge_pair`). Full details in §8.

The critical gate is **`enabled`** (audit A1): it is wired from
`memory.dream.auto_absorb.enabled`, which is **OFF by default**. When disabled,
`run_refine_pass` short-circuits — **no judge, no merge** — and logs the manual
path. Duplicates are then surfaced on demand via `durin memory absorb-suggest`
and merged with `durin memory absorb`. When enabled, the pass judges
alias-overlap pairs and auto-merges those that pass the threshold + quarantine.

### 2.5 always_on pass — curate the pinned guidance

`run_always_on_pass(workspace, *, token_budget=1500, types=FEEDBACK_TYPES,
llm_invoke=None, model="glm-5.1")` (`durin/memory/always_on_dream.py`) curates
which **feedback entities** (`stance` / `practice` / `feedback`) are injected
into EVERY prompt — the pinned "Always-on guidance" block built by
`principal.build_pinned_context`. The agent authors these feedback entities as
the user gives standing guidance; this pass owns the `always_on` flag (design
§2.11, audit A4).

```
1. Gather all stance/practice/feedback entity pages; render + token-count each.
2. Rank them best-first via an LLM judge (_RANK_PROMPT) that DROPS items
   contradicting a higher-priority item. Fallback when no LLM / a single item:
   user_authored first, then most-recently-updated.
3. Fit the ranked list into token_budget (a hard ceiling; a later smaller item
   may still fit, so overflow SKIPS rather than breaks).
4. Mark the survivors always_on=true via principal.mark_always_on; unmark the
   rest. Only the flag changes — no entity is ever deleted.
```

Because only the flag flips, a pruned or contradicted item returns
automatically when the budget frees up or the conflict resolves.
`token_budget` is wired from `memory.dream.always_on_token_budget` (0 disables
the pin). Telemetry: `memory.dream.always_on` (`selected` / `pruned` /
`dropped` / `tokens`).

---

## 3. Concurrency, throttle, and the per-session cursor

There is **no cross-process `.dream.lock` and no `min_seconds`/cursor file** —
those belonged to the deleted `DreamRunner`. The settled model uses two much
simpler mechanisms.

### 3.1 `ReactiveDreamGate` — in-process lock + throttle

`ReactiveDreamGate` (`dream_passes.py`) guards the reactive triggers. The
post-compaction / session-close hooks each fire on a daemon thread in the
gateway process; without a guard a burst of session closes would spawn
overlapping extract passes. One `ReactiveDreamGate` instance is shared by both
reactive triggers in a gateway.

```python
gate = ReactiveDreamGate()
skip = gate.try_begin(min_seconds)   # "" → run; "locked"/"throttled" → skip
if not skip:
    try:
        run_extract_pass(...)
    finally:
        gate.end()
```

`try_begin` is **non-blocking**:

- returns `"locked"` when a pass is already running (the lock is held), or
- returns `"throttled"` when a pass ended within `min_seconds`
  (`memory.dream.min_seconds_between_runs`, default 300s; 0 disables), or
- returns `""` (run) otherwise.

A skipped run is harmless: the per-session cursor means the skipped session's
turns are picked up by the in-flight pass, the next reactive trigger, or the
daily cron. The skip is recorded as `memory.dream.throttled` with the reason.
**The daily cron is never throttled** — it does not go through the gate.

### 3.2 Per-session extract cursor

The extract pass tracks progress with a **per-session** cursor (not per-entity).
It is an integer turn count stored in the session's sidecar
`<stem>.meta.json` under the `derived.extract_cursor` key
(`get_extract_cursor` / `set_extract_cursor` in `extract_runner.py`). The pass
processes only turns after the cursor and advances it to the total turn count
after each batch. Because the extractor's writes are idempotent under per-field
precedence, re-running over the same turns is safe.

This is the **only** cursor in the dream system. The per-entity
`dream_processed_through` cursor was removed (N3).

### 3.3 Write concurrency is handled downstream

The passes do not hold a lock for their writes. Every entity write goes through
`memory_writer.write_entity`, which is an optimistic multi-writer path:
read page@HEAD → apply `FieldPatch`es with precedence → build a commit via
dulwich plumbing (no working-tree mutation) → `refs.set_if_equals` CAS → retry
on contention. The refine merge uses `write_files_cas` to commit the canonical
edit, the absorbed deletion, and the archive copy in one atomic CAS commit.
This replaces the legacy working-tree apply pipeline and its G3 race (see
`02_indexing.md` and `memory_writer`).

---

## 4. Writes, provenance, and git history

The extract pass does **not** run a bespoke JSON-Patch apply pipeline. It builds
`FieldPatch` objects and hands them to `memory_writer.write_entity`. There is no
`===PATCH===` / `===BODY_DELTA===` / `===COMMIT===` envelope, no
`dream_patch_parser`, no `dream_apply`, no `.md.bak` rollback — those modules
were deleted with the legacy consolidator.

**Provenance** is carried per field: each `FieldPatch` has a `source_ref`
(the session-turn marker) and an author (`dream`), persisted in the entity
page's provenance map by `memory_writer` / `field_patch` (`01_data_and_entities.md`).

**Git history.** Entity writes are committed by `memory_writer` with author
`durin-memory <memory@durin.local>`; absorb merges are committed by the
absorption path with author `durin-dream <dream@durin.local>` and structured
trailers (`Absorbed:`, `Into:`, `Reason:`, `Judge-Confidence:`) that
`durin memory history` / `durin memory revert` parse. The commit messages are
assembled inline by `memory_writer` / `absorption` (the legacy
`dream_commit_message.py` builder was deleted — audit B1).

**Git substrate (`durin/utils/git_repo.py`).** This history sits on a generic,
subsystem-agnostic helper that wraps **dulwich** (pure-Python git) so durin needs
no system `git` binary. It is deliberately not memory-specific: any subsystem can
use it for local versioned storage. The contract:

- **Strictly local.** durin never configures a remote; there is no push/pull. Sync
  is a user opt-in concern, out of scope for the substrate.
- **The owning subsystem decides** when to `init()` (idempotent), the commit
  author/email (e.g. `durin-memory` vs `durin-dream` above), and which structured
  trailers to emit.
- **Commit-message convention:** `subject` + blank line + `body` + blank line +
  RFC822 `Key: value` trailers. The trailers are parsed back on read, so a caller
  can query e.g. which entities a commit touched without re-parsing the prose body.

### 4.1 Per-entity relation cap (soft / alert-only)

`memory_writer.write_entity` counts an entity's relations before and after the
patches and calls `check_relation_cap` (`durin/memory/entity_relation_cap.py`,
soft 50 / hard 200; audit A3). Crossing the **soft** cap emits
`memory.entity_relation_cap_warned`; crossing the **hard** cap emits
`memory.entity_relation_cap_rejected`. Both also log. **No write is blocked and
no relation is dropped** — this is alert-only "de momento". Enforcing the hard
cap (actually rejecting) is a one-line flip in
`memory_writer._emit_relation_cap` when mega-hubs prove real. (The telemetry
docstrings still describe the deleted dream's VALIDATION-rollback behaviour;
the live behaviour is the soft path described here.)

---

## 5. Configuration

All knobs live under `memory.dream.*` in `durin/config/schema.py`
(`MemoryDreamConfig`), with the dedup knobs nested under `auto_absorb`
(`AutoAbsorbConfig`).

| Setting | Default | Effect |
|---|---|---|
| `memory.dream.enabled` | `true` | Master switch for the cron + both reactive triggers. Manual `durin memory dream` works regardless. |
| `memory.dream.cron` | `0 3 * * *` | Cron expression for the daily extract / skill / refine / always_on pass. |
| `memory.dream.post_compaction` | `true` | Arm the reactive extract trigger on session compaction. |
| `memory.dream.on_session_close` | `true` | Arm the reactive extract trigger on session close. |
| `memory.dream.model_override` | `null` | Override the dream model. Resolved via `resolve_memory_model` (precedence: `aux_models.memory` → this → caller default). |
| `memory.dream.min_seconds_between_runs` | `300` | Throttle window for the reactive `ReactiveDreamGate`. 0 disables. The cron is never throttled. |
| `memory.dream.max_seconds_per_run` | `600` | Hard wall-clock cap per extract pass; the pass yields after the current session and the per-session cursor resumes the rest. 0 = run to completion. |
| `memory.dream.always_on_token_budget` | `1500` | Token ceiling for the always-on pin (per-turn cost). 0 disables the pin. |
| `memory.dream.auto_absorb.enabled` | `false` | Master switch for the refine pass's auto-merge. OFF → judge+merge skipped; use `durin memory absorb-suggest` / `absorb`. |
| `memory.dream.auto_absorb.confidence_threshold` | `95` | LLM-judge confidence floor (0-100) for an auto-merge. |
| `memory.dream.auto_absorb.min_age_hours` | `24` | Quarantine: skip a candidate pair if either page is younger than this (created_at, falling back to updated_at). 0 disables. |

The model used by every pass is resolved by
`durin.memory.model_resolve.resolve_memory_model(config)`:
`agents.aux_models.memory` (preset or inline `model`) → `memory.dream.model_override`
→ `None` (the pass's own default, which is `glm-5.1` via `default_llm_invoke`).

---

## 6. Triggers

Dream runs in response to three trigger kinds:

| Trigger | When | Source | Passes run |
|---|---|---|---|
| **cron** | Daily at `memory.dream.cron` (default 03:00) | Cron scheduler (`cli/commands.py`, system job `memory_dream`) | extract → skill-extract → refine → always_on |
| **post_compaction** | After a session is compacted | `agent.consolidator.on_post_compaction` hook (`cli/commands.py`) | extract only |
| **session_close** | When a session ends | `agent.on_session_close` hook (`cli/commands.py`) | extract only |
| **manual** | `durin memory dream` | `cli/memory_cmd.py` | extract → skill-extract → refine → always_on |

### 6.1 Daily cron

Registered in `cli/commands.py` as the system job `memory_dream` (id +
schedule from `memory.dream.cron`, only when `memory.dream.enabled`). The
`on_cron_job` handler intercepts `job.name == "memory_dream"` and runs the four
passes directly (NOT through the agent loop), each offloaded to a thread with
`asyncio.to_thread` so the cron loop stays responsive during the LLM calls:

```
ex = run_extract_pass(ws, model, max_seconds=memory.dream.max_seconds_per_run)
sk = run_skill_extract_pass(ws, model)
rf = run_refine_pass(ws, model, enabled, confidence_threshold, min_age_hours)
ao = run_always_on_pass(ws, model, token_budget=always_on_token_budget)
```

It also runs the skill-curation step (`curate_catalog`) afterward (out of scope
for this doc). The whole handler is wrapped so a failure logs and never crashes
the cron loop.

### 6.2 Reactive triggers (extract only)

When `memory.dream.enabled` and (`post_compaction` or `on_session_close`),
`cli/commands.py` constructs one shared `ReactiveDreamGate` and a `_spawn_dream`
closure. Each trigger fires `_spawn_dream(trigger, session_key)`, which launches
a daemon thread that:

1. Calls `gate.try_begin(min_seconds_between_runs)`; on a non-empty skip reason
   emits `memory.dream.throttled` and returns.
2. Otherwise runs **only** `run_extract_pass(ws, model,
   max_seconds=max_seconds_per_run)`.
3. Calls `gate.end()` in a `finally`.

The hooks are attached with `hasattr` guards
(`agent.consolidator.on_post_compaction`, `agent.on_session_close`) so test
scaffolds without those attributes still work. Refine / skill / always_on stay
on the daily cron — the reactive path is intentionally extract-only, because the
fresh signal is the new conversation turns and refine/always_on are
workspace-wide passes that don't need per-session cadence.

### 6.3 Manual

`durin memory dream` (`cli/memory_cmd.py`, `cmd_dream`) runs the full sequence —
extract, skill-extract, refine (gated by `auto_absorb.enabled`, with the
disabled path printing the absorb-suggest hint), then always_on — and prints a
one-line summary. The optional `entity` argument is accepted for back-compat but
**not used** by the new passes (extract discovers entities from sessions);
`--dry-run` is unsupported and prints a notice.

---

## 7. (removed) Archive workflow

The legacy fragment→page consolidate-and-archive lifecycle **does not exist**
(N4). Dream does not consume episodic entries, so there is nothing to archive on
apply. `archive_episodic` is a manual operation only, reachable via
`durin memory forget` (which refuses entity pages) and the webui. Entity pages
have their own lifecycle: `durin memory absorb` (merge → the absorbed page moves
to `memory/archive/entities/<type>/<slug>.md`) and `durin memory revert`
(undo). See `01_data_and_entities.md` for archive folder semantics and
`02_indexing.md` for how the indexer drops archived rows.

---

## 8. Entity dedup — the refine pass + absorb-judge

The refine pass (`run_refine` in `durin/memory/refine_dream.py`) is durin's
entity-dedup engine. It is gated by `memory.dream.auto_absorb.enabled`
(§2.4 / §5) and, when enabled, judges alias-overlap candidate pairs with an LLM
and merges the ones the judge calls the *same* identity.

### 8.1 Candidate discovery

`EntityAbsorption.find_candidates()` (`durin/memory/absorption.py`) returns
pairs of entities that share at least one alias. The signal is the alias index:
any alias key that resolves to more than one entity ref contributes a candidate
per pair, sorted so pairs with more shared aliases come first (stronger signal).

### 8.2 Filters before the judge

`run_refine` walks the candidates and skips a pair (each with a
`memory.absorb.skipped` event carrying the reason) when:

| Reason | Condition |
|---|---|
| `cross_type` | the two refs have different entity types (e.g. `person:` vs `project:`) |
| `tombstoned` | the pair was recorded in `.refine_tombstones.json` because the user previously rejected/un-merged it (`is_tombstoned`); refine never re-merges it |
| `load_failed` | one of the two pages couldn't be loaded |
| `user_managed` | either page is `author == "user_authored"` — user-managed pages are left alone (design §2.4) |
| `quarantine` | `min_age_hours > 0` and either page is younger than the window (`_too_fresh`, using `created_at` → `updated_at`; no timestamp = treated as old, fail-open) |

### 8.3 The judge

For a surviving pair, `judge_pair` (`durin/memory/absorb_judge.py`) is called
with both pages, their shared aliases, the canonical/absorbed refs, and each
page's **file mtime** (`_page_mtime`, audit N7a — fed so the judge can reason
about staleness like "observed years apart"). The judge:

- loads its prompt template from `durin/templates/dream/absorb_judge.md` (the
  largest fenced block in the doc),
- renders each page as a block (file-mtime line, aliases, identifiers, body),
- treats alias overlap as *evidence, not proof* — the prompt defaults to
  `different` when content evidence is thin (mitigates the high blast radius of
  a bad merge),
- parses a markdown envelope (`===VERDICT===` / `===CONFIDENCE===` /
  `===REASONING===` / `===END===`),
- retries up to `max_retries` (default 2) on a parse failure,
- returns a `JudgeResult(verdict, confidence, reasoning)` or raises
  `JudgeError` (the caller treats that as "skip this candidate").

The verdict vocabulary is **identity-judgement**, not action-prescription:

| Verdict | Meaning |
|---|---|
| `same` | A and B describe the same real entity |
| `different` | distinct entities that merely share an alias (e.g. two people named "Marcelo") |
| `unclear` | the judge couldn't decide |

Every judged pair emits `memory.absorb.judged` (`verdict` + `confidence`) for
threshold tuning.

### 8.4 The merge

Refine merges only when `verdict == "same"` AND
`confidence >= confidence_threshold`; otherwise the pair is kept separate. On a
merge it calls `EntityAbsorption.absorb(canonical, absorbed, reason="refine",
judge_reasoning=..., judge_confidence=...)` and emits
`memory.absorb.auto_merged`. `absorb` (`absorption.py`) does a deterministic
structural merge via `_merge_pages`:

- **aliases**: union (plus the absorbed page's display name as an alias);
- **attributes**: union, canonical wins on a key conflict;
- **relations**: union, deduped by `(to, type)` — dropping these would silently
  disconnect the merged entity (G1);
- **body**: append the absorbed body under a `## Absorbed from <ref>` section;
- **provenance / identifiers**: union;
- archive marker fields are not propagated.

The merge commits **three file ops in one CAS commit** via `write_files_cas`
(canonical updated, absorbed deleted, archived copy written to
`memory/archive/entities/<type>/<slug>.md` with `archived_into`), author
`durin-dream`. It then refreshes the in-memory alias index (add canonical, drop
absorbed) and updates the vector index (delete the absorbed row, **re-upsert the
canonical** with the merged body so semantic search isn't stale — glm peer
review C4). The operation is idempotent: if the absorbed page is already
archived, `absorb` is a no-op and returns `None`.

**Recovery:** `cd memory && git revert <merge_sha>`. `durin memory revert`
wraps this; for an auto-absorb commit (trailer `Reason: auto`) it emits
`memory.absorb.reverted` — the regret-rate signal for tuning the threshold.
After a user reverts/un-merges, the deletion path records a `do_not_absorb`
tombstone (`add_tombstone`) so the next refine doesn't undo the revert.

### 8.5 Self-consistency bias

When the judge model equals the extract model, the judge tends to confirm. The
prompt instructs the model to peer-review critically and prefer `unclear` over a
forced confirmation; using a different model for the judge reduces this further.
(Note: `AutoAbsorbConfig` no longer carries a `judge_model` knob — it was a
marginal-value field and was removed in audit B3; the refine pass uses the
resolved memory model for both extraction and the judge.)

---

## 9. Schema notes

Entity pages parse missing `attributes` / `relations` / `provenance` as empty
(`EntityPage`), so older pages keep working; the extract pass adds those fields
as a natural side effect of the first write. There is no bulk migration step —
the legacy "v1 → v2 lazy on Dream touch" framing no longer applies because the
write path is `memory_writer`, not a versioned apply pipeline. Search reads any
page regardless. See `01_data_and_entities.md` for the page schema.

---

## 10. Schema drift control

Within a single entity, the extract prompt's `EXISTING ATTRIBUTE KEYS` block
(`build_extract_prompt`) instructs the LLM to reuse a key when the meaning
matches, which prevents `email` → `e-mail` → `correo` drift within that entity.
Scope is per-entity: drift *between* entities is tolerated; workspace-wide
attribute-key normalization is a deferred concern (see
`08_scope_and_discarded.md`). Cross-entity *identity* duplication is the refine
pass's job (§8), not the prompt's.

---

## 11. Telemetry

All events are best-effort (telemetry never breaks a pass) and defined in
`durin/telemetry/schema.py`:

| Event | Emitted by | Key fields |
|---|---|---|
| `memory.dream.start` | extract + refine | `kind` (`extract`/`refine`) |
| `memory.dream.end` | extract + refine | `kind`, `duration_ms`; extract: `entities_consolidated` / `entities_failed` / `sessions` / `yielded`; refine: `merged` / `kept` / `candidates` |
| `memory.dream.patch_applied` | `extract_entity` (one per entity) | `entity_ref`, `ops_applied`, `trigger="extract"`, `committed`, `source_ref` |
| `memory.dream.skill_extract` | skill-extract pass | `skills_touched`, `duration_ms` |
| `memory.dream.max_seconds_reached` | extract pass (cap hit) | `kind`, `max_seconds`, `elapsed_ms`, `sessions_done` |
| `memory.dream.throttled` | reactive gate skip | `trigger`, `reason` (`locked`/`throttled`) |
| `memory.dream.always_on` | always_on pass | `selected`, `pruned`, `dropped`, `tokens`, `duration_ms` |
| `memory.absorb.judged` | refine (per judged pair) | `canonical`, `absorbed`, `verdict`, `confidence` |
| `memory.absorb.auto_merged` | refine (per merge) | `canonical`, `absorbed`, `confidence` |
| `memory.absorb.skipped` | refine (per skipped pair) | `canonical`, `absorbed`, `reason` |
| `memory.absorb.reverted` | `durin memory revert` (auto-absorb only) | `canonical`, `absorbed`, `original_sha`, `confidence` |
| `memory.entity_relation_cap_warned` | `memory_writer` (soft cap crossed) | `entity_ref`, `current_count`, `new_count` |
| `memory.entity_relation_cap_rejected` | `memory_writer` (hard cap crossed; alert-only) | `entity_ref`, `current_count`, `new_count` |

The event **names** are reused from the legacy dream so existing dashboards keep
counting; the producers and field shapes are the new passes'.

---

## 12. Failure modes

The deleted consolidator's structured failure enum
(`DreamApplyFailureKind` = `validation | patch_runtime | round_trip | io`) and
its per-entity quarantine are **gone** — there is no JSON-Patch apply pipeline
to fail in those ways. The settled model's failure handling is simpler and
spread across the passes:

| Failure | Behaviour |
|---|---|
| **Extract: bad session** | caught per session in `run_extract_pass`; recorded in `out["errors"]`; the loop continues to the next session. |
| **Extract: empty / unparseable LLM output** | `parse_attributes` returns `{}`; `extract_entity` returns a no-op; the per-session cursor still advances (the turns held no extractable attributes). |
| **Extract: write contention** | `memory_writer.write_entity` retries the read-apply-CAS loop; raises only after `_MAX_RETRIES` (high contention). |
| **Extract: time budget** | the pass yields after the current session (`memory.dream.max_seconds_reached`); the per-session cursor resumes the rest next trigger. |
| **Refine: judge error** | `JudgeError` → the pair is skipped (`reason=judge_error:...`); other pairs proceed. |
| **Refine: disabled** | `run_refine_pass` returns immediately with `disabled=True`; nothing is judged or merged. |
| **Reactive: overlap / burst** | `ReactiveDreamGate.try_begin` returns `locked`/`throttled`; the run is skipped (`memory.dream.throttled`); the cursor makes it harmless. |
| **Relation cap** | soft/hard cap crossings emit telemetry + log; the write still proceeds (alert-only, §4.1). |
| **Cron handler** | the whole `memory_dream` branch is wrapped; a failure logs (`memory_dream cron failed`) and never crashes the cron loop. |

A skipped or failed extract is never data loss: nothing was archived, the page
on disk is untouched, and the per-session cursor only advances over turns that
were actually processed. The next trigger or the daily cron retries.

---

## 13. Module-level decisions

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Passes | Four: extract (frequent), skill-extract, refine, always_on. The legacy single consolidator was deleted. | §2 |
| 2 | Triggers | cron (all four) + reactive post_compaction/session_close (extract only) + manual (all four). The per-write threshold trigger was removed. | §6 |
| 3 | Concurrency | In-process `ReactiveDreamGate` (lock + throttle) for reactive triggers; the daily cron is never throttled. No cross-process lock file. | §3.1 |
| 4 | Cursor | Per-**session** `extract_cursor` in `<stem>.meta.json`. The per-entity `dream_processed_through` cursor was removed (N3). | §3.2 |
| 5 | Write path | `memory_writer.write_entity` (FieldPatch + per-field precedence + dulwich CAS). No JSON-Patch envelope, no `.md.bak` rollback. | §4 |
| 6 | Fragments | Raw track (`/remember` + session summaries), NOT consolidated; no episodic→archive lifecycle (N3/N4). | §0, §7 |
| 7 | Extract prompt | Entity ref/name + existing attribute keys + body + turns. No `existing_uris` block — extract enriches only agent-upserted entities, so it can't create duplicates (A5). | §2.2 |
| 8 | Dedup (refine) | Alias-overlap candidates → cross-type/tombstone/user-managed/quarantine filters → LLM judge (`same`/`different`/`unclear`) → merge on `same` + `confidence ≥ threshold`. Gated OFF by default by `auto_absorb.enabled` (A1). | §8 |
| 9 | always_on | Distil feedback (stance/practice/feedback): LLM rank + drop contradictions + fit token budget; flip `always_on` flag only (A4). | §2.5 |
| 10 | Relation cap | Soft 50 / hard 200, alert-only in `memory_writer`; no write blocked, no data dropped (A3). | §4.1 |
| 11 | Recovery | `git revert` of the absorb commit (`durin memory revert`); `do_not_absorb` tombstone stops re-merge. | §8.4 |
| 12 | Model | `resolve_memory_model`: `aux_models.memory` → `dream.model_override` → default (`glm-5.1`). | §5 |

---

## 14. Cross-references

- Entity page schema, per-field author precedence, provenance:
  `01_data_and_entities.md`.
- The CAS write path (`memory_writer`, `field_patch`, `write_files_cas`) and the
  indexer re-deriving FTS/vector after a write: `02_indexing.md`.
- Archive folder semantics (exclusion from search): `01_data_and_entities.md`.
- Hot-path read of entity pages: `03_search_pipeline.md`.
- The agent's write tools (`memory_upsert_entity`, `memory_ingest`,
  `memory_forget`) and the `/remember` fragment producer: `04_agent_tools.md`.
- Telemetry event catalog: `07_telemetry_and_observability.md` and
  `durin/telemetry/schema.py`.
