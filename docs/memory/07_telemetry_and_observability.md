---
title: Telemetry and observability
version: 0.1-draft
status: under construction
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

---

## 4. Recall events (hot path)

Emitted by `memory_search` and its sub-pipeline.

### 4.1 `memory.recall`

Top-level event, emitted once per `memory_search` call.

| Field | Type | Description |
|---|---|---|
| `query` | string | Query string (truncated to 200 chars for telemetry storage) |
| `keywords` | string \| null | Optional keywords param |
| `scope` | enum | `dreamed | undreamed | all` |
| `level` | enum | `warm | cold` |
| `result_count` | int | Final count returned (after limit) |
| `total_candidates` | int | Total candidates before limit |
| `strategy` | enum | `vector | hybrid | grep | failed` (which path produced results) |
| `recovered_from` | string \| null | If recovery activated, the component that recovered (`lancedb_rebuild`, etc.) |
| `recovery_duration_ms` | float \| null | Recovery overhead |
| `duration_ms` | float | Total search duration |

### 4.2 `memory.recall.vector`

Vector retrieval sub-path. Already exists in `schema.py:442` (`MemoryRecallVectorEvent`).

Fields covered there are sufficient; additions for v2:

| Added field | Type | Description |
|---|---|---|
| `cross_encoder_active` | bool | Was the cross-encoder step run? |
| `cross_encoder_duration_ms` | float \| null | Time spent in cross-encoder if active |

### 4.3 `memory.recall.lexical`

NEW event for FTS5 path observability.

| Field | Type | Description |
|---|---|---|
| `query` | string | Truncated query |
| `tokenizer_used` | enum | `unicode61 | trigram | like_fallback` (per §5.4 doc 02) |
| `hit_count` | int | FTS5 returned hits |
| `duration_ms` | float | FTS5 query duration |

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

NEW event for the cross-RRF fusion step.

| Field | Type | Description |
|---|---|---|
| `sources_active` | list of strings | e.g., `["vector", "lexical"]` or `["vector", "lexical", "grep"]` |
| `keyword_boost_applied` | bool | Whether `w_lexical` was boosted because `keywords` was provided |
| `dedup_count` | int | How many uris appeared in multiple sources (i.e., the "co-occurrence boost" landed on N items) |
| `duration_ms` | float | RRF computation duration |

### 4.6 `memory.silent_retrieval_miss`

NEW event. Detects turns where the agent should have invoked `memory_search` but didn't, and the user signaled dissatisfaction. Used as the **telemetry-driven trigger** to activate the §2.F eager pre-fetch feature (currently deferred — see `08_scope_and_discarded.md` §4.1).

**When emitted:** at the start of turn N+1, if BOTH conditions hold:

1. In turn N, the agent's response did NOT include a `memory_search` tool call (despite the user message being question-shaped or referencing past context).
2. In turn N+1, the user's message looks like a re-ask, correction, or negation of the prior response. Heuristic detection:
   - Substring overlap > 60% with turn N's user message (re-ask)
   - Starts with negation tokens ("no,", "wrong,", "actually,", "that's not...")
   - Contains correction patterns ("I said X, not Y", "you forgot...")

| Field | Type | Description |
|---|---|---|
| `turn_n_user_message_hash` | string | Hash of turn N user message (privacy: no full text) |
| `turn_n_agent_invoked_tools` | list of strings | Which tools the agent called in turn N (empty list if none) |
| `detector_signals` | list of enum | Which of the 3 heuristics fired: `re_ask | negation | correction` |
| `session_id` | string | For correlation |

**Aggregate metric:** `silent_retrieval_miss_rate` = count of this event / total user turns. **Action trigger:** if rate > 5% over a rolling 7-day window, the operator should evaluate activating §2.F. This is the data-driven case for that feature.

**False positives accepted.** The heuristics will trigger on legitimate user negations that have nothing to do with memory ("no, do X instead"). The metric is a rate, not a per-event signal — only the aggregate matters.

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
| `trigger` | string | `threshold | cron_daily | post_compaction | session_close | manual` |
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

NEW.

| Field | Type | Description |
|---|---|---|
| `entity_uri` | string | The entity that failed to consolidate |
| `kind` | enum | `llm_call_failed | parse_failed | validation_failed | round_trip_failed` |
| `attempt_count` | int | This entity's running failure count (0-3) |
| `quarantined` | bool | Whether this failure triggered quarantine |
| `error` | string | Short error message |

This is the event downstream alerts watch to detect persistent broken entities.

### 6.5 `memory.dream.patch_applied`

NEW.

| Field | Type | Description |
|---|---|---|
| `entity_uri` | string | Target entity |
| `op_count` | int | Number of JSON Patch ops applied |
| `body_delta_chars` | int | Length of body delta |
| `commit_sha` | string | Git commit SHA |
| `cursor_advanced_to` | ISO timestamp | New cursor value |

