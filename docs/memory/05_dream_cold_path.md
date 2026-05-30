---
title: Dream — cold-path consolidation
version: 0.1-draft
status: current — describes the shipped system (P11 era, 2026-05-30)
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md, 02_indexing.md
related: 03_search_pipeline.md, 04_agent_tools.md
---

# Dream — cold-path consolidation

This document specifies Dream, the cold-path process that consolidates raw observations (`memory/episodic/`, `memory/stable/`) into canonical entity pages (`memory/entities/<type>/<slug>.md`). Dream is the only place in the system where an LLM operates on memory; every step is asynchronous and may take seconds to minutes per pass.

**Invariant:** nothing Dream does blocks the hot path. The agent's search and tool calls run against whatever state Dream has produced so far. Dream improves the state over time.

---

## 1. Scope and non-scope

### In scope

- Trigger types and dispatch.
- Lock + throttle + cursor mechanics.
- Consolidator prompt (extended for v2 schema with attributes + relations + provenance).
- JSON Patch-based apply (diff over the existing entity page, not full rewrite).
- Entity deduplication via the absorb-judge LLM step.
- Archive workflow for consolidated episodic.
- Schema upgrade (v1 entity pages → v2 lazy on touch).
- Schema drift control (prompt includes existing schema).
- Git commits (one per apply, structured message).
- Failure modes and recovery.

### Out of scope

- The hot-path read of consolidated entity pages — see `03_search_pipeline.md` and `04_agent_tools.md`.
- The indexer re-deriving LanceDB/FTS5 rows after Dream apply — see `02_indexing.md` §6.
- The schema of entity pages themselves — see `01_data_and_entities.md` §3.5.

---

## 2. Triggers

Dream runs in response to one of six trigger labels. Each is recorded in telemetry and in the resulting git commit:

| Trigger label | When | Source | Frequency (typical) |
|---|---|---|---|
| `threshold` | Per-entity pending count crossed `threshold_entries` after a `memory_store` write | `memory_store` tool | A few per day during active use |
| `post_ingest_threshold` | Per-entity pending count crossed `threshold_entries` after a `memory_ingest` write | `memory_ingest` tool | A few per ingest burst |
| `cron_daily` | Daily scheduled run (default: 03:00 local). Picks up entities whose pending count is below threshold but non-zero, or which have been quiet for > X days. | Cron scheduler | Once per day |
| `session_close` | At the end of a long session (> 50 turns), trigger a pass on all entities mentioned in the session. | AgentLoop teardown | Once per session close |
| `post_compaction` | After conversation compaction emits a `_last_summary`, trigger a pass on entities mentioned in the new summary. | Compaction handler | A few per long session |
| `manual` | Operator command: `durin dream run` (optionally with `--entity <uri>` filter). | CLI | On demand |

The two threshold labels (`threshold` and `post_ingest_threshold`) exist as separate strings so telemetry can distinguish which write surface triggered the consolidation. Behavior of the consolidation itself is identical — the labels only differ for diagnostics.

Triggers are not mutually exclusive — multiple can request a run concurrently. The lock (§4) serializes them.

### 2.1 Threshold trigger dispatches asynchronously

A critical implementation detail: the `threshold` trigger fires from inside `memory_store` and `memory_ingest` tools (after the write completes). To avoid blocking the agent's tool response on a multi-second Dream pass, the dispatch runs on a **daemon thread**:

```python
# durin/memory/threshold_trigger.py (simplified)
import threading

def maybe_dispatch_threshold_dream(workspace, entities, dream_config, ...):
    if accumulated_count(entities) < threshold:
        return
    thread = threading.Thread(
        target=lambda: DreamRunner(workspace, **dream_config).run(trigger="threshold"),
        daemon=True,
    )
    thread.start()
```

Implication: when `memory_store` returns to the agent, the threshold-triggered Dream pass may still be running in the background. The agent does NOT wait for consolidation to complete. The `memory.dream.start` / `memory.dream.end` telemetry events (doc 07 §6) fire asynchronously to the original tool call.

If the process exits before the daemon thread completes, the partial pass is abandoned — but no data is lost because the cursor is only advanced after a full successful apply (§4.3). The next trigger picks up the same pending entries.

The `cron_daily`, `session_close`, `post_compaction`, and `manual` triggers do NOT use daemon threading — they run in the foreground of whatever process invoked them (the cron scheduler, the agent loop teardown, etc.).

### 2.2 Threshold counting logic

The threshold trigger checks **per-entity** activity, not workspace-wide. The function `count_pending_for_trigger(workspace, entity_filter=...)` (in `durin/memory/threshold_trigger.py`) returns `{entity_uri: count}` summing two contributions:

| Contribution | What it counts | Why it counts |
|---|---|---|
| **Episodic post-cursor** | Episodic entries newer than the entity's `dream_processed_through` cursor that are tagged with the entity in their `entities:` field | These are the entries Dream WILL consolidate. Direct signal that consolidation is due. Uses the same discovery helper as Dream itself to keep cursor semantics identical (no double-counting). |
| **Corpus tagged with the entity** | Corpus entries that mention the entity in their `entities:` field | Dream does NOT consolidate corpus — an ingested document is already canonical-ish on its own. But if the user has been actively dropping documents about an entity, that's a signal the entity is "hot" and worth consolidating its episodic backlog. |

