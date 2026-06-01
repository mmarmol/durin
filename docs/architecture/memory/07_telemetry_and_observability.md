---
title: Telemetry and observability
version: 0.1-draft
status: current — describes the shipped system (P11 era, 2026-05-30)
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 03_search_pipeline.md, 05_dream_cold_path.md
related: 04_agent_tools.md
---

# Telemetry and observability

This document specifies the telemetry events emitted by the memory subsystem, the metrics derivable from them, and the operational dashboards / alerts that should be wired up. Telemetry is the only way humans observe whether the system is behaving correctly — without it, regressions silently degrade retrieval quality and Dream cost accumulates unnoticed.

**Principle:** every decision point that could fail, degrade, or surprise the operator emits an event. Events are JSON, structured, and grep-friendly. Aggregation is downstream.

---

> **Implementation status:** events marked **NEW** in this document are v2 targets that do not yet exist in `durin/telemetry/schema.py`. The existing events (per `_REGISTRY` in schema.py) are explicitly noted as "already exists". Adding the new events is tracked as a deliverable in `09_implementation_roadmap.md` Phase 7 (Telemetry v2).

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
- Cost accounting for LLM calls — that's its own subsystem (could derive from `memory.dream.end.cost_usd` if added, but the cost ledger is upstream).

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

The `event` name is the registry key. Payload schemas are validated against the `_REGISTRY` in `schema.py` — adding a new event type requires adding a TypedDict and registering it.

---

## 3. Event categories

| Category | Path | When emitted | Purpose |
|---|---|---|---|
| Recall | `memory.recall*` | Every `memory_search` call (and sub-paths) | Hot-path retrieval observability |
| Store | `memory.store*` | Every `memory_store` call | Write observability + dedup tracking |
| Ingest | `memory.ingest*` | Every `memory_ingest` call | Ingest observability |
| Dream | `memory.dream.*` | Every Dream pass (start, end, skipped, per-entity result) | Cold-path consolidation observability |
| Absorb | `memory.absorb.*` | Every absorb-judge decision (auto-merge, skipped, reverted) | Dedup observability |
| Search-fail | `memory.search.failure` | Whenever a search-path component fails (recoverable or not) | Recovery + degradation tracking |
| Index | `memory.index.*` | Index re-derivation events (per write, per rebuild) | Indexer health |
| Embedding | `memory.embedding.*` | Model load (`.load`) + per-embed timing (`.embed`) from `FastembedProvider` | Provider performance + eviction signals |
| Hot layer | `memory.hot_layer.failure` | When the hot-layer renderer fails to assemble a context block (read error, parse error) | Context-assembly resilience signal |
| Health | `memory.health_check`, `memory.health.critical` | Every health-check tick (A11) + 3-strike escalation (A7) | Subsystem availability monitoring |

Audit B10 (2026-05-28) added the `Embedding`, `Hot layer`, and `Health` rows — these events are emitted by the code but the original §3 table omitted them. The TypedDicts live in `durin/telemetry/schema.py`.

> **Source of truth.** The exhaustive, authoritative list of event types is `durin/telemetry/schema.py::EVENTS` — `tests/telemetry/test_schema_catalog.py` enforces, in both directions, that every event emitted in the source tree has a catalog entry and vice versa. **This document annotates the events whose fields or usage need explanation; it does not mirror the catalog event-for-event.** A new event shipping without a subsection here is expected, not drift — consult `schema.py` for the complete set. (For example `memory.dream.budget_exhausted` — §6.6 below — plus the `memory.dream.legacy.*` family and `memory.fallback_tool_used` all exist in the catalog.)

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

### 6.1 `memory.dream.start`

Already exists.

| Field | Type | Description |
|---|---|---|
| `trigger` | string | `threshold | post_ingest_threshold | cron_daily | post_compaction | session_close | manual` (audit F18, 2026-05-28 added `post_ingest_threshold`; matches the `dream.end` enum in §6.2 and the `threshold_trigger.py` ingest path) |
| `entity_filter` | string \| null | If filter was applied |
| `entities_pending` | int | How many entities have post-cursor entries |

### 6.2 `memory.dream.end`

Shipped pre-A5 with `trigger` + `entity_filter` + `entities_consolidated` + `entities_failed` + `duration_s`. Audit A5 (commit landing this doc update) added the four cost-telemetry fields below, renamed `duration_s` → `duration_ms`, and removed the old `duration_s` field. Consumers that pinned to `duration_s` need to update.

| Field | Type | Description |
|---|---|---|
| `trigger` | string | One of `threshold | post_ingest_threshold | cron_daily | post_compaction | session_close | manual`. |
| `entity_filter` | string | When set, restricted the pass to one entity (empty string when no filter). |
| `entities_consolidated` | int | Entities whose `apply` succeeded. |
| `entities_failed` | int | Entities whose `consolidate_entity` or `apply` raised. |
| `entities_quarantined` | int | Entities whose failure was the third structural strike (`dream_quarantine` field set on the page). Subset of `entities_failed`. |
| `llm_call_count` | int | LLM calls across the pass — sums initial + retries per entity. |
| `llm_input_tokens_total` | int | Prompt tokens summed across every LLM call. Cost = `total_input_tokens * input_rate + total_output_tokens * output_rate`. |
| `llm_output_tokens_total` | int | Completion tokens summed across every LLM call. |
| `duration_ms` | float | Wall-clock of the full pass (lock → consolidate → release). Replaces the pre-A5 `duration_s`. |