---

## 7. Absorb-judge events

Already exist (`memory.absorb.*`). Reproduced briefly for completeness:

| Event | When |
|---|---|
| `memory.absorb.judged` | A pair was judged (merge / keep_separate / unsure) |
| `memory.absorb.auto_merged` | The pair was merged |
| `memory.absorb.skipped` | Pair was skipped (quarantine, cross-type, etc.) |
| `memory.absorb.reverted` | A merge was undone (manual operator action) |

Schemas in `schema.py`. Not redefining here.

---

## 8. Search-failure events

### 8.1 `memory.search.failure`

NEW. Detailed in §14.5 of doc 03.

| Field | Type | Description |
|---|---|---|
| `component` | enum | `lancedb | fts5 | cross_encoder | watcher | disk` |
| `kind` | enum | `missing | corrupted | syntax | load_error | timeout | stale_entry` |
| `recoverable` | bool | Was the failure flagged recoverable? |
| `recovery_attempted` | bool | Did we try? |
| `recovery_succeeded` | bool | Did recovery work? |
| `recovery_duration_ms` | float \| null | Time spent in recovery |
| `degraded_to` | enum \| null | `lexical_only | vector_only | grep_only | no_rerank | null` |

---

## 9. Index events (indexer health)

### 9.1 `memory.index.write`

NEW. Emitted whenever the indexer re-derives a row.

| Field | Type | Description |
|---|---|---|
| `uri` | string | Item indexed |
| `trigger` | enum | `tool_write | watcher | manual_rebuild | dream_apply` |
| `targets` | list | `["lancedb", "fts5_unicode61", "fts5_trigram"]` |
| `duration_ms` | float | Total re-derivation time |
| `embedding_skipped` | bool | True if the item was already up-to-date (mtime check) |

### 9.2 `memory.index.rebuild`

NEW. Emitted by `durin reindex` command.

| Field | Type | Description |
|---|---|---|
| `entities_count` | int | Total .md files walked |
| `embedding_batches` | int | Number of embedding batches processed |
| `duration_ms` | float | Total rebuild time |
| `prior_index_existed` | bool | Was this a fresh build or rebuild over existing |

### 9.3 `memory.index.staleness_detected`

NEW. Emitted when a search-time staleness check finds a row out of date.

| Field | Type | Description |
|---|---|---|
| `uri` | string | The stale URI |
| `delta_seconds` | float | `mtime - indexed_at` |
| `action` | enum | `re_derived | filtered | queued` |

### 9.4 `memory.health_check`

Emitted by the background health-check cron on every tick — both passing and failing probes, so a dashboard can graph "uptime" of each component.

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
| `dream_llm_cost_per_day_usd` | sum of `llm_input_tokens × price + ...` | < $1.50 (alerting threshold) |
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

- Local file storage (typically `~/.durin/telemetry/*.jsonl`).
- Default rotation: keep 30 days. Older events compressed to `.jsonl.gz` and kept 1 year, then deleted.
- Operator can extend retention via config.
- Telemetry is NOT pushed to a remote server by default. If the operator opts in (settings), events ship to a configurable HTTPS endpoint.

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
| 8 | Retention | 30 days uncompressed + 1 year compressed. | §12.2 |
| 9 | Privacy defaults | Query truncated, URIs logged, content NEVER logged. | §13 |

### Open

None at the module level.

---

## 15. Implementation status

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| Memory event registry | 12 events in `schema.py` | + 7 new events (recall.lexical, recall.rerank, recall.rrf, search.failure, index.write, index.rebuild, index.staleness_detected, dream.entity_failed, dream.patch_applied) | Add TypedDicts + register |
| Cost in dream.end | Not present | Add `llm_input_tokens_total`, `llm_output_tokens_total`, optional `llm_cost_usd` | Wire LLM token counts into pass result |
| Privacy: query truncation | Not enforced | Truncate to 200 chars at emit time | Add helper in emit_tool_event |
| Privacy: URI hashing opt-in | Not present | Optional via config | New config flag |
| Alarms / dashboards | None | Internal threshold checks + optional Grafana export | Out of scope for memory subsystem; downstream |

---

## 16. Cross-references

- Tool calls that emit recall events: `04_agent_tools.md` §2-§5.
- Search pipeline failure mode + recovery (which emit `memory.search.failure`): `03_search_pipeline.md` §14.
- Dream entity failure + quarantine (which emit `memory.dream.entity_failed`): `05_dream_cold_path.md` §12.5.
- Indexer write triggers (which emit `memory.index.write`): `02_indexing.md` §6.
- Absorb-judge events already in production: `durin/telemetry/schema.py`.