**Stable entries do NOT count.** Stable is user-marked durable; Dream does not consume it (§7).

When any entity's count reaches `threshold_entries` (default 5), the trigger dispatches a Dream pass **filtered to that single entity** (`entity_filter=<uri>`), not a workspace-wide pass. This keeps each threshold-triggered run small and focused — the entity that just received the N+1 write is the one consolidated.

If multiple entities cross the threshold from a single write (e.g., `memory_store` with `entities=[person:marcelo, project:durin]` and both were already at N-1), each one dispatches its own daemon thread. The Dream lock (§4.1) serializes them.

**Configuration knobs** (under `memory.dream.*`):

- `threshold_entries`: minimum count to trigger (default 5)
- `min_seconds_between_runs`: throttle window (default 300s) — applies AFTER the per-entity check; multiple per-entity dispatches within the throttle window are skipped with `reason=throttle`

---

## 3. The Dream pass — high-level flow

### 3.0 Components

Dream is organized in two layers, separated by concern:

| Layer | File | Role |
|---|---|---|
| `DreamConsolidator` | `durin/memory/dream.py` | **Pure consolidation logic.** Takes one entity + a batch of post-cursor entries, returns a `ConsolidationResult` (page diff + commit message). Stateless. No lock, no throttle, no telemetry. Tests use this directly to validate logic without operational scaffolding. |
| `DreamRunner` | `durin/memory/dream_runner.py` | **Production orchestrator.** Holds the lock, throttle, telemetry, batching, and the optional absorb-judge pass. Wraps `DreamConsolidator` — instantiates one per entity and delegates the actual LLM call + parse + apply. |

**In production, only `DreamRunner` is invoked.** Triggers from §2 dispatch to it; `memory_store` / `memory_ingest` call it via `threshold_trigger.py`; the CLI command `durin dream run` calls it. Nothing else in production talks to `DreamConsolidator` directly.

**`dream.py` also exports** helper types used elsewhere (without going through Runner): `EntryRef` (referenced by CLI display code), `default_llm_invoke` (the LLM helper, also used by `query_rewriter.py`), `DreamError`.

The rest of §3-§6 describes what happens during a Runner pass; the LLM call, response parsing, and apply pipeline are implementation details delegated to `DreamConsolidator`.

### 3.1 Pass sequence

A single Dream pass:

```
1. Trigger fires (with optional entity_filter)
2. Throttle check: if last run was < MIN_INTERVAL ago, skip with reason="throttle"
3. Discover pending entities (those with > 0 post-cursor entries) →
   apply entity_filter if provided
4. Acquire lock at memory/.dream.lock
   - if lock held by another process → skip with reason="concurrent_lock"
   - if lock is stale (> STALE_LOCK_SECONDS old, default 10 minutes) → take over
5. For each pending entity (sequential, not parallel):
   a. Load existing entity page (or create placeholder if new)
   b. Load pending entries (post-cursor episodic/stable/corpus)
   c. Build the consolidator prompt (§5)
   d. Invoke LLM (default: glm-5.1 via durin.security.secrets)
   e. Parse response into ConsolidationResult (page diff + commit message)
   f. Apply: validate, write, archive consumed episodic, advance cursor
   g. Run absorb-judge pass if alias overlap detected
   h. Trigger index re-derivation for the changed entity page
6. Release lock
7. Emit telemetry: entities_consolidated, entities_failed, duration_ms
```

Sequential per-entity is deliberate. Parallel apply across entities can race on shared aliases or on the alias index; serialized is simpler and the entire pass is cold-path anyway.

---

## 4. Lock + throttle + cursor

### 4.1 Lock

File: `memory/.dream.lock` (process-local, mtime-based liveness).

Contains:
```json
{
  "pid": <int>,
  "trigger": "threshold|cron|...",
  "started_at": "ISO timestamp",
  "host": "<hostname>"
}
```

Acquisition:
- `acquire_lock`: write file with `O_EXCL`. If exists, check mtime — if older than `STALE_LOCK_SECONDS` (default 600s = 10 min), assume crashed previous run and take over.
- `release_lock`: delete the file. Always called from a `finally` block.

This is the SAME lock that the indexer (`02_indexing.md` §6.3) coordinates with via mtime comparison. No new lock infrastructure.

### 4.2 Throttle

Throttle prevents bursty triggers (e.g., 10 `memory_store` calls in a row, each firing threshold-trigger) from running 10 Dream passes back-to-back.

Mechanism:
- File `memory/.dream.last_run` carries the timestamp of the most recent successful run.
- `min_seconds_between_runs` (default **300s = 5 min**, configurable) is the throttle window.
- If a trigger fires within the window, it is recorded in telemetry as `skipped, reason=throttle` and not executed.
- `manual` trigger bypasses throttle.

This is a soft optimization — missing one trigger doesn't lose data (the next non-throttled trigger picks up the same pending entries).

### 4.3 Cursor