> **Token totals are best-effort.** A provider that doesn't surface `usage` (legacy `LLMInvoke` mocks, custom transports) leaves the counts at 0. The Dream cost alarm (doc 08 §3 R3) under-reports when tokens are missing — the safe-failure direction (no false positives). See `LLMResponse` in `durin/memory/dream.py`.

### 6.3 `memory.dream.skipped`

Already exists.

| Field | Type | Description |
|---|---|---|
| `trigger` | string | The trigger that was skipped |
| `reason` | enum | `throttle | concurrent_lock | no_pending | disabled` |
| `entity_filter` | string \| null | If filter was applied |

### 6.4 `memory.dream.entity_failed`

Emitted by `durin.memory.dream_apply._emit_apply_telemetry` whenever
one entity's apply step fails. Audit F6 (2026-05-28) aligned this
section to the shipped `DreamApplyFailureKind` enum.

| Field | Type | Description |
|---|---|---|
| `entity_ref` | string | The entity that failed to consolidate (e.g. `person:marcelo`) |
| `trigger` | string | Same trigger label the dream pass started with (`threshold`, `cron_daily`, `manual`, …) — see §6.1 |
| `kind` | enum | `validation | patch_runtime | round_trip | io` — values of `DreamApplyFailureKind` |
| `error_message` | string | Truncated to 500 chars (caller-side) |
| `failure_count_now` | int | Post-increment counter; `0` for ambient (`io`) and pre-quarantine increments are emitted before the page is persisted, so structural kinds may also emit `0` here when the increment is recorded on the page separately |
| `quarantined_until` | string | optional — ISO timestamp set only when this failure crossed the 3-strike quarantine threshold |

Failure kind taxonomy (cross-references doc 05 §12):

- **Structural** (count toward quarantine): `validation`, `patch_runtime`, `round_trip`.
- **Ambient** (do NOT count): `io` (disk write errors). Upstream LLM call failures bubble up before `dream_apply` runs and are tallied by the runner totals, not by this event.

The pre-F6 spec referenced `llm_call_failed` / `parse_failed` as kinds emitted on this event; neither value is actually emitted by any production callsite (the upstream consolidator path does not emit `memory.dream.entity_failed` on raw LLM failures — it just bubbles the exception up to the runner so the entity is skipped and the next trigger retries).

### 6.5 `memory.dream.patch_applied`

NEW.

Emitted by `durin.memory.dream_apply._emit_apply_telemetry` on
every successful apply. Audit F8 (2026-05-28) aligned this section
to the shipped TypedDict — the pre-F8 spec named fields that no
production callsite ever emits (`entity_uri`, `op_count`,
`commit_sha`, `cursor_advanced_to`).

| Field | Type | Description |
|---|---|---|
| `entity_ref` | string | Target entity (e.g. `person:marcelo`) |
| `trigger` | string | Pass trigger (same vocabulary as §6.1) |
| `ops_applied` | int | Count of JSON Patch ops that landed |
| `sources_count` | int | Distinct `provenance` values across the applied ops (how many source observations contributed) |
| `body_delta_chars` | int | Length of the `===BODY_DELTA===` block in chars |
| `cursor_after` | string | The ISO timestamp stamped on the page (`dream_processed_through`) after this apply |
| `duration_ms` | float | Wall-clock of the apply step |

The commit SHA is deliberately not emitted. F8 (2026-05-28) framed this as "dashboards can join via `entity_ref + cursor_after`"; G8 (2026-05-28) corrected the rationale because that join is fragile (requires parsing commit-message trailers, ambiguous when two entity touches share a cursor in the same pass). The real reason the field stays out: the realistic consumers (operator forensics, audit) use `git log memory/entities/<type>/<slug>.md` directly — the file path is known from the event's `entity_ref`, and `git log` carries the trailers per doc 05 §6. A debug dashboard would benefit from the SHA in telemetry but no such consumer exists. If one ever does, the cheap path is a NEW event `memory.dream.commit_recorded` fired after `repo.commit(...)` returns in `dream.py::apply()`, joining to `memory.dream.patch_applied` on `(session_key, iteration, entity_ref)`. See doc 08 §2.16 for the full reasoning.

### 6.6 `memory.dream.budget_exhausted`

Emitted by `DreamRunner` when an entity's accumulated wall-clock crosses `max_seconds_per_run` *after* a successful batch in the FIFO drain loop (so each entity always makes at least one batch of forward progress; doc 05 §4.4). The remaining pending entries are deferred to the next pass.

