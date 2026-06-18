---
title: Telemetry and observability
version: 0.1-draft
status: current — describes the shipped system (post-migration, 2026-06-06)
last_updated: 2026-06-06
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 03_search_pipeline.md, 05_dream_cold_path.md
related: 04_agent_tools.md
---

# Telemetry and observability

This document specifies the telemetry events emitted by the memory subsystem, the metrics derivable from them, and the operational dashboards / alerts that should be wired up. Telemetry is the only way humans observe whether the system is behaving correctly — without it, regressions silently degrade retrieval quality and Dream cost accumulates unnoticed.

**Principle:** every decision point that could fail, degrade, or surprise the operator emits an event. Events are JSON, structured, and grep-friendly. Aggregation is downstream.

---

> **Implementation status:** events marked **NEW** in this document are v2 targets that do not yet exist in `durin/telemetry/schema.py`. The existing events (per the `EVENTS` catalog in schema.py) are explicitly noted as "already exists". Adding the new events is tracked as a deliverable in `09_implementation_roadmap.md` Phase 7 (Telemetry v2).

## 1. Scope

### In scope

- Event schema per memory subsystem operation.
- Payload fields that matter for diagnostics and bench correlation.
- Metrics derivable from events.
- Suggested dashboards.
- Sampling and retention policy.

### Out of scope

- The telemetry transport itself (file vs. SQLite vs. external) — that's `durin/telemetry/` infrastructure.
- Tracing across distributed processes — durin is single-process; no need.
- Cost accounting for LLM calls — that's its own subsystem; the cost ledger is upstream.

---

## 2. Telemetry stack overview

durin already has a telemetry infrastructure (`durin/telemetry/schema.py` registers event types). The memory subsystem emits via `emit_tool_event` and similar helpers.

Each event has:

```json
{
  "event": "memory.<category>.<action>",
  "timestamp": "ISO_8601",
  "session_id": "<uuid>",
  "run_id": "<uuid>",
  "payload": { /* event-specific */ }
}
```

The `event` name is the catalog key. Payload schemas are declared in the `EVENTS` catalog in `schema.py` — adding a new event type requires adding a TypedDict and registering it there.

---

## 3. Event categories

| Category | Path | When emitted | Purpose |
|---|---|---|---|
| Recall | `memory.recall*` | Every `memory_search` call (and sub-paths) | Hot-path retrieval observability |
| Store | `memory.store*` | Every `memory_store` call | Write observability + dedup tracking |
| Ingest | `memory.ingest*` | Every `memory_ingest` call | Ingest observability |
| Dream | `memory.dream.*` | Every dream pass (start / end per pass, per-entity `patch_applied`, `skill_extract`, `max_seconds_reached`, reactive-trigger `throttled`, `always_on` distillation) | Cold-path consolidation observability |
| Relation cap | `memory.entity_relation_cap_*` | Entity write crosses the soft (50) or hard (200) relation cap — **alert-only**, the write still proceeds (A3) | Mega-hub formation early-warning |
| Absorb | `memory.absorb.*` | Every absorb-judge decision (auto-merge, skipped, reverted) | Dedup observability |
| Search-fail | `memory.search.failure` | Whenever a search-path component fails (recoverable or not) | Recovery + degradation tracking |
| Index | `memory.index.*` | Index re-derivation events (per write, per rebuild) | Indexer health |
| Embedding | `memory.embedding.*` | Model load (`.load`) + per-embed timing (`.embed`) from `FastembedProvider` | Provider performance + eviction signals |
| Hot layer | `memory.hot_layer.failure` | When the hot-layer renderer fails to assemble a context block (read error, parse error) | Context-assembly resilience signal |
| Health | `memory.health_check`, `memory.health.critical` | Every health-check tick (A11) + 3-strike escalation (A7) | Subsystem availability monitoring |
| Turn rollup | `turn.memory_usage` | Once per turn at save time (`AgentLoop._state_save`), including turns with zero tool calls | Turn-level denominators for silent-miss and prefetch-substitution analysis (`search_calls == 0` rows are the signal) |

Audit B10 (2026-05-28) added the `Embedding`, `Hot layer`, and `Health` rows — these events are emitted by the code but the original §3 table omitted them. The TypedDicts live in `durin/telemetry/schema.py`.

> **Source of truth.** The exhaustive, authoritative list of event types is `durin/telemetry/schema.py::EVENTS` — `tests/telemetry/test_schema_catalog.py` enforces, in both directions, that every event emitted in the source tree has a catalog entry and vice versa. **This document annotates the events whose fields or usage need explanation; it does not mirror the catalog event-for-event.** A new event shipping without a subsection here is expected, not drift — consult `schema.py` for the complete set. (For example `memory.fallback_tool_used` and `memory.skill_miss` exist in the catalog without a dedicated subsection here.)

---

## 4. Recall events (hot path)

Emitted by `memory_search` and its sub-pipeline.

### 4.1 `memory.recall`