Each entity page has `dream_processed_through` in its frontmatter (`01_data_and_entities.md` §3.4): an ISO timestamp or msg_idx. Episodic/stable/corpus entries with timestamps **strictly after** the cursor are "post-cursor" — they are candidates for the next Dream pass.

After successful apply, the cursor is moved to the timestamp of the latest consumed entry in this batch (`batch_last_ts`). This guarantees:
- No re-processing of already-consumed entries in subsequent passes.
- New entries that arrived during the LLM call (race-safe) will be picked up on the next pass.

If apply fails (LLM error, parse error, validation error), the cursor is NOT advanced. The entries remain post-cursor and the next pass tries again.

---

## 5. The consolidator prompt (v2)

The prompt is the single most important LLM-facing surface in the system. It is the canonical text in `durin/templates/dream/consolidator.md`. This section specifies the prompt's structure and the contract it establishes with the LLM.

> **Implementation status:** §5 and §6 describe the format that **shipped** in Phase 1.9 (commit `6aafc3f`, 2026-05-28). The Dream consolidator now emits `===PATCH===` + `===BODY_DELTA===` + `===COMMIT===` markers with a JSON Patch list, parsed by `durin/memory/dream_patch_parser.py` and applied by `durin/memory/dream_apply.py` with `.md.bak` rollback. The earlier draft of this section described v1 (full-page rewrites) as the current state; corrected in audit B7 (2026-05-28).

### 5.1 Inputs to the prompt

| Input | Source | Purpose |
|---|---|---|
| `entity_id` (e.g., `person:marcelo`) | Trigger | The entity to consolidate |
| `existing_page` | Read from disk | Current entity page (frontmatter + body) |
| `existing_schema` | Derived from `existing_page` via `EntityPage.from_text` (audit F7, 2026-05-28) | List of attribute keys and relation types already used on this entity, rendered as `attributes: k1, k2` + `relation types: t1, t2` (sorted; `(none)` when empty). |
| `existing_uris` | Workspace state via `durin.memory.entity_inventory.existing_uris_by_recent_mtime` (audit F17, 2026-05-28) | URIs of entities already in `memory/entities/**`, sorted by file mtime descending, capped at 100. Used to discourage duplicate entity creation (e.g. `person:marcelo_marmol` when `person:marcelo` already exists). |
| `pending_entries` | Walk post-cursor `memory/episodic/`, `memory/stable/` filtered by entity tag | The N new observations to consolidate |
| `recent_history` | `git log --since='30 days ago' -- <entity_path>` via `format_recent_history` (audit F7, 2026-05-28) | Last few git commits of THIS entity page, so LLM sees recent changes Dream made. |

### 5.2 Prompt structure

```
You are durin's Dream consolidator. Process N new observations about
entity_id and update its canonical page.

ENTITY: {entity_id}

EXISTING PAGE (current canonical state):
{existing_page_content}

EXISTING SCHEMA for this entity (for coherence; not a constraint):
  attributes: {list_of_attribute_keys}
  relation types: {list_of_relation_types}

  Guidance:
  - PREFER reusing an existing key when the new info has the same semantic meaning.
  - If you notice two existing keys mean the same thing (e.g. 'email' and 'e-mail'),
    unify them in your output: emit ops that consolidate to one canonical key.
  - You MAY introduce new keys if the new information genuinely needs them.
  - The goal is coherent evolution, not rigid preservation.

EXISTING ENTITY URIs in workspace (consider for dedup; create new only if no match):
  {list_of_uris_truncated_to_100}

RECENT GIT HISTORY for this entity (so you can avoid undoing recent updates):
  {recent_commits_with_short_diffs}

PENDING OBSERVATIONS ({n_entries}):
{entries_text}

Return your output in this exact format:

===PATCH===
[
  {"op": "add", "path": "/attributes/email", "value": "marcelo@mxhero.com",
   "provenance": "episodic/2026-05-23T10-12.md"},
  {"op": "replace", "path": "/attributes/phone", "value": "+34123",
   "provenance": "episodic/2026-05-25T09-14.md"},
  {"op": "add", "path": "/relations/-", "value": {
     "to": "person:susana", "type": "spouse", "since": 2010
  }, "provenance": "episodic/2026-01-15T19-00.md"}
]

===BODY_DELTA===
<markdown to append to the body, OR empty if no body change>

===COMMIT===
<subject line, max 70 chars>

<optional commit body with reasoning, Sources lines, Cursor-after line>

===END===

RULES:
1. Prefer reusing existing attribute keys when meaning matches (see EXISTING SCHEMA
   above). You MAY add new keys if genuinely needed; you MAY unify keys you observe
   are semantically duplicate.
2. Same guidance for relation types.
3. For each patch op, include 'provenance' pointing to the source observation.
4. Do NOT remove existing attributes or relations unless an observation EXPLICITLY
   contradicts them. When in doubt, append history via valid_from/valid_until
   instead of overwriting.
5. If an observation tells you about a different entity than entity_id, IGNORE it.
   Each Dream pass is single-entity.
6. The COMMIT message is for the git log: short subject + optional body, then
   trailers in the format specified in the Dream skill (Sources, Cursor-after, etc.).
```