| Field | Type | Description |
|---|---|---|
| `trigger` | string | Pass trigger (same vocabulary as §6.1) |
| `entity_ref` | string | Entity whose drain was cut short |
| `pending_remaining` | int | Entries left unconsolidated, deferred to the next pass |
| `elapsed_s` | float | Wall-clock spent on this entity when the budget tripped |
| `budget_s` | int | The `max_seconds_per_run` ceiling |

Use to detect entities whose backlog consistently outruns the per-pass budget (a signal to raise `max_seconds_per_run` or investigate why one entity accumulates so many entries).

*The legacy `Dream` consolidator (`durin/agent/memory.py`) emits its own `memory.dream.legacy.{start,end,skipped}` family — see `schema.py` for those shapes; they mirror the entity-centric events but for the session-history consolidation path.*

---

## 7. Absorb-judge events

Already exist (`memory.absorb.*`). Reproduced briefly for completeness:

| Event | When |
|---|---|
| `memory.absorb.judged` | A pair was judged (same / different / unclear) |
| `memory.absorb.auto_merged` | The pair was merged |
| `memory.absorb.skipped` | Pair was skipped (quarantine, cross-type, etc.) |
| `memory.absorb.reverted` | A merge was undone (manual operator action) |

Schemas in `schema.py`. Not redefining here.

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
- `dream_apply` — `Consolidator.apply` re-indexed the entity page
  after a successful consolidation. Bursty around cron triggers.
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

These are aggregations the operator should track (via dashboards or periodic checks):

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
| `dream_passes_per_day` | count of `memory.dream.start` | 5-50 |
| `dream_skipped_rate` | `memory.dream.skipped` / total triggers | < 30% |
| `dream_entity_failure_rate` | `memory.dream.entity_failed` / total entities consolidated | < 2% |
| `dream_quarantined_entities` | accumulated `entities_quarantined` | 0-2 (alerting threshold) |
| `dream_llm_cost_per_day_usd` | sum of `llm_input_tokens × price + ...` | $0.25-$1.50/day (target soak range per doc 09 §11.1); two-tier alarm — warn at $1.50/day (audit F19, 2026-05-28), error at $5/day (§11 below + doc 08 §3 R3) |
| `dream_duration_p95_ms` | `memory.dream.end.duration_ms` | < 60s (per pass) |

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
| Dream entities_quarantined > 2 | single event | error |
| Dream LLM cost > $1.50/day | rolling 24h sum | warn (audit F19, 2026-05-28: warn tier — operator inspects) |
| Dream LLM cost > $5/day | rolling 24h sum | error |
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
| 5 | Quarantine telemetry | `memory.dream.entity_failed` emits with `quarantined: true` when triggered. Operator alarms on this. | §6.4 |
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
| Memory event registry | 25+ events in `schema.py` (audit C6: corrected from "12 events". `memory.*` keys in `EVENTS` cover recall (incl. `.lexical` / `.rerank` / `.rrf` / `.failure`), index (`.write` / `.rebuild` / `.staleness_detected`), dream (`.start` / `.end` / `.skipped` / `.entity_failed` / `.patch_applied`), absorb, store, ingest, embedding, hot_layer, health). Counts grow as new events ship (A5 added cost fields to `dream.end`; A6 added `tick_id`/`duration_ms` to `health_check`; B9 added `search.failure`; 2026-05-30 removed `recall.decay` with temporal decay). | — |
| Cost in dream.end | Shipped (audit A5, 2026-05-28). `memory.dream.end` carries `llm_input_tokens_total`, `llm_output_tokens_total`, `llm_call_count`. The `default_llm_invoke` extracts per-call usage from litellm `response.usage`; `_ConsolidateTotals` aggregates across the pass. See §6.2 and audit E6. | Optional `llm_cost_usd` would multiply by per-model price; left out because the cost ledger is upstream (see §1 "out of scope"). | None |
| Privacy: query truncation | Enforced at emit time (audit C6: was incorrectly "Not enforced" in the v1 draft). `durin/agent/tools/_telemetry.py::_truncate_freetext` trims fields named `query`, `text`, `snippet`, `content`, `needle` to 200 chars before persistence. Applied by `emit_tool_event` so every event consumer gets a trimmed payload. | — | — |
| Privacy: URI hashing opt-in | Not present | Optional via config | New config flag |
| Alarms / dashboards | None | Internal threshold checks + optional Grafana export | Out of scope for memory subsystem; downstream |

---

## 16. Cross-references

- Tool calls that emit recall events: `04_agent_tools.md` §2-§5.
- Search pipeline failure mode + recovery (which emit `memory.search.failure`): `03_search_pipeline.md` §14.
- Dream entity failure + quarantine (which emit `memory.dream.entity_failed`): `05_dream_cold_path.md` §12.5.
- Indexer write triggers (which emit `memory.index.write`): `02_indexing.md` §6.
- Absorb-judge events already in production: `durin/telemetry/schema.py`.