Top-level event, emitted once per `memory_search` call. Audit E1
(2026-05-28) aligned the payload with this table; pre-E1 only the
first four required fields were emitted.

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Raw query string |
| `scope` | enum | yes | `dreamed | undreamed | all` |
| `level` | enum | yes | `warm | cold` |
| `result_count` | int | yes | Final count returned (after limit) |
| `strategy` | enum | yes | `vector | lexical | hybrid | grep` (which path produced results) |
| `duration_ms` | float | yes | Total search wall-clock |
| `total_candidates` | int | yes | `vector_count + lexical_count` from the pipeline (pre-limit) |
| `keywords` | string \| null | yes | The LLM-supplied keyword hint (`null` when omitted) |
| `in_context_deduped` | int | optional | Hits collapsed to pointer lines because their rendered content was already in the caller's hot layer (P4, 2026-06-10; doc 03 §12.5). 0 when nothing deduped or dedup is off (subagent / direct constructions). |
| `recovered_from` | list of strings | only on degraded run | Pipeline sources that raised and recovered (e.g. `["vector"]`) |
| `recovery_duration_ms` | float | only on degraded run | Wall-clock spent inside the safe wrappers that swallowed failures |
| `iteration` | int | optional | Agent iteration counter (auto-injected by `emit_tool_event`, audit F20 2026-05-28). Caller-supplied values win — subagents stamping the parent session pass it explicitly. |
| `session_key` | string \| null | optional | Session key (auto-injected by `emit_tool_event` from the bound `TelemetryLogger`, audit F20 2026-05-28). |

`recovered_from` + `recovery_duration_ms` mirror the tool's response
shape (`MemorySearchTool.execute()`) — both are omitted on clean
runs to keep dashboards' degradation panels grep-friendly.

### 4.2 `memory.recall.vector`

Vector retrieval sub-path. Already exists in `schema.py:442` (`MemoryRecallVectorEvent`).

Fields covered there are sufficient; additions for v2:

| Added field | Type | Description |
|---|---|---|
| `cross_encoder_active` | bool | Was the cross-encoder step run? |
| `cross_encoder_duration_ms` | float \| null | Time spent in cross-encoder if active |

### 4.3 `memory.recall.lexical`

FTS5 path observability. Emitted by `durin/memory/lexical_search.py`.
Audit E2 (2026-05-28) aligned doc with actual emission — pre-E2 the
table referenced a non-existent `tokenizer_used` field.

| Field | Type | Description |
|---|---|---|
| `route` | enum | `unicode61 | trigram | like_substring` (from `LexicalRoute` enum in `query_router.py`) |
| `query_chars` | int | Length of normalised query (post Unicode NFC + casefold) |
| `cjk_chars` | int | Count of CJK chars in the query — drives the route decision |
| `hit_count` | int | FTS5 returned hits |
| `duration_ms` | float | FTS5 query duration |

The raw `query` is intentionally NOT included here — `memory.recall`
already carries it, and duplicating it doubles per-row storage on a
hot path. Dashboards join `memory.recall.lexical` to `memory.recall`
on `(session_key, iteration)` if they need the original text.

### 4.4 `memory.recall.rerank`

NEW event for cross-encoder rerank step.

| Field | Type | Description |
|---|---|---|
| `model` | string | Cross-encoder model used |
| `input_count` | int | Candidates passed in (typically 50) |
| `output_count` | int | Candidates after rerank (typically 10) |
| `score_delta_top1` | float | Cross-encoder score - bi-encoder rank position |
| `duration_ms` | float | Rerank step duration |

This event only emits when cross-encoder is enabled (default OFF).

### 4.5 `memory.recall.rrf`

Cross-source RRF fusion step. Emitted by `durin/memory/rrf_fusion.py`.
Audit E3 (2026-05-28) aligned doc with actual emission — pre-E3 the
table referenced abstract roll-ups (`sources_active`, `dedup_count`)
that the code never emitted; the actual schema gives per-source hit
counts, which are strictly richer.

| Field | Type | Description |
|---|---|---|
| `vector_count` | int | Hits contributed by the vector path (0 ⇒ vector inactive) |
| `lexical_count` | int | Hits contributed by the FTS5 lexical path |
| `grep_count` | int | Hits contributed by the grep fallback path |
| `fused_count` | int | Unique URIs after RRF fusion (post-dedup) |
| `boosted` | bool | True when caller passed `keywords` and `w_lexical` was bumped to 2.5 |
| `duration_ms` | float | RRF computation duration |

Dashboards derive "sources active" as the set of `*_count > 0`; the
"co-occurrence dedup" is implicit in `vector_count + lexical_count +
grep_count − fused_count` (URIs counted in N sources but unified
into one row).

### 4.6 `memory.silent_retrieval_miss` (discarded — see doc 08 §2.11)

**Not emitted.** Discarded in audit B9 (2026-05-28). The v1 spec proposed three heuristics to detect "agent should have called `memory_search` but didn't":

1. Substring overlap > 60% with turn N's user message.
2. Starts with negation tokens ("no,", "wrong,", "actually,").
3. Contains correction patterns ("I said X, not Y", "you forgot…").

Honest review: only (1) is language-agnostic — and it generates too many false positives (legitimate refinements look like re-asks). (2) and (3) are inherently English-shaped; the token lists and patterns would need per-language maintenance, and even then they wouldn't catch idiomatic corrections in CJK / Spanish. Without an LLM classifier (which breaks the telemetry budget), the event is unreliable for the multi-lingual workloads durin actually targets.

The downstream consumer (§2.F eager pre-fetch) is itself deferred (doc 08 §4.1) — so even a reliable signal would have no consumer. If a future use case needs this kind of "miss" detection, the right approach is different (e.g. LLM-based classifier in background, explicit user-feedback signals, or post-hoc analysis on bench traces) — not these heuristics.