The exact prompt template lives in `durin/templates/dream/consolidator.md` and is the source of truth. This document specifies the contract; the template is the implementation.

### 5.3 Why this prompt shape

| Element | Reason |
|---|---|
| `existing_schema` | Prevents drift — LLM reuses known keys instead of inventing synonyms |
| `existing_uris` | Prevents duplicate entity creation (e.g., `person:marcelo` and `person:marcelo_marmol` for the same person) |
| `recent_history` | LLM sees its own recent decisions; avoids accidentally undoing what was just consolidated |
| JSON Patch (not full page) | Surgical edits; LLM doesn't need to copy unchanged content; less risk of accidental deletion |
| `provenance` field per op | Auditability — every attribute/relation can be traced to its source observation |
| Rule 4 (don't remove unless contradicted) | Defends against LLM hallucinating "forget X" behavior; preserves data by default |
| Rule 5 (single-entity per pass) | Keeps each pass focused; multi-entity passes would need much longer prompts and more error modes |

---

## 6. Apply pipeline

After the LLM returns a response, the apply pipeline:

```
1. Parse response: split ===PATCH===, ===BODY_DELTA===, ===COMMIT=== sections
   - Use `json_repair` to parse the PATCH (LLM small-model JSON quirks)
   - Strip code fences from each section
2. Validate the patch against the schema:
   - Each op has a valid 'op', 'path', 'value', 'provenance'
   - Paths only touch /attributes/*, /relations/*, /body (the latter is BODY_DELTA's job)
   - provenance source_refs must point to entries that exist
3. Read the current .md file
4. Copy the target to `.md.bak` BEFORE any mutation (audit F16, 2026-05-28:
   the pre-F16 doc listed this AFTER the write step, which contradicts
   `durin/memory/dream_apply.py` lines 165-168 where the backup happens
   first so any failure has something to restore from)
5. Apply the JSON Patch operations to the frontmatter (using `jsonpatch` lib)
6. Append BODY_DELTA to the body if non-empty
7. Update internal fields: dream_processed_through = batch_last_ts, updated_at = now
8. Re-render the entire .md (frontmatter + body), validate round-trip
9. Write to a temp file, fsync, atomically rename over the target
10. Post-write: re-parse the written file; if it doesn't pass schema validation,
    restore from `.md.bak` and report failure (`ROUND_TRIP` kind)
11. On success: delete `.md.bak`; commit to `memory/.git/` with the COMMIT
    message + structured trailers:
    Sources: <list of consumed entries>
    Entities-touched: <entity_id>
    Cursor-after: <batch_last_ts>
    Trigger: <trigger_name>
12. Archive consumed episodic entries (§7)
13. Trigger indexer re-derivation for this entity page (§02_indexing.md §6.1)
```

If any step 2-10 fails, the apply is rolled back (file restored from `.md.bak`, cursor not advanced) and the entity is marked as failed in telemetry. The next pass will try again.

### 6.1 Cursor advance invariant (G2)

Critical correctness invariant for batch consolidation:

**The cursor is set to `batch_last_ts` — the actual timestamp of the latest entry the runner passed to the LLM in this batch — NOT to whatever the LLM emitted in its commit's `Cursor-after:` trailer.**

Reasoning: small models (and sometimes large ones under prompt pressure) occasionally process only a subset of a multi-entry batch — for example, the runner passes 10 entries but the LLM's response only references 5 of them, emitting `Cursor-after:` for the 5th entry's timestamp. If the runner trusted the LLM's `Cursor-after:` and advanced the cursor only to entry 5, the entries 6-10 would become **forever post-cursor in a way that no future pass would see them** (because they're below the new cursor floor but the LLM never actually consumed them).

By forcing `dream_processed_through = batch_last_ts` (the timestamp of entry 10, regardless of which entries the LLM emitted ops for), the next pass picks up either the entries the LLM skipped (if they're still post-cursor against batch_last_ts somehow — they shouldn't be by construction) OR the next batch of new entries arriving after batch_last_ts.

The LLM's `Cursor-after:` trailer is still recorded in the git commit message for audit — it tells us what the LLM *thought* it processed. The runner's enforced cursor tells us what was *actually* taken out of the post-cursor queue. Divergence between the two is a useful diagnostic signal (logged at `INFO` level when detected).

Trade-off: if the LLM truly processed only 5 of 10 entries, those 5 are correctly captured (their PATCH ops are applied) but the other 5 are silently dropped from re-processing. Mitigation: this is rare with modern models on reasonable batch sizes (default `max_entries_per_batch = 10`); when it happens, the missing ops manifest as gaps in the entity page that the next batch of entries (or a manual `durin dream run --entity <uri>`) will fill in. Data is not lost from disk — only from the consolidation pass.

This invariant is enforced in `dream.py::ConsolidationResult.batch_last_ts` and applied in `DreamConsolidator.apply()` before writing the entity page.

---

## 7. Archive workflow

Per `01_data_and_entities.md` §3.6 and §5.3, episodic entries that Dream has consumed are **moved to `memory/archive/episodic/`** rather than deleted. Mechanics:

