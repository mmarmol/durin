# Code audit — Tier 1 + Tier 2 hardening (May 2026)

> Comprehensive audit of the 15 commits that landed Tier 1 + Tier 2 harness
> improvements (OpenClaw + Hermes-inspired). Three parallel passes: **bugs &
> quality**, **telemetry coverage**, and **dead code & dangling wiring**.
>
> Final state at audit time: 1793 tests passing, 0 critical bugs, 0 dead
> code findings, telemetry coverage 10/12 features (2 gaps with concrete
> file:line fixes proposed).

---

## Executive summary

| Pass | Critical | Material | Minor | Notes |
|---|---|---|---|---|
| Bugs & quality | **0** | **1** (clarity) | 2 | Material item is a refactor opportunity in `post_compaction_guard.observe`, not incorrect behaviour. |
| Telemetry | — | **2 gaps** | 4 schema inconsistencies | 4 pre-existing tool families emit events undocumented in `ARCHITECTURE.md`. |
| Dead code | **0** | **0** | **0** | All new modules wired end-to-end. All config fields read by a consumer. `from __future__ import annotations` is intentional and is the only "unused" import flagged by static analysis (false positive). |

**Verdict**: ship-grade. No correctness or wiring blockers. The two telemetry gaps and the orphan-doc list below are tractable in one short PR. The bug-audit "material" item is a clarity refactor, not a fix.

---

## Pass 1 — Bugs & quality

### Critical (could cause incorrect behaviour)

**None.**

### Material (works but fragile / surprising)

- **[`durin/utils/post_compaction_guard.py:145-156`]** — `observe()` decrements `remaining_attempts` *before* counting matches. The semantics are correct (3 identical triples in a row → trip on the 3rd call), but the method conflates two counters into one body:
  - `remaining_attempts` is the "how many tool calls can I still watch" budget.
  - `matches` is the "how many of the watched calls were identical" detector.

  These could be separate concerns. As written, a reader has to trace the off-by-one between "decrement before" and "trip on `>=`" to convince themselves there isn't a bug. **Recommendation**: refactor `observe()` into two helpers (`_track_attempt()` + `_check_trip()`) OR add a docstring example walk-through of `window_size=3` with 3 identical triples to lock the semantic in the comments.

### Minor (improvable but not blocking)