See `08_scope_and_discarded.md` §2.11 for the full rationale.

### 4.7 `memory.recall.decay` (removed 2026-05-30)

Temporal decay was removed from the search pipeline (see doc 03 §10).
The event is no longer emitted and the `MemoryRecallDecayEvent`
TypedDict was deleted from `durin/telemetry/schema.py`. Pre-removal
dashboards consuming this key will stop receiving data — that's the
intended signal.

---

## 5. Store / Ingest events

### 5.1 `memory.store`

Already exists in schema.py:453. Sufficient as-is.

### 5.2 `memory.ingest`

Already exists in schema.py:464.

### 5.3 `memory.store.blocked_near_duplicate`

Already exists in schema.py:669. Emits when `memory_store` refused to write due to dedup pre-check.

---

## 6. Dream events (cold path)

The dream is now **four passes** (doc 05; doc 11 C2): **extract** (sessions → entity attributes), **refine** (dedup duplicate entities), **skill** (procedural-skill extraction), and **always_on** (pinned-guidance distillation). The daily cron runs all four; reactive triggers (`post_compaction` / `session_close`) run **extract only**, gated by the in-process throttle; manual `durin memory dream` runs the cron set. Emitters live in `durin/memory/dream_passes.py`, `extract_dream.py`, `always_on_dream.py`, `refine_dream.py`, and the reactive trigger in `durin/cli/commands.py`.

> **Migration note.** The pre-migration `DreamConsolidator` / `DreamRunner` cluster, the JSON-Patch apply pipeline (`dream_apply.py`), the per-entity `dream_processed_through` cursor, and the `memory.dream.{skipped,entity_failed,budget_exhausted,legacy.*}` events were **deleted** (doc 11 B2/C2, N3). The events below are the ones the new passes actually emit; each maps 1:1 to a TypedDict in `schema.py`.

### 6.1 `memory.dream.start`

`MemoryDreamStartEvent`. Emitted by both the extract and refine passes (`dream_passes.py`).

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | string | yes | `extract` or `refine` — which pass began |
| `session_key` | string \| null | optional | Session key (auto-injected by `emit_tool_event` when emitted in a session context; the cron path emits `None`) |

### 6.2 `memory.dream.end`

`MemoryDreamEndEvent`. Emitted by both passes; `kind` + `duration_ms` are always present, the remaining fields are pass-specific (extract sets the entity counts, refine sets the merge counts).

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | string | yes | `extract` or `refine` |
| `duration_ms` | int | yes | Wall-clock of the full pass |
| `entities_consolidated` | int | extract only | Entities whose attributes were written (`out["entities"]`) |
| `entities_failed` | int | extract only | Sessions that raised during extraction (length of the error list) |
| `sessions` | int | extract only | Session files that yielded at least one extracted entity |
| `yielded` | bool | extract only | `True` when the `max_seconds_per_run` cap was hit and the per-session cursor will resume the remainder next trigger |
| `merged` | int | refine only | Duplicate pairs auto-merged |
| `kept` | int | refine only | Pairs judged distinct (kept separate) |
| `candidates` | int | refine only | Alias-overlap candidate pairs considered |

There are **no cost-telemetry fields** (`llm_call_count` / `llm_*_tokens_total`) and no `trigger` / `entity_filter` / `entities_quarantined` on this event — those belonged to the deleted `DreamRunner` and were removed with it.

### 6.3 `memory.dream.patch_applied`

`MemoryDreamPatchAppliedEvent`. Emitted by `durin/memory/extract_dream.py` once **per entity** the extract pass writes. The event name and `ops_applied` field are reused from the legacy event so existing dashboards keep counting consolidations — but the semantics changed: `ops_applied` now counts **attributes written**, not JSON-Patch ops (there is no JSON-Patch pipeline anymore; writes go through `memory_writer.write_entity` via CAS).

| Field | Type | Description |
|---|---|---|
| `entity_ref` | string | Target entity (e.g. `person:marcelo`) |
| `ops_applied` | int | Count of attributes written to the entity page |
| `trigger` | string | Always `"extract"` today (the only pass that authors attributes) |
| `committed` | bool | Whether the CAS write produced a commit (`write_entity` result) |
| `source_ref` | string | The session-turn marker the attributes were extracted from (falls back to `"extract_dream"`) |

### 6.4 `memory.dream.skill_extract`

`MemoryDreamSkillExtractEvent`. Emitted by the skill pass (`dream_passes.py`) after it writes/updates procedural skills (doc 05 §8e).

| Field | Type | Required | Description |
|---|---|---|---|
| `skills_touched` | int | yes | Number of skill files written or updated this pass |
| `duration_ms` | int | optional | Wall-clock of the skill pass |

### 6.5 `memory.dream.max_seconds_reached`

`MemoryDreamMaxSecondsReachedEvent`. Emitted by the extract pass when elapsed wall-clock crosses `memory.dream.max_seconds_per_run` (0 = unbounded); the pass yields after the current session and the **per-session** cursor resumes the remainder on the next trigger (doc 05 §8e). The companion `dream.end` for that pass carries `yielded=True`.