```
For each consumed episodic entry (referenced in this pass's PATCH provenance):
  - Add frontmatter fields: archived_at, archived_into
  - Move the file: memory/episodic/<id>.md → memory/archive/episodic/<id>.md
  - The indexer removes the LanceDB row + FTS5 rows for this uri
  - Archive folder is NEVER scanned by the default search pipeline (§3.6 of doc 01)
```

Stable entries are **never** auto-archived (§5.4 of doc 01). Even if Dream consumes a stable entry's content, the entry stays in `memory/stable/` because the user/agent explicitly marked it as durable.

Corpus entries are not archived by Dream — they are deleted only on re-ingest of the source (§5.5 of doc 01).

---

## 8. Entity dedup — absorb-judge (opt-in, OFF by default)

The absorb-judge is a separate LLM-driven step that detects when two entity pages should be merged. It is **disabled by default** and enabled per-workspace via config. Defaults are conservative — false-positive merges destroy data structure silently, and the recovery path (revert a git commit) is fine but the noise is high.

### 8.1 Why opt-in

The blast radius of a silent bad merge is high enough that explicit opt-in is the right ergonomics. From `durin/config/schema.py::AutoAbsorbConfig`:

> "Master switch — keep OFF by default; the blast radius of a silent bad merge is high enough that opt-in is the right ergonomics."

When operators have telemetry data (from real Dream runs) to inform tuning, they can lower the confidence threshold or shorten the quarantine. Until then, defaults err on the side of "do nothing rather than do the wrong thing".

### 8.2 Configuration

Settings live under `memory.dream.auto_absorb.*` in the workspace config.

| Setting | Default | Effect |
|---|---|---|
| `enabled` | `false` | Master switch. When false, the absorb-judge step never runs. |
| `confidence_threshold` | `95` (0-100) | Minimum judge confidence for auto-merge. High threshold favors precision over recall — pairs warranting merge typically also warrant manual review at 95. Tune down with telemetry data from `memory.absorb.judged`. |
| `min_age_hours` | `24` | Both pages in a candidate pair must be at least this old (by file mtime). Blocks the "premature consolidation" loop where Dream creates two near-identical pages in the same pass and immediately merges its own hallucinations. |
| `judge_model` | `null` (uses dream model) | Override model for the judge. When `null`, falls back to the Dream consolidator's model. Setting a different model reduces self-consistency bias (§8.5). |

### 8.3 Trigger (when enabled)

After a successful Dream apply, the alias index is checked for **alias overlap** between the just-touched entity and any other entity in the workspace:

- If `entity_a.aliases ∩ entity_b.aliases ≠ ∅` and the entities have the same `type`, AND
- Both pages are older than `min_age_hours` (quarantine), AND
- The pair hasn't been judged in the last 24 hours (re-judge throttle — avoids burning LLM budget on the same pair every Dream run)

→ The pair is queued for the absorb-judge LLM call.

### 8.4 Judge prompt

The judge (defined in `durin/memory/absorb_judge.py` and `durin/templates/dream/absorb_judge.md`) receives:

| Input | Purpose |
|---|---|
| Canonical entity page (`entity_a`) | The candidate to keep |
| Absorbed entity page (`entity_b`) | The candidate to merge in |
| Cross-type filter | Both must have the same type to be merge candidates |
| Source refs of both | So judge can see when each was created |

It returns one of (verdict vocabulary is **identity-judgement**, not action-prescription — the runner maps verdict + confidence to the merge action):

| Verdict | Meaning | Action when `confidence ≥ confidence_threshold` |
|---|---|---|
| `same` | A and B describe the same real entity | Combine `entity_b` into `entity_a`, archive `entity_b` |
| `different` | A and B are distinct entities with overlapping aliases (e.g., two people named "Marcelo") | Do nothing; log `memory.absorb.judged` |
| `unclear` | The judge couldn't decide; defer for now | Re-judge in 24h; do nothing |

### 8.5 Judge decision and merge action

The judge returns a structured decision plus a confidence score (0-100). The merge proceeds only if `verdict == "same"` AND `confidence ≥ confidence_threshold`. Decisions below threshold are logged as `memory.absorb.judged` events without merging — the operator can inspect these later to decide whether to lower the threshold.

### 8.6 Merge action (when confidence passes threshold)

When `verdict == "same"` and confidence passes the threshold:

```
1. Load both pages
2. Apply a structured merge:
   - aliases: union
   - attributes: union; on conflict, keep the value from entity_a (canonical wins),
     log the conflict in the commit body
   - relations: union (dedup by (to, type) pair)
   - body: append entity_b.body to entity_a.body with a separator section
   - provenance: union
3. Write the merged entity_a back
4. Move entity_b to memory/archive/entities/<type>/<slug>.md
   with archived_into: <entity_a_uri> in frontmatter
5. Update the alias index (entity_a inherits all of entity_b's aliases)
6. Re-derive LanceDB + FTS5 for entity_a; delete rows for entity_b
7. Commit with message: "merge entity_b into entity_a (judge: <reasoning>)"
```

**Recovery:** `cd memory && git revert <merge_commit_sha>`. The merge is a single commit; reverting restores both pages to their pre-merge state. Telemetry records the revert as `memory.absorb.reverted`.