- **[`durin/utils/tool_argument_repair.py:90`]** — `if 0 <= last_close < len(out) - 1:` — works correctly (we only want to strip trailing if there's at least one character after the last close), but a reader might initially expect `<= len(out) - 1`. The comment "Find the last JSON-closing character" could mention "and confirm there's trailing content to strip" to remove the moment of doubt.
- **[`durin/agent/memory.py` `maybe_consolidate_by_tokens`]** — `if estimated <= 0:` after an estimator exception merges two cases (empty session vs. estimation error) into one early-return. Both legitimately skip consolidation; distinguishing them in the debug log line ("estimator returned 0" vs. "session empty") would help future diagnosis.

### Verified clean (no findings)

- `durin/agent/runner.py` — all post-Tier 1/2 additions (`_mid_turn_precheck`, `_await_with_compaction_grace`, idle-timeout counter, unknown-tool guard, post-compaction wiring, turn-budget enforcement) integrate correctly with the existing iteration loop. Defensive `try/except` blocks around side-effect-prone callbacks (`is_compacting`, guard `observe`) prevent harness bugs from breaking turns.
- `durin/agent/context.py` — 3-tier prompt layering is clean: each `_build_*_layer` method returns a string that's empty-joinable, no state shared across the three calls.
- `durin/agent/loop.py` — `_is_compacting` lambda closure correctly captures `_session_key_for_compact` for the lifetime of the run.
- `durin/utils/history_image_prune.py` — completed-turn detection handles the edge case where a turn has only `tool` messages (no user/assistant) cleanly.
- `durin/utils/tool_result_validation.py` — per-block validation is idempotent and identity-preserving when no block needed modification.
- `durin/providers/openai_compat_provider.py` — `_resolve_parallel_tool_calls` correctly handles `tools=None` (skips injection so the API doesn't reject the request).
- `durin/heartbeat/service.py` — `heartbeat_session_key` defensive against `isolated=True` accidentally returning the shared key (12-hex suffix guarantees uniqueness).
- `durin/cli/commands.py::on_heartbeat_execute` — isolated-session cleanup is exception-safe (`try/except` around `delete_session`).

---

## Pass 2 — Telemetry coverage

### Inventory (full event list)

26 distinct event types emitted from `durin/`. Documented vs. undocumented in `docs/architecture/README.md`:

| Event | Source | Documented? |
|---|---|---|
| `agent_mode.turn_start` | `runner.py` | ✓ |
| `agent_mode.switch` | `command/builtin.py`, `tools/plan_mode.py` | ✓ |
| `agent_mode.tool_denied` | `runner.py` | ✓ |
| `cache.usage` | `progress_hook.py` | ✓ |
| `circuit_breaker.idle_timeout` | `runner.py` (Tier 1 2C) | ✓ |
| `compaction.grace_extended` | `runner.py` (Tier 1 2F) | ✓ |
| `compaction.lock_timeout` | `memory.py` (Tier 2 A3) | ✓ |
| `compaction.preemptive_trigger` | `memory.py` (Tier 2 A1) | ✓ |
| `mid_turn_precheck.overflow` | `runner.py` (Tier 2 A2) | ✓ |
| `plan_mode.presented` | `tools/plan_mode.py` | ✓ |
| `post_compaction_loop.tripped` | `runner.py` (Tier 2 C2) | ✓ |
| `provider.rate_limit` | `telemetry/logger.py` | ✓ |
| `provider.rate_limit_exhausted` | `telemetry/logger.py` | ✓ |
| `tool.edit_file` | `tools/filesystem.py` | ✓ |
| `tool.exec.spill` | `tools/shell.py` | ✓ |
| `tool.grep` | `tools/search.py` | ✓ |
| `tool.read_file` | `tools/filesystem.py` | ✓ |
| `tool.repo_overview` | `tools/repo_overview.py` | ✓ |
| `tool_call.argument_repair` | `utils/tool_argument_repair.py` (Tier 2 B1) | ✓ |
| `turn_budget.enforced` | `runner.py` (Tier 1 2H) | ✓ |
| `unknown_tool.loop_guard` | `runner.py` (Tier 2 B2) | ✓ |
| `ask_user.question_asked` | `tools/ask_user.py` | ✗ |
| `ask_vision.{start,error,end}` | `tools/interpret_image.py` | ✗ |
| `ask_audio.{start,error,end}` | `tools/interpret_audio.py` | ✗ |
| `sleep.{start,cancelled,end}` | `tools/sleep.py` | ✗ |

### Gaps — feature shipped, telemetry missing

These two recently-shipped features have NO emit site at all. Adding events is a one-liner each:

1. **Per-model `parallel_tool_calls` injection (Tier 1 2G)** — `openai_compat_provider.py:644`, inside `if resolved_parallel is not None and tools:`. Suggested event:
   ```
   provider.parallel_tool_calls_injected
     model, value (True/False), reason="model_override"
   ```
   *Justification*: zero visibility into which models the override is firing for. Without this, we can't validate that the config dict is doing useful work in production.

2. **History image / audio prune (Tier 2 B3)** — `runner.py` sanitize pipeline, right after `messages_for_model = prune_processed_history_images(...)`. Suggested event:
   ```
   history_media.pruned
     image_blocks_removed, audio_blocks_removed, preserve_turns, session_key
   ```
   Counts derivable by comparing input/output lengths. *Justification*: no way today to see how often the pruner saves real bytes vs. is a no-op. Affects whether 3 is the right default `preserve_turns`.

### Orphan events (emitted but undocumented)

Four tool families predate Tier 1/2 but were never added to the telemetry table in `ARCHITECTURE.md` §5. Fix is documentation-only:

- `ask_user.question_asked`
- `ask_vision.{start,error,end}`
- `ask_audio.{start,error,end}`
- `sleep.{start,cancelled,end}`

Add them under "Tool-level instrumentation" in `ARCHITECTURE.md`. No code change.

### Schema inconsistencies

| Field | Present in N events | Missing from | Recommendation |
|---|---|---|---|
| `session_key` | 8 loop-control events | All `tool.*`, `cache.usage`, `agent_mode.*`, `provider.*` | Add to `cache.usage` (already iteration-correlated) and to tool events when caller has a session. |
| `iteration` | 5 events | Tool events | Add to tool events — currently impossible to correlate a tool call to the LLM turn it came from without manual matching. |
| Units in field names | most | inconsistent — `chars` vs. `tokens` vs. `bytes` vs. `_s` | Standardise: `*_chars`, `*_tokens`, `*_bytes`, `*_s`, `*_ms`. Audit and rename. |

**Recommendation**: Create `durin/telemetry/schema.py` with `TypedDict` definitions for each event. Locks the payload contract, enables IDE autocomplete on `.log(...)`, and gives us a single place to add `session_key` / `iteration` as required base fields.

---

## Pass 3 — Dead code & dangling wiring

**Verified clean across all axes.**

Specifically confirmed (zero call-site failures across `durin/`):

- All 7 new utility modules (`tool_result_validation.py`, `tool_argument_repair.py`, `history_image_prune.py`, `post_compaction_guard.py`, plus pre-existing edits) are imported and consumed by the runner / providers / consolidator.
- All new `AgentRunSpec` fields (`is_compacting`, `post_compaction_guard`) are read inside `run()`.
- All new config fields (`AgentDefaults.preemptive_compact_ratio`, `AgentDefaults.parallel_tool_calls`, `HeartbeatConfig.isolated_sessions`, `ModelPresetConfig.preemptive_compact_ratio`) flow from `config.json` → `Config` → consumer.
- All new env vars (`DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS`, `DURIN_COMPACTION_GRACE_S`, `DURIN_TURN_BUDGET_CHARS`, `DURIN_COMPACTION_LOCK_TIMEOUT_S`, `DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS`, `DURIN_HISTORY_IMAGE_PRESERVE_TURNS`, `DURIN_POST_COMPACTION_GUARD_WINDOW`) have both a reader function in the implementation AND are documented in `ARCHITECTURE.md`.
- All telemetry event emit sites have matching documentation (with the 4 orphan-doc exceptions above, which are pre-existing).
- `ProviderSnapshot.preemptive_compact_ratio` (added in A1) is read by `Consolidator.set_provider` via `_apply_provider_snapshot`.
- Static unused-import check: only `from __future__ import annotations` flagged, which is intentional (PEP 563 / typing forward-reference behaviour).

No dead constants, no half-finished implementations, no commented-out paths, no imports of removed code.

---

## Recommended follow-ups (prioritised)

### P1 — Address before next major change

1. **Document the 4 orphan-event families** (`ask_user`, `ask_vision`, `ask_audio`, `sleep`) in `docs/architecture/README.md` §5. Documentation-only, ~10 minutes.
2. **Emit telemetry for Tier 1 2G (`parallel_tool_calls`) and Tier 2 B3 (history media prune)**. Two one-liners with one-line tests each. ~30 minutes.

### P2 — Quality of life

3. **Refactor `PostCompactionLoopGuard.observe()`** to separate the attempt counter from the match detector. Either as two private helpers or as an explicit docstring walk-through. ~20 minutes.
4. **Distinguish "estimator returned 0" from "session empty"** in the consolidator's idle-log line. ~5 minutes.

### P3 — Strategic

5. **Centralise telemetry schema**: `durin/telemetry/schema.py` with `TypedDict` per event. Forces `session_key` and `iteration` as common base fields, removes the schema-inconsistency findings in one stroke. Bigger refactor (~½ day) but unblocks reliable downstream consumers (dashboards, dream's history reader, etc.).

---

## Methodology

Audit performed in three parallel passes by `Explore` subagents, each given:
- Explicit scope (recently-touched files for passes 1 + 2; whole `durin/` package for pass 3).
- Explicit `IGNORE` list (style nits, type-annotation gaps, tests/, performance).
- Required output format (markdown with `file:line` citations, severity tiering).

Findings then spot-verified against the source (e.g., line numbers, claims about whether a callback is read, unused-imports manual check) before publication.

The agents collectively read every file modified in the 15 Tier 1 + Tier 2 commits, plus all consumers that needed cross-reference (loop.py, providers, telemetry/logger.py).