| Field | Type | Description |
|---|---|---|
| `kind` | string | `"extract"` (the only pass with a wall-clock cap) |
| `max_seconds` | int | The `max_seconds_per_run` ceiling that tripped |
| `elapsed_ms` | int | Wall-clock spent when the cap tripped |
| `sessions_done` | int | Sessions extracted before yielding |

Use to detect a session backlog that consistently outruns the per-pass budget (a signal to raise `max_seconds_per_run`).

### 6.6 `memory.dream.throttled`

`MemoryDreamThrottledEvent`. Emitted by the reactive trigger (`durin/cli/commands.py::_spawn_dream`) when the in-process gate skips a `post_compaction` / `session_close` extract. A skipped reactive run is harmless — the per-session cursor makes the next run idempotent.

| Field | Type | Description |
|---|---|---|
| `trigger` | string | The reactive trigger that was skipped (`post_compaction` \| `session_close`) |
| `reason` | string | `"locked"` (a pass was already running) or `"throttled"` (one ran within `min_seconds_between_runs`) |

### 6.7 `memory.dream.always_on`

`MemoryDreamAlwaysOnEvent`. Emitted by the always_on distillation pass (`durin/memory/always_on_dream.py`, A4). The pass gathers feedback entities, an LLM judge ranks them and drops contradictions, the survivors are fitted into a token budget (`memory.dream.always_on_token_budget`), and the selected refs are marked `always_on`. No entity is deleted — only the flag flips.

| Field | Type | Description |
|---|---|---|
| `selected` | int | Items kept `always_on` (fit the token budget) |
| `pruned` | int | Items ranked but that didn't fit the budget |
| `dropped` | int | Items removed by the contradiction judge |
| `tokens` | int | Token budget consumed by the selected set |
| `duration_ms` | int | Wall-clock of the pass |

(The internal `changed` counter — how many flags flipped — is logged but not emitted on this event.)

### 6.8 `memory.dream.discover`

`MemoryDreamDiscoverEvent`. Emitted by `durin/memory/extract_dream.py::discover_entities` once **per session** the extract pass's mention-discovery stage processes (doc 05 §2.1, stage 2). Lets dashboards measure discovery **precision** over time (the ratio of `written` to `proposed`, audited against junk).

| Field | Type | Description |
|---|---|---|
| `proposed` | int | Entities the discovery LLM returned for the session |
| `written` | int | Proposals committed as new/updated dream-authored pages |
| `skipped` | int | Proposals dropped (already handled in stage 1, or tombstoned) |
| `refs` | list[string] | The entity refs written |

---

## 7. Absorb-judge events

`memory.absorb.*`, emitted by `durin/memory/refine_dream.py` (the refine pass) and `durin/cli/memory_cmd.py` (revert). Schemas in `schema.py`; reproduced briefly:

| Event | TypedDict | When |
|---|---|---|
| `memory.absorb.judged` | `MemoryAbsorbJudgedEvent` | A candidate pair reached the LLM judge — carries `canonical` / `absorbed` / `verdict` (`same` \| `different` \| `unclear`) / `confidence` |
| `memory.absorb.auto_merged` | `MemoryAbsorbAutoMergedEvent` | The pair was auto-merged (`verdict == "same"` and `confidence >= confidence_threshold`) |
| `memory.absorb.skipped` | `MemoryAbsorbSkippedEvent` | Pair skipped before/at the judge — `reason` ∈ `cross_type` \| `tombstoned` \| `user_managed` \| `quarantine` \| `below_threshold` \| `verdict_different` \| `verdict_unclear` \| `judge_failed` \| `page_load_failed` |
| `memory.absorb.reverted` | `MemoryAbsorbRevertedEvent` | A prior auto-merge was undone via `durin memory revert` (the regret-rate signal) |

The auto-merge gate respects `memory.dream.auto_absorb.enabled` (A1): when disabled the refine pass does not judge or merge, so `absorb.*` events only appear when auto-absorb is on (or via the manual `durin memory absorb` path). `min_age_hours` quarantine surfaces as `absorb.skipped` with `reason="quarantine"` (B3).

---

## 7a. Relation-cap events (alert-only)

`memory.entity_relation_cap_warned` / `memory.entity_relation_cap_rejected` (`MemoryEntityRelationCapWarnedEvent` / `MemoryEntityRelationCapRejectedEvent`), emitted by `durin/memory/memory_writer.py::_emit_relation_cap` (A3, doc 11). When an entity write would take its relation count across the **soft cap (50)** the `_warned` event fires; across the **hard cap (200)** the `_rejected` event fires.

**These are ALERT-ONLY — not a rollback.** The write still proceeds and no relation is dropped (no data loss); the events are the operator signal that an entity is growing into a mega-hub before sub-paging (audit B-14) becomes necessary. Enforcing the hard cap is a one-line flip in `_emit_relation_cap` if mega-hubs prove real.

| Field | Type | Description |
|---|---|---|
| `entity_ref` | string | The entity being written |
| `current_count` | int | Relation count **before** this write |
| `new_count` | int | Relation count **after** this write |
| `iteration` | int | optional — auto-injected by `emit_tool_event` |
| `session_key` | string \| null | optional — auto-injected by `emit_tool_event` |

---

## 8. Search-failure events

### 8.1 `memory.search.failure`

Shipped in audit B9 (2026-05-28). Emitted once per `run_search_pipeline` invocation where at least one of the safe wrappers (`_safe_vector_search`, `_safe_lexical_search`, `_safe_grep_fallback`) caught an exception. The pipeline always recovers (the surviving sources cover the loss most of the time); the event lets dashboards see degradation rate per component.