### 8.7 Self-consistency bias mitigation

When `judge_model == dream_model` (e.g., both glm-5.1), the judge tends to confirm Dream's recent decisions. The judge prompt includes a directive to peer-review critically and to flag uncertainty as `unclear` rather than confirm. (Corrected from `unsure` in audit C5 — §8.4 and `absorb_judge.py:73` both use the canonical `unclear`.)

Per `feedback_question_user_input.md`-style reasoning: an LLM is more reliable when it has a different role (review vs. produce) than when it is asked to confirm its own output. Using a different model for the judge (when budget allows) further reduces this bias.

---

## 9. Schema upgrade — v1 → v2 lazy

Per `01_data_and_entities.md` §8, entity pages without `attributes`/`relations`/`provenance` (v1) continue to parse with those fields treated as empty.

When Dream first touches a v1 entity page in v2 code:

1. The consolidator prompt is invoked normally — `existing_schema` is empty (no attributes yet).
2. The LLM emits PATCH ops creating the first attributes/relations.
3. Apply pipeline adds the v2 fields to the frontmatter as a natural side effect.
4. `provenance` map is initialized empty and populated as ops are applied.

The migration is non-destructive and on-demand. A workspace with thousands of v1 entity pages doesn't need a bulk migration — each page upgrades when next consolidated. Until then, search still works (the indexer reads v1 pages just fine).

---

## 10. Schema drift control

The mechanism that prevents `email` → `e-mail` → `correo` drift across consolidations is the `existing_schema` block in the prompt (§5.2). The LLM sees what keys this entity already uses and is instructed to reuse them.

Per-entity scope (not workspace-wide):

- Drift between entities is acceptable. `person:marcelo.attributes.email` and `person:susana.attributes.correo_electronico` is tolerated — different entities can have different schemas if the LLM consistently produced them so.
- Drift WITHIN an entity is what the prompt prevents.

Workspace-wide consolidation (e.g., normalizing `email` vs `correo_electronico` across all person pages) is a deferred concern (see `08_scope_and_discarded.md` cross-entity consistency checks).

---

## 11. Git commits

Every Dream apply creates exactly one commit in `memory/.git/`:

```
<commit subject from ===COMMIT===>

<optional commit body>

Sources: episodic/2026-05-23T10-12.md, episodic/2026-05-25T09-14.md
Entities-touched: person:marcelo
Cursor-after: 2026-05-25T09:14:00Z
Trigger: threshold
Run-id: <uuid>
```

Properties:

- **One commit per apply.** Bulk passes that consolidate 10 entities produce 10 commits.
- **Author**: `durin-dream <dream@durin.local>`.
- **Trailers are grep-able.** Format rules live in the Dream skill (see `06_prompts_and_instructions.md`). The LLM is instructed to include `Sources:`, `Cursor-after:`, `Entities-touched:`. The code post-processes the commit message and:
  - **Auto-completes `Trigger:` and `Run-id:`** (these are always known by the runner, not the LLM).
  - **Verifies presence** of `Sources:`, `Cursor-after:`, `Entities-touched:` — if missing, fills them in from runner state and logs a warning.
  - Does NOT block the commit if a trailer is missing; auto-fills with safety net.
- **Commit subject** comes from the LLM (≤ 70 chars).
- **No `--amend`.** Each consolidation is a discrete event in history.

This hybrid (skill instructs + code verifies) gives flexibility to the LLM while guaranteeing the trailers that downstream queries (e.g., git log greppable for `memory_history`) depend on.

User manual edits to `.md` files are committed separately by the indexer (§6.4 of doc 02) with `author: user`.

---

## 12. Failure modes

Audit F6 (2026-05-28) aligned this section to the shipped enum.
The `DreamApplyFailureKind` enum in `durin/memory/dream_apply.py`
emits four kinds: `validation | patch_runtime | round_trip | io`.
LLM call failures (§12.1) happen UPSTREAM of `dream_apply` and are
tracked by the consolidator path, not by this enum. The pre-F6
spec used `_failed` suffixes that no caller ever emitted.

### 12.1 LLM call failure (upstream)

If `default_llm_invoke` raises (network error, API timeout, rate limit):
- Caught in the consolidator BEFORE `dream_apply` runs.
- Telemetry emitted with the runner-level counter (not `DreamApplyFailureKind` — the apply step never executed).
- Skip this entity for this pass; DO NOT advance cursor.
- Continue to next entity.
- Pass exits with one failure; next trigger will retry.

### 12.2 Patch runtime failure (`kind=patch_runtime`)

If `===PATCH===` JSON is malformed (after `json_repair`) OR if applying a parsed op raises:
- Log: `kind=patch_runtime`.
- Skip this entity; cursor stays.

The pre-F6 spec split this as "parse failure"; the shipped enum
folds both JSON-decode errors and patch-op runtime errors into the
same category because the operator response (skip + cursor stays)
is identical.

### 12.3 Validation failure (`kind=validation`)

If the patch tries to overwrite `dream_processed_through` or touches paths outside allowed roots:
- Reject the patch.
- Log: `kind=validation` with details.
- Skip; cursor stays.

### 12.4 Round-trip failure (`kind=round_trip`)

If after applying the patch, the resulting frontmatter doesn't re-parse:
- Restore from `.md.bak`.
- Log: `kind=round_trip`.
- Skip; cursor stays.

### 12.4a IO failure (`kind=io`)

If the disk write itself fails (disk full, permission denied, etc.):
- Log: `kind=io`.
- Skip; cursor stays.

`io` is treated as **ambient** alongside upstream LLM failures —
the entity has no structural defect; the environment is at fault.

### 12.5 Repeated structural failures on the same entity

If the same entity fails **3 times in a row** with a **structural** failure type — `STRUCTURAL_FAILURE_KINDS = {validation, patch_runtime, round_trip}` per `dream_quarantine.py:44` — across consecutive passes:

- Add `dream_failure_count: 3` and `dream_quarantine: <ISO_timestamp_now+7d>` to the entity's frontmatter.
- Subsequent passes skip this entity until `dream_quarantine` expires (default 7 days).
- Telemetry emits `kind=quarantined`.
- Operator manually inspects, fixes, and removes the fields (or waits for quarantine to expire).

**Crucial: only structural failures count.** Ambient failures (upstream LLM call failures from rate limits / network timeouts, and `kind=io` from disk-write errors) do NOT increment the counter. Reasoning: ambient failures don't indicate a problem with the entity itself; they indicate transient infrastructure issues. Counting them would quarantine perfectly healthy entities during a z.ai outage or a disk-full event.

After a successful apply, the counter resets to 0.

This prevents one persistently-broken entity (corrupt frontmatter, edge-case prompt-killer, etc.) from leaking LLM call budget across passes. Cost-of-implementation: ~30 LOC + 2 frontmatter fields.

### 12.6 Lock takeover after crash

If a previous Dream pass crashed before releasing the lock:
- Next pass detects stale lock (mtime > 10 min old).
- Takes over the lock with its own PID.
- Continues normally. The crashed pass's partial work is fine — it didn't advance any cursor it shouldn't have (cursor advance is the LAST step of apply).

---

## 13. Throughput and cost considerations

Dream cost is dominated by LLM calls (one per entity per pass + occasional absorb-judge calls):

| Metric | Typical value |
|---|---|
| LLM calls per pass | 1 per pending entity (5-50 typical) |
| Tokens per call | ~2-5k input, ~500-1500 output |
| Calls per day (active workspace) | 50-200 |
| Cost per call (glm-5.1) | ~$0.005 |
| Total daily cost | $0.25 - $1.00 |

This is the entire reason for the threshold-trigger design: avoid running Dream on every `memory_store` (which would be costly + slow). Threshold + cron together produce ~10-50 passes/day, each consuming a batch.

If Dream cost becomes a concern, two levers exist:
- Raise threshold (consolidate larger batches).
- Use a smaller/cheaper model for routine consolidations and reserve glm-5.1 for complex ones.

These are config knobs, not architectural changes.

---

## 14. Module-level decisions

All consistent with prior decisions in docs 00-04.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Trigger types | Six: threshold (per-entity, post-write), post_ingest_threshold (post-ingest pathway, mirrors threshold), cron_daily (safety net), session_close, post_compaction, manual. Threshold is the primary trigger; cron is the safety net. Corrected from "Five" in audit C4 (2026-05-28) — §2 of this doc enumerates all six and code wires all six (commit `c3eff1e`). | §2 |
| 2 | Lock granularity | Single workspace-level file lock at `memory/.dream.lock`. Already exists; not introducing a new lock. | §4.1 |
| 3 | Sequential per-entity within a pass | Yes. Parallel apply across entities can race on aliases / alias index. Cold path; serialization is acceptable. | §3 |
| 4 | Throttle behavior | **300s (5 min) minimum interval between runs** (configurable via `min_seconds_between_runs`). Already in code. Manual trigger bypasses throttle. | §4.2 |
| 5 | Cursor advancement | Only on full successful apply. Failures leave cursor where it was, so entries get retried next pass. | §4.3, §12 |
| 6 | Apply mechanism — JSON Patch + Dream skill package | JSON Patch (RFC 6902) over the frontmatter, plus BODY_DELTA for body append. Surgical edits, not full page rewrites. Reduces hallucination-induced data loss. **All prompt material lives as a Dream skill package** (`durin/templates/dream/`) including the main prompt, JSON Patch reference, few-shot examples, and rules — concatenated at call time. Overhead ~1-2k tokens per call, accepted for precision. | §5.2, §6, doc 06 |
| 7 | Prompt context (existing_schema + existing_uris + recent_history) | Yes, three context blocks: (a) **existing_schema** — attribute keys + relation types on this entity, **for coherence, not as constraint** (LLM may add new keys or unify duplicates); (b) **existing_uris** — top-100 entity URIs by recent mtime, to discourage duplicate entity creation; (c) **recent_history** — `git log` of the entity page over last 30 days, so the LLM sees its own previous decisions and avoids undoing them. Total ~1k tokens/call extra. | §5.2, §5.3 |
| 8 | Absorb-judge trigger | Alias overlap + same type + 24h quarantine. LLM-judged verdict: `same` / `different` / `unclear` (audit E22 corrected — pre-E22 this row said `merge / keep_separate / unsure`, which never matched the `absorb_judge.py` enum or the prompt template). The dispatcher auto-merges only on `verdict == "same"` AND `confidence ≥ threshold`. | §8 |
| 9 | v1 → v2 schema migration | Lazy on Dream touch. No bulk migration. Search works on v1 pages too. | §9 |
| 10 | Drift control scope | Per-entity (workspace-wide normalization is deferred). | §10 |
| 11 | Git commit pattern — skill instructs + code verifies | One commit per apply. Author: `durin-dream`. **Skill instructs** the LLM to format the commit message with trailers `Sources:`, `Cursor-after:`, `Entities-touched:`. **Code post-processes** the message: auto-completes `Trigger:` and `Run-id:` (always known by runner); verifies the LLM-supplied trailers are present and auto-fills from runner state if missing (warning, not block). | §11 |
| 12 | Quarantine after repeated STRUCTURAL failures | After 3 consecutive **structural** failures (`validation`, `patch_runtime`, `round_trip`) on the same entity, set `dream_failure_count` + `dream_quarantine: <now+7d>` in its frontmatter; skip until quarantine expires. **Ambient failures (upstream LLM call errors + `kind=io` disk errors) do NOT count** — those indicate infrastructure issues, not entity-level problems. Counter resets after a successful apply. Audit F6 (2026-05-28) aligned the enum names to the shipped code. | §12.5 |
| 13 | Cost expectation | $0.25-$1.00/day for active workspace at typical pass rates. Adjustable via threshold + model. | §13 |