| Field | Type | Description |
|---|---|---|
| `component` | string | Comma-joined list of affected sources (`vector`, `lexical`, `grep`). |
| `recovery_attempted` | bool | Always `True` today — the pipeline doesn't ship without recovery. Field kept for forward-compat. |
| `recovery_succeeded` | bool | `True` iff the result set is non-empty despite the failure. |
| `recovery_duration_ms` | float | Time spent inside the failed wrapper(s) before falling through to the surviving sources. |
| `degraded_to` | string | `full` (only `grep` failed and the others covered), `vector_only`, `lexical_only`, `grep_only`, or `none` (recovery_succeeded == False). |

The v1 spec proposed a richer field set (`kind` enum, `recoverable` bool). Audit B9 cut those: the wrappers don't classify the exception type today (catching `Exception`), so emitting `kind="syntax"` vs `kind="timeout"` would be inventing data. If a future operational need surfaces, the wrappers can grow per-exception handlers and the field can be added then.

---

## 9. Index events (indexer health)

### 9.1 `memory.index.write`

Emitted by `durin/memory/indexer.py::reindex_one_file` whenever the
indexer re-derives a row. Audit E5 (2026-05-28) aligned the payload
with the dashboards documented in §10.3 (perf) and doc 09 §216
(capacity).

| Field | Type | Description |
|---|---|---|
| `uri` | string | Item indexed (e.g. `person:marcelo`, `episodic/2026/...`) |
| `op` | enum | `upsert | delete` |
| `index` | enum | `fts` always today; `lancedb` reserved for the future per-row vector path |
| `trigger` | enum | `watcher | dream_apply | drift_repair` — see taxonomy below |
| `duration_ms` | float | Wall-clock of the FTS upsert/delete call (powers `index_write_p95_ms` alert in §10.3) |

**Trigger taxonomy** — pre-E5 spec listed `tool_write` and
`manual_rebuild`; both were dropped because they don't apply at
this layer:

- `tool_write` — agent writes go through `reindex_one_file` via the
  file watcher; no tool calls the indexer directly.
- `manual_rebuild` — `durin memory reindex` CLI invokes `rebuild_fts_index`
  which emits `memory.index.rebuild` (§9.2), not `.write`.

The real triggers are:

- `watcher` (default) — `MemoryFileWatcher` picked up a `.md` change.
  Steady-state load. Bulk of events.
- `dream_apply` — the reactive index path re-indexed an entity page
  after a dream write (the trigger label is retained from the
  pre-migration consolidator for dashboard continuity). Bursty around
  cron / reactive triggers.
- `drift_repair` — `HealthChecker.run_tick` repaired a stale row.
  Rare; persistently > 0 means the watcher is missing events.

`targets` (multi-index list) and `embedding_skipped` (mtime
short-circuit) from the v1 spec were dropped — only FTS is written
from this callsite today, and there is no mtime short-circuit. If a
future change adds per-row vector writes or mtime caching, the
fields can be reintroduced then.

### 9.2 `memory.index.rebuild`

Emitted by `durin.memory.indexer._emit_rebuild`. Audit F10
(2026-05-28) aligned this section to the shipped TypedDict — the
pre-F10 spec named fields that no production callsite ever emits
(`entities_count`, `embedding_batches`, `prior_index_existed`).

| Field | Type | Description |
|---|---|---|
| `target` | enum | `fts` (or `all` / `lancedb` in future). Identifies which index was rebuilt; `durin memory reindex --target fts` always emits `"fts"` today. |
| `indexed` | int | Rows successfully written (`IndexStats.indexed`) |
| `errors` | int | Rows that failed to index (`IndexStats.errors`) |
| `duration_ms` | float | Wall-clock of the rebuild |
| `reason` | string | optional — when present, surfaces why the rebuild happened (e.g. `schema_version_bump`); absent for plain operator-triggered rebuilds |

### 9.3 `memory.index.staleness_detected`

Emitted by `durin.memory.indexer._emit_staleness` whenever the
health-check cron detects a row whose `fts_meta.mtime` lags behind
the file's mtime, or a file under `memory/` that has no row in the
index. Audit G3 (2026-05-28) re-added `delta_seconds` after F11
wrongly justified dropping it; `action` stays dropped.

| Field | Type | Description |
|---|---|---|
| `uri` | string | The stale URI |
| `reason` | enum | `missing_row` (file exists, no FTS row) `| mtime_lag` (FTS row's `indexed_at < file mtime - 60s tolerance`) `| row_for_missing_file` (FTS row exists but the source `.md` was deleted) |
| `delta_seconds` | float | optional — set ONLY when `reason='mtime_lag'`; carries `current_file_mtime - indexed_mtime` so dashboards can graph p50/p95 staleness magnitude. Missing on `missing_row` and `row_for_missing_file` because there is no indexed_mtime to compare against. |

**Why `delta_seconds` and not the F11 "implicit join" justification**:
F11 claimed the magnitude is implicit in joining
`staleness_detected@T1 → memory.index.write@T2`. That join surfaces
the **recovery latency** (T2 − T1), which is a different metric
from the **staleness magnitude** (T1 − indexed_mtime). Recovery
latency tells you how fast the cron repaired the gap once detected;
staleness magnitude tells you how far behind the watcher fell
before the cron tick fired. Operators need both.

The pre-F11 spec also proposed an `action` field
(`re_derived | filtered | queued`). That stays dropped because the
cron always re-derives via `reindex_one_file` — the enum would be
single-valued and meaningless.

### 9.4 `memory.health_check`

Emitted by the background health-check cron on every tick — both passing and failing probes, so a dashboard can graph "uptime" of each component.

**Scheduling (audit A11, 2026-05-28).** `HealthChecker.run_tick()` is driven by `HealthCheckScheduler`, a daemon thread started by `AgentLoop.__init__` when `cfg.memory.health_check.enabled` is true — **default ON**. The default interval is **900 seconds (15 min)**; configurable via `[memory.health_check] interval_seconds`. The first tick fires immediately so a fresh process gets a probe in its first interval window; subsequent ticks wait the configured interval. `AgentLoop.stop()` drains the scheduler via `threading.Event` so shutdown is responsive even with a large interval (no long sleep to outlast).

Shipped pre-A6 (commit `022d4b1`, P2.4) with `status`, `components`, `drift_count`, optional `errors`. Audit A6 (2026-05-28) added `tick_id` and `duration_ms`. Both additions are additive — pre-A6 consumers continue to work (there were none in-tree at the time, but the contract is preserved).

| Field | Type | Description |
|---|---|---|
| `tick_id` | string (32-char UUID hex) | Per-tick correlation id — useful when many ticks land in the same logging window. |
| `status` | string | Aggregate: `ok` (all probes pass), `degraded` (one or more probes fail but not yet escalated), or `critical` (a component reached the 3-consecutive-failure threshold and an alert was emitted). |
| `components` | `dict[str, str]` | Per-probe status. Keys are probe names (`fts`, `lance` — more may be added without payload restructuring). Values are `ok`, `fail`, or `skipped`. |
| `drift_count` | int | Files re-indexed during this tick's repair pass (via `detect_index_staleness` → `reindex_one_file`). A persistently high count signals a watcher gap. |
| `duration_ms` | float | Wall-clock of the tick (probes + drift repair + emit). |
| `errors` | `dict[str, str]` (optional) | Per-component error messages when a probe returned `fail`. Present only when at least one component failed. |

**Shape decisions and what's deliberately NOT emitted** (recorded in doc 11 A6 so future contributors don't re-add them without evidence):

- **No `triggered_by`**: only one trigger (`scheduled`) exists today. Adding an enum with a single value is noise. When `eager_post_failure` (or another trigger) gets implemented, add the field then.
- **No nested `components` (`{name: {status, details}}`)**: a flat `components` map + a separate `errors` map carries the same information with less structure. Matches the convention in Prometheus/OpenTelemetry (labels separate from metric values).
- **No `restorations_attempted` / `restorations_succeeded`**: `_repair_drift` runs silently per drift issue; the `drift_count` already signals that repair work happened, and `errors` carries any per-component failures. Add these counters only when an operator alarm motivates it.

When the tick's drift repair triggers re-indexes, those emit their own `memory.index.write` events (§9.1) for traceability.

### 9.5 `memory.health.critical`

Emitted once when a component crosses the consecutive-failure threshold (3 strikes by default). Re-armed on the next successful tick for that component, so a recovered-then-re-failing component escalates again.

| Field | Type | Description |
|---|---|---|
| `component` | string | The probed component that crossed the threshold. Today: `fts` or `lance` (more may be added without payload restructuring). |
| `consecutive_failures` | int | Count that triggered the threshold (typically 3). |
| `last_error` | string | Last probe error message, truncated to 200 chars. |
| `manual_recovery_hint` | string | The CLI command an operator runs to rebuild the failed component — e.g. `durin memory reindex --target fts` for `fts`, `durin memory reindex --target lancedb` for `lance`. The mapping lives in `_RECOVERY_HINTS` in `durin/memory/health_check.py`; the anti-drift test (`tests/memory/test_health_critical_a7_recovery_hint.py`) keeps the hint targets aligned with the CLI's accepted target set. Audit A7 added this field — the v1 spec proposed `durin reindex --target lancedb` (missing `memory` subcommand); the in-code constants use the verified prefix. |

**Naming note**: the probe component name (`lance`) differs from the CLI `--target` value (`lancedb`). This is legacy drift between the two modules; `_RECOVERY_HINTS` translates at the boundary so operators see a runnable command, not a probe name they have to interpret. If we ever reconcile the names, the dict still works (and the anti-drift test catches a half-finished rename).

This is the event operators should alarm on.

---

## 10. Metrics derived from events

These are aggregations the operator should track (via dashboards or periodic checks).

They are computed by `durin/memory/stats.py` — a **read-only** aggregator that walks
`~/.cache/durin/telemetry/*.jsonl` for events and the workspace filesystem for
ground-truth counts — and surfaced via `durin memory stats [--days N] [--json]`
(`cli/memory_cmd.py`). Aggregation is the prerequisite for evaluating any
metric-gated decision: without it, gates like eager-inject or auto-absorb
thresholds are faith-based rather than observable.

### 10.1 Hot-path health