### Open

None at the module level.

---

## 15. Implementation status (current vs target)

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| Trigger types | threshold + cron_daily + post_compaction + session_close + manual labels exist in code | Same (labels already defined); just ensure threshold is dispatched from `memory_store`/`memory_ingest` and not only as runner label | Minor wiring; document label semantics |
| Lock + throttle + cursor | Active (works well) | Same | None |
| Consolidator prompt | v2 — PATCH + BODY_DELTA + COMMIT, with existing_schema + uris + recent_history. **Shipped in Phase 1.9 (commit `6aafc3f`).** Multi-file Dream skill package at `durin/templates/dream/` (main prompt + json_patch_reference + examples/ + rules), assembled by `dream_prompt_builder.build_dream_prompt`. | — | — |
| Apply mechanism | v2 — JSON Patch over frontmatter + body delta with `.md.bak` rollback. **Shipped in Phase 1.9.** See `durin/memory/dream_apply.py`. | — | — |
| Provenance tracking | **Shipped (Phase 1.9).** Every PATCH op carries a `provenance` pointer; collected during apply and persisted in the entity page's `provenance` field. See `dream_patch_parser.py` + `dream_apply.py`. Audit E21 (2026-05-28) flipped this row from "Not explicit". | — | — |
| Archive of consumed episodic | **Shipped (Phase 1.9).** `dream_archive_consumed.archive_consumed_episodic` moves provenance-cited episodic entries to `memory/archive/episodic/` after a successful apply. Wired by the Dream consolidator post-apply. Audit E21 flipped from "Not implemented". | — | — |
| Absorb-judge | Active for alias overlap | Same; add merge action that uses structured merge + archive | Extend `judge_pair` decision handling |
| v1 → v2 migration | n/a (v2 doesn't exist yet) | Lazy on Dream touch | Entity page parser already preserves unknown frontmatter; just add v2 fields |
| Git commits | **Shipped (Phase 1.9).** Hybrid model active: skill instructs the LLM to include `Sources:`, `Cursor-after:`, `Entities-touched:` trailers; code auto-completes `Trigger:` + `Run-id:` always and fills LLM-missing trailers. See `dream_commit_message.py` + `dream_git_history.py`. Audit E21 flipped from "Active (LLM-emitted subject + body)" — the row still claimed migration work that already landed. | — | — |
| Failure quarantine | **Shipped (Phase 1.9).** `dream_quarantine` + `dream_failure_count` fields on the entity page; `record_failure` increments and stamps a 7-day quarantine on the 3rd structural strike (parse / validation / round-trip — ambient failures excluded). The runner skips quarantined entities. See `dream_quarantine.py`. Audit E21 flipped from "Not implemented". | — | — |

---

## 16. Cross-references

- Entity schema (frontmatter v2 with attributes/relations/provenance): `01_data_and_entities.md` §3.5.
- Archive folder behavior (exclusion from all search paths): `01_data_and_entities.md` §3.6.
- Indexer re-derivation triggered after Dream apply: `02_indexing.md` §6.1.
- Alias index updated on entity merge: `02_indexing.md` §6 (chokepoint walker).
- Hot-path read of consolidated entity pages: `03_search_pipeline.md` §4.3.
- `memory_store` and `memory_ingest` create the entries Dream consumes: `04_agent_tools.md` §3, §4.
- Onboarding wizard exposes Dream cost expectations: `06_prompts_and_instructions.md` (pending).
- Telemetry events emitted by Dream: `07_telemetry_and_observability.md` (pending).