| Metric | Source | Healthy range |
|---|---|---|
| `recall_p95_ms` | `memory.recall.duration_ms` | < 130ms (default), < 900ms (cross-encoder ON) |
| `recall_p99_ms` | same | < 200ms (default), < 1500ms (rerank ON) |
| `recall_recovery_rate` | `memory.recall.recovered_from != null` / total | < 1% (recovery should be rare) |
| `recall_result_count_p50` | `memory.recall.result_count` | 5-10 (low → maybe coverage gap) |
| `recall_strategy_distribution` | `memory.recall.strategy` | mostly `hybrid` or `vector`; `grep` fallback rare |

### 10.2 Cold-path / Dream

| Metric | Source | Healthy range |
|---|---|---|
| `dream_passes_per_day` | count of `memory.dream.start` (split by `kind`) | 5-50 |
| `dream_extract_failure_rate` | `memory.dream.end{kind=extract}.entities_failed` / `sessions` | < 2% |
| `dream_throttled_rate` | `memory.dream.throttled` / total reactive triggers | < 30% |
| `dream_yield_rate` | `memory.dream.max_seconds_reached` (or `dream.end.yielded=True`) / extract passes | low → near 0 (persistent yields ⇒ raise `max_seconds_per_run`) |
| `dream_duration_p95_ms` | `memory.dream.end.duration_ms` | < 60s (per pass) |
| `relation_cap_warnings_per_week` | count of `memory.entity_relation_cap_warned` | low (a rising count ⇒ a forming mega-hub) |

### 10.3 Index health

| Metric | Source | Healthy range |
|---|---|---|
| `index_write_p95_ms` | `memory.index.write.duration_ms` | < 50ms (per row) |
| `staleness_events_per_day` | count of `memory.index.staleness_detected` | < 10 |
| `index_rebuild_frequency` | count of `memory.index.rebuild` | manual / rare |

### 10.4 Absorb / dedup

| Metric | Source | Healthy range |
|---|---|---|
| `absorb_decisions_per_day` | count of `memory.absorb.judged` | depends on workspace activity |
| `absorb_merge_rate` | `auto_merged / judged` | 5-30% (depends on alias overlap density) |
| `absorb_reverts_per_week` | count of `memory.absorb.reverted` | 0-1 (high revert rate = bad judge) |

---

## 11. Alarms / alerts (suggested)

When telemetry collection lands in a dashboard, these conditions should alert:

| Alert | Trigger | Severity |
|---|---|---|
| Recall p95 > 2× expected | continuous over 1 hour | warn |
| Recall persistent recovery (>5% rate) | over 1 hour | warn |
| `memory.entity_relation_cap_rejected` (hard cap crossed) | single event | warn (mega-hub forming; alert-only, the write still landed) |
| Dream extract failure rate > 5% | rolling 24h | warn (sessions repeatedly failing extraction) |
| Recall returning 0 results > 10% of calls | 1 hour rolling | warn (could be a real "nothing in memory" or a search bug) |
| `memory.search.failure` with `recovery_succeeded=false` | single event | error |
| Index rebuild took > 5 minutes | single event | warn (workspace might have grown unexpectedly) |
| Absorb reverted > 3 times in 24h | rolling sum | error (judge is making bad calls) |

These thresholds are guidelines; they should be tuned once we have weeks of production telemetry.

---

## 12. Sampling and retention

### 12.1 Sampling

- All events are emitted unconditionally (no sampling in MVP).
- Memory subsystem volumes are small (~100-1000 events/day for a single workspace).
- If volume grows beyond ~1M events/day (very high activity), sampling can be introduced — only on high-cardinality recall events.

### 12.2 Retention

- Local file storage at `~/.cache/durin/telemetry/*.jsonl` (per-session, per-day file).
- Rotation: see `durin/telemetry/retention.py` — `COMPRESSION_AGE_DAYS=30`, `DELETION_AGE_DAYS=90`. Files older than the compression threshold are gzipped in place; archives older than the deletion threshold are removed. Defaults can be raised by editing the constants (configurable retention is `08_scope_and_discarded.md` B5 — deferred until an operator asks).
- Retention runs on the health-check tick (P7.2 piggyback). No separate cron.

### 12.3 Opt-in HTTPS push (audit A8)

Telemetry is NOT pushed to any remote server by default. The `PushSink` library (`durin/telemetry/push.py`) supports fan-out to an HTTPS endpoint for operators who want to centralize observability data — e.g. into Grafana/Loki, Datadog, a custom collector, or an internal durin dashboard.

**Why this exists**: telemetry is first-class infrastructure for understanding what durin does and how it's used. The local JSONL is always the source of truth, but to query, alarm, and analyze across sessions/machines, an export pipeline matters. PushSink is the pipe.

**Configuration** (default OFF):

```toml
[telemetry.push]
enabled = false
url = ""                              # e.g. "https://collector.example.com/durin"
token_secret_name = ""                # name of the secret in ~/.durin/secrets.json
batch_size = 10                       # events buffered before a POST
```

**Auth**: the bearer token NEVER lives in `config.json`. Set it in the secret store:

```sh
durin secrets set DURIN_TELEMETRY_PUSH_TOKEN <token>
```

…and reference the name in config (`token_secret_name = "DURIN_TELEMETRY_PUSH_TOKEN"`).

**Privacy implications**: every event the local JSONL receives also POSTs to the endpoint. That includes the truncated `query`, `text`, `snippet`, `content`, `needle` fields (200 char max per `_truncate_freetext`). Enable push ONLY when:

- The endpoint is YOUR OWN infrastructure (or a service you've vetted for the kind of content durin events carry).
- You understand that 200-char truncations of queries can still contain personal information.
- TLS is enforced on the URL (`https://...` only).

**Behaviour**:

- Wiring is in `durin/telemetry/wiring.py::wire_push_sink`, invoked once per session by `AgentLoop`.
- Misconfiguration (URL or `token_secret_name` empty, or the secret not in the store) → push is silently disabled and the local JSONL keeps working. The startup log surfaces a warning so the operator can fix it.
- `PushSink.log()` is isolated in a try/except inside the logger's fan-out loop — a failing push NEVER breaks the JSONL write or the calling tool.
- On agent shutdown, `AgentLoop` calls `push_sink.flush()` so the last partial batch isn't lost.
- Failed POSTs are restored to the buffer by `PushSink._drain()`; the next batch retries them.

---

## 13. Privacy considerations

Telemetry events include user-facing data (query strings, entity URIs). For an installation that processes sensitive content:

- Query strings are **truncated to 200 chars** in telemetry — full query never logged.
- Entity URIs (e.g., `person:marcelo`) are logged. If this is sensitive, the operator can configure URI hashing in telemetry config.
- Tool result content is NEVER logged in telemetry — only counts and metadata.
- LLM input/output tokens are NEVER logged in full — only counts.

These defaults protect against accidental data leak when telemetry is shared with developers for debugging.

---

## 14. Module-level decisions

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Event categories | 6: recall, store, ingest, dream, absorb, search-fail, index. Mirrors operational concerns. | §3 |
| 2 | Sub-events for recall pipeline | recall.vector, recall.lexical, recall.rerank, recall.rrf — separate per step for diagnosis. | §4 |
| 3 | New event for failure handling | `memory.search.failure` per §14.5 doc 03; carries recovery + degradation info. | §8 |
| 4 | New events for indexer | `memory.index.write`, `.rebuild`, `.staleness_detected` — observability for the indexer's health. | §9 |
| 5 | Relation-cap telemetry | `memory.entity_relation_cap_warned/rejected` fire when an entity write crosses the soft/hard cap — **alert-only** (A3): the write proceeds, nothing is dropped. Operator watches for forming mega-hubs. | §7a |
| 6 | No external telemetry sink in MVP | Local jsonl file. Optional HTTPS push later. | §12 |
| 7 | Sampling = none in MVP | Volume is low; emit everything. | §12.1 |
| 8 | Retention | 30 days uncompressed + 60 more days compressed (90 days total). The original v1 draft proposed "1 year compressed"; aligned to code in audit B5 (`COMPRESSION_AGE_DAYS=30`, `DELETION_AGE_DAYS=90` in `durin/telemetry/retention.py`). | §12.2 |
| 9 | Privacy defaults | Query truncated, URIs logged, content NEVER logged. | §13 |

### Open

None at the module level.

---

## 15. Implementation status

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| Memory event registry | The `memory.*` keys in `EVENTS` cover recall (incl. `.vector` / `.lexical` / `.rerank` / `.rrf` / `.failure`), index (`.write` / `.rebuild` / `.staleness_detected`), dream (`.start` / `.end` / `.patch_applied` / `.skill_extract` / `.max_seconds_reached` / `.throttled` / `.always_on`), absorb (`.judged` / `.auto_merged` / `.skipped` / `.reverted`), store, ingest, forget, upsert_entity, relation-cap (`entity_relation_cap_warned` / `_rejected`), embedding, hot_layer, health, skill_miss, fallback_tool_used. The post-migration sweep (doc 11 B2) removed the deleted `dream.{skipped,entity_failed,budget_exhausted,legacy.*}` events. `tests/telemetry/test_schema_catalog.py` enforces catalog↔emission by name. | — |
| Cost in dream.end | Not emitted. The deleted `DreamRunner` carried `llm_*_tokens_total` / `llm_call_count` on `dream.end`; the four-pass dream does not aggregate per-call usage, so those fields are gone (doc 11 B2). | If dream cost telemetry is wanted again, add usage aggregation in the passes and new fields — the cost ledger itself stays upstream (see §1 "out of scope"). | None |
| Privacy: query truncation | Enforced at emit time (audit C6: was incorrectly "Not enforced" in the v1 draft). `durin/agent/tools/_telemetry.py::_truncate_freetext` trims fields named `query`, `text`, `snippet`, `content`, `needle` to 200 chars before persistence. Applied by `emit_tool_event` so every event consumer gets a trimmed payload. | — | — |
| Privacy: URI hashing opt-in | Not present | Optional via config | New config flag |
| Alarms / dashboards | None | Internal threshold checks + optional Grafana export | Out of scope for memory subsystem; downstream |

---

## 16. Cross-references

- Tool calls that emit recall events: `04_agent_tools.md` §2-§5.
- Search pipeline failure mode + recovery (which emit `memory.search.failure`): `03_search_pipeline.md` §14.
- The four-pass dream (extract / refine / skill / always_on) that emits the `memory.dream.*` events: `05_dream_cold_path.md`.
- Relation-cap alert-only decision (A3), cursor removal (N3), and the deleted dream cluster.
- Indexer write triggers (which emit `memory.index.write`): `02_indexing.md` §6.
- Absorb-judge + relation-cap event schemas: `durin/telemetry/schema.py`.
