---
title: Remaining work — actionable post-Phase-1.9
version: 1.0
status: living document
last_updated: 2026-05-28
audience: humans and LLMs picking up the work
depends_on: 09_implementation_roadmap.md (spec); 99_phase_progress_review.md (decisions log)
---

# Remaining work (post-Phase-1.9)

> **⚠️ Superseded (2026-06).** This pre-migration tracker is kept for history. The
> Phase-1.9 work shipped with the entity-centric migration; the **current** state,
> remediation record, and remaining items live in
> [`docs/backlog.md`](../../backlog.md). For how the system works **now**, read
> `00`-`08` (memory) + `docs/architecture/skills/` (skills) — not this list.

This document is the **granular list of what remains** after the current state (commit `c820447`). The original plan (`09_implementation_roadmap.md`) is still the **specs** reference, but uses a "deliverables" granularity that hid scope and latent bugs in the autonomous session. This doc applies the format discussed in D5+D6:

| Field | Meaning |
|---|---|
| **TYPE** | 🟢 module · 🟡 refactor · 🔴 integration · 🟣 test-migration · 📄 docs |
| **DoD** | Observable, not "implemented" but "X works end-to-end via Y" |
| **Refs** | Specific files + lines or modules to touch |
| **LOC** | Honest estimate (not "small/medium/large") |
| **Risk** | What can break / potential latent bugs |

**Per-phase ordering convention**: items are listed in **dependency** order, not priority. If X depends on Y, X comes after.

**State at close of second-pass audit (2026-05-28)**: ~5096 tests collected; tests/memory/ 1000 passed + 1 skipped (pre-existing). The v2 system runs end-to-end. Phase 4 (cross-encoder) shipped (P4.1-P4.4); Phase 8 (validation with LoCoMo bench) is still pending. Audit E35/E36 (2026-05-28) updated this header — the "4888 tests" count and the phrase "Phase 4 + Phase 8 remain" lagged behind the day's audit commits.

> **Audit refresh 2026-05-28** (audit B3): most of the P2-P7 items listed without ✅ DONE below **were closed during the day 2026-05-28** via the A1-A11 audit commits. The current per-item state with rationale is in the 2026-05 audit reconciliation (historical) (section for each A*). Quick summary by phase:
>
> - **Phase 2**: P2.2 ✅ (commit `c3eff1e`), P2.3 ✅ module + wiring in `989d33e` (A11), P2.4 ✅ module + wiring in `989d33e` (A11), **P2.5 reverted** in `7a835f8` (audit A4 — violates "filesystem is source of truth" principle).
> - **Phase 3**: P3.3 ✅ commit `bc55686`.
> - **Phase 4**: P4.1/P4.2/P4.3 ✅ commit `b3c50c6`. P4.4 ✅ commit `11d9f96`.
> - **Phase 5**: P5.1 ✅, P5.2 ✅ commit `2e7097a`, P5.3 ✅ commit `572d5cf`, P5.4 ✅ was a no-op (verified in B4 audit), P5.5 ✅ but **shipped as a pytest sync test** in `tests/memory/test_tool_description_sync.py` instead of the `scripts/audit_tool_descriptions.py` script the original plan proposed — divergence documented in B4, same objective achieved via CI test, P5.6 ✅ commit `2e7097a`.
> - **Phase 6**: P6.1/P6.2/P6.3 ✅ commit `572d5cf`. P6.4 was already ✅.
> - **Phase 7**: P7.1 was already ✅. P7.2 ✅ commit `2e7097a`. P7.3 (PushSink) ✅ wired end-to-end in `b822b75` (A8) with secret store + config + tests.
> - **Phase 8**: pending (validation with LoCoMo bench, etc.).
>
> The individual items below keep their description for historical reference; sections with ✅ DONE inline are up-to-date. When there is divergence between the original plan and what was actually shipped, doc 11 documents the reason.

---

## Phase 2 — Indexing v2 (4 pending items)

### P2.1 — Re-index-on-write hooks ✅ DONE (commit `1ea70ac`)
Documented here for historical reference. Hooks in `memory_store.execute`, `memory_ingest.execute`, `DreamConsolidator.apply`.

### P2.2 — Schema-version startup check ✅ DONE (commit `c3eff1e`)

- **TYPE**: 🔴 integration
- **DoD**: When `<workspace>/.durin/index/meta.json::schema_version != CURRENT_SCHEMA_VERSION` or the file doesn't exist, the next call to `MemorySearchTool.execute` (or equivalent) triggers `rebuild_fts_index` + `VectorIndex.rebuild_from_workspace` automatically and emits `memory.index.rebuild` with `reason="schema_mismatch"`.
- **Refs**:
  - Read: `durin/memory/index_meta.py::load_index_meta` (already exists).
  - Comparison: `durin/memory/index_meta.py::CURRENT_SCHEMA_VERSION` (= 2).
  - Hook point: `durin/agent/tools/memory_search.py::MemorySearchTool.execute` line ~256 (before calling `run_search_pipeline`) — check meta + rebuild if stale.
  - Cleaner alternative: hook in `MemorySearchTool.__init__` to do it once per process.
- **LOC**: ~30 (`_ensure_index_fresh()` helper + call sites + 1 test).
- **Risk**: if the rebuild takes long (>10s for large workspaces), it blocks the first `memory_search` post-update. Mitigation: emit progress log + consider a lock so two processes don't rebuild concurrently.
- **Test**: set `meta.json` with `schema_version=1`, call `memory_search`, assert rebuild ran + meta updated.

### P2.3 — Watchdog file watcher 🔨 PARTIAL (was wrongly ✅; corrected 2026-06-06 by the completeness re-audit)

> **Correction (N1/N2):** the DoD claimed full credit but two halves were never
> implemented. (a) **`author: user` commit** — the watcher only ever called
> `reindex_one_file`; it never committed. NOW provided by the **human-edit guard**
> (`memory_writer._commit_dirty_as_user`, N1, 2026-06-06): the next system write
> commits any dirty hand edit with `author:user` before its hard-reset ff, so the
> edit isn't clobbered. (b) **vector re-index of a hand edit** — `reindex_one_file`
> is FTS-only; the watcher never re-embeds. Tracked as **N2**
> (now ✅ resolved). DoD's "within 5 s a `memory_search` surfaces the
> edit" holds for FTS, NOT for vector recall, until N2 lands.

- **TYPE**: 🟢 module (~120 LOC)
- **DoD**: Edit `memory/entities/person/marcelo.md` with vim and, within 5 seconds, the next `memory_search` for "marcelo" surfaces the words from the edit. Additionally: the commit in `memory/.git/` is recorded with `author: user`.
- **Refs**:
  - New module: `durin/memory/file_watcher.py`.
  - Dependency: `watchdog` (pip install). Add to `pyproject.toml`.
  - Watch path: `<workspace>/memory/` excluding `archive/` and `pending/`.
  - On each mtime event → `reindex_one_file(workspace, path)` + git commit with `author=user`.
  - Lifecycle: start in `durin/agent/loop.py::AgentLoop.start` or in CLI `durin agent`.
- **LOC**: ~120 (watcher + lifecycle hook + 4 tests).
- **Risk**: watchdog on macOS uses FSEvents (built-in); on Linux uses inotify (fine); on Docker / network FS may fail — `watchdog` falls back to polling automatically but it needs verification. Doc 02 §6.3 confirms this mitigation.
- **Test**: `tmp_path` + touch file + wait for event + assert FTS index sees it.

### P2.4 — Health-check cron ✅ DONE (module: `022d4b1`; scheduler + lifecycle wiring in `989d33e` / audit A11)

- **TYPE**: 🟢 module (~150 LOC, per spec §5.1)
- **DoD**: Every 15 minutes (configurable), a background job:
  1. Calls `detect_index_staleness(workspace)` and for each drift logs + fires `reindex_one_file`.
  2. Probes LanceDB connect (best-effort): if it fails, log + queue rebuild.
  3. Emits `memory.health_check` event with `{components: {fts: "ok", lance: "degraded", ...}, drift_count: N}`.
  4. After 3 consecutive failures in the same component within 1h, emits `memory.health.critical` and pauses the component.
- **Refs**:
  - New: `durin/memory/health_check.py`.
  - Reuse `detect_index_staleness` (already exists in `indexer.py`).
  - Scheduler: `croniter` (already in deps) + a daemon thread or asyncio task.
  - Telemetry: 2 new events in `durin/telemetry/schema.py` (`memory.health_check`, `memory.health.critical`).
  - Config: new `memory.health_check.{enabled, interval_seconds}` in `durin/config/schema.py`.
- **LOC**: ~150 (module ~80 + 2 telemetry events ~30 + config ~10 + tests ~30).
- **Risk**: cron inside the agent process vs separate cron. Doc 02 suggests in-process (one thread). That means `durin agent` must be running for the health-check to occur — agent shut down = no probe.
- **Test**: simulate LanceDB failure with monkeypatch + verify emit + verify pause after 3 failures.

### P2.5 — LanceDB body column extension ❌ REVERTED (audit A4, commit `7a835f8`) — violated "filesystem is source of truth"

- **TYPE**: 🟡 refactor (~50 LOC)
- **DoD**: `level="cold"` queries return the body without touching disk. `VectorIndex.rebuild_from_workspace` populates the new column. Existing table is migrated (forced rebuild on schema_version bump — see P2.2).
- **Refs**:
  - Schema in `durin/memory/vector_index.py::_record_for` and `_record_for_entity_page`.
  - Add column `body: str` to the record dict.
  - In `memory_search._sectioned_to_result` for level=cold: read `body` from vector_meta instead of calling `_enrich_body` which reads disk.
- **LOC**: ~50 (schema change + reads + test).
- **Risk**: existing table requires rebuild (can't ALTER in LanceDB without recreating). Mitigation: bump `CURRENT_SCHEMA_VERSION` and let P2.2 trigger it.
- **Test**: store an entry + reindex + query level=cold + assert body present without having read the .md.

---

## Phase 3 — Search pipeline v2 (1 pending item)

### P3.1 — Entity-aware rerank wiring ✅ DONE (commit `1ea70ac`)
### P3.2 — Grep fallback wired ✅ DONE (commit `1ea70ac`)
### P3.3 — Intent router pattern detection ✅ DONE (commit `bc55686`)

- **TYPE**: 🟢 module (~80 LOC)
- **DoD**: Query "mmarmol@mxhero.com" (email pattern) → search_pipeline detects the pattern + boosts lexical weight to 2.5 even if the agent did NOT pass `keywords`. Same for queries that look like URLs (`https://...`), UUIDs, file paths.
- **Refs**:
  - Extend `durin/memory/query_router.py::decide_lexical_route` to also return `auto_keywords: str | None`.
  - Patterns: email regex, URL regex, UUID regex, file path regex (all in a constant `_IDENTIFIER_PATTERNS`).
  - In `search_pipeline.run_search_pipeline`: if `auto_keywords` and `keywords is None`, treat as `keywords_provided=True`.
- **LOC**: ~80 (router extension + 6 tests for each pattern + integration).
- **Risk**: false positives (e.g. "v1.2.3" matches version pattern but the user wants semantic). Mitigation: only patterns with clear identifiers (email/URL/UUID/path); no version strings, no bare numbers.
- **Test**: query "find marcelo@mxhero.com" → routing decision has `auto_keywords="marcelo@mxhero.com"`. Query "marcelo in spain" → `auto_keywords=None`.

---

## Phase 4 — Cross-encoder opt-in (todo) — total ~250 LOC

### P4.1 — Cross-encoder runner module ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🟢 module (~100 LOC)
- **DoD**: `CrossEncoderReranker(model_id).score(query, [doc_text, ...]) -> [scores]`. Lazy-loads model on first call. Batches inputs of N=32. Graceful degradation: model load failure → log + return None (caller skips).
- **Refs**:
  - New: `durin/memory/cross_encoder.py`.
  - Dependency: `sentence-transformers` or `FlagEmbedding` (verify). Optional dep via `durin[cross-encoder]` extra.
  - Default model: `BAAI/bge-reranker-base` (~100M, MIT, lower RAM) per doc 03 §9.1; `jinaai/jina-reranker-v2-base-multilingual` remains a curated alternative.
- **LOC**: ~100 (module + lazy load + batching + 5 tests).
- **Risk**: model download on first invocation (bge-reranker-base is ~100M, far smaller than jina's ~1.1GB). Mitigation: progress log + consider pre-download in onboarding (P6.1).
- **Test**: mock model with stub `score(query, docs) → [random]`; verify batching of 32; verify failure handling.

### P4.2 — Integration in search_pipeline step 5 ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🔴 integration (~30 LOC)
- **DoD**: When `memory.search.cross_encoder.enabled=true` and `pipeline_result.hits` is non-empty, `run_search_pipeline` invokes the reranker with (query, [hit.snippet+body for hit in top 50]) and reorders. Hits ranked > 10 dropped. Emit `memory.recall.rerank`.
- **Refs**:
  - Hook point: `durin/memory/search_pipeline.py::run_search_pipeline` after `_entity_aware_rerank`, before `apply_per_source_cap`.
  - Config: `memory.search.cross_encoder.{enabled, model, batch_size}` in `durin/config/schema.py`.
  - Telemetry: new `memory.recall.rerank` event in schema.
- **LOC**: ~30 + 3 tests.
- **Risk**: latency. Mitigation: spec says OFF by default; user must opt in explicitly.
- **Test**: with cross_encoder enabled + stub model, verify re-ranking applied.

### P4.3 — Onboarding question (doc 06 §6.2) ✅ DONE (commit `b3c50c6`)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: `durin/cli/onboard_memory.py::prompt_enable_cross_encoder(current: bool) -> dict` with verbatim text from doc 06 §6.2. Returns `{"enabled": bool, "model": str}`.
- **Refs**:
  - Existing pattern: `prompt_enable_auto_absorb` in `onboard_memory.py`.
- **LOC**: ~30 + 4 tests.

### P4.4 — Web dashboard memory settings panel ✅ DONE

- **TYPE**: 🟡 refactor webui (~250 LOC component + ~50 i18n)
- **DoD**: "Memory" section added to the Settings nav with three observable blocks:
  1. Cross-encoder toggle (`memory.search.cross_encoder.enabled`) + dropdown of 4 curated models.
  2. Number input for `memory.dream.threshold_entries` (commit on Enter or Save button).
  3. Read-only summary of the `CLASS_HALF_LIFE_DEFAULTS` defaults (not configurable; they live in code).
- **Refs**:
  - New: `webui/src/components/settings/MemorySettings.tsx`.
  - Nav wiring: `webui/src/components/settings/SettingsView.tsx` (SETTINGS_NAV_ITEMS + render branch).
  - i18n: `webui/src/i18n/locales/en/common.json` (full memory namespace); `es/common.json` (only nav.memory).
  - Backend: reuses existing `/api/config` and `/api/config/set` in `durin/channels/websocket.py`.
- **Verification**: `npx tsc --noEmit` passes; `npx vitest run` 142 tests pass; `npm run build` produces dist (vite build 1.89s).
- **Manual test pending**: `npm run dev` + open Settings → Memory, toggle cross-encoder, change model, adjust threshold, verify `~/.durin/config.json` changes.
- **Assumed risk**: the cross-encoder dropdown is disabled when the toggle is OFF — deliberately conservative UX to avoid configuring a model that won't be used.

---

## Phase 5 — Tools v2 (4 pending items, ~180 LOC)

### P5.1 — `memory_search` keywords ✅ DONE (commit `c820447` / D6)
### P5.2 — `memory_search` `recovered_from` + `recovery_duration_ms` fields ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟡 refactor (~40 LOC)
- **DoD**: When `run_search_pipeline` triggers a recovery (e.g. lance index recreated on the fly), the tool response dict includes `"recovered_from": ["lance"]` and `"recovery_duration_ms": <float>`. When there is no recovery, the fields don't appear.
- **Refs**:
  - `SearchPipelineResult` needs to carry `recovered_from: list[str]` and `recovery_duration_ms: float`.
  - `run_search_pipeline` populates them based on whether the safe wrappers had to fail+recover.
  - `memory_search._sectioned_to_result` passes them to the final dict.
- **LOC**: ~40 + 2 tests.
- **Depends on**: nothing — can go before or after P2.4 (health-check).

### P5.3 — `memory_ingest` recursive character splitter ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟡 refactor (~60 LOC)
- **DoD**: `memory_ingest` with a 50-page PDF generates N chunks of ~1500 chars with ~200 chars overlap, preferring cuts at paragraph > line > sentence > word > char. Verifiable via test: feed 10000-char text, verify chunk count + overlaps + boundary preference.
- **Refs**:
  - Replace current logic in `durin/agent/tools/memory_ingest.py` (line ~150-200, `_chunk_content` section or similar).
  - Pattern: LangChain RecursiveCharacterTextSplitter (conceptual reference, do NOT copy).
- **LOC**: ~60 (splitter ~40 + tests ~20).
- **Risk**: splitter changes can move existing chunks in production → re-ingest necessary. Mitigation: only applies to new ingests; existing corpora are not re-processed automatically.
- **Test**: 10k-char text with well-defined paragraphs; verify cuts at paragraph boundaries; verify overlap.

### P5.4 — `memory_drill` remove `include_context` flag ✅ DONE (no-op — verified the flag never existed; B4 audit)

- **TYPE**: 🟡 refactor (~10 LOC)
- **DoD**: The tool description in doc 06 §3.4 does not mention `include_context`. The tool in `durin/agent/tools/memory_drill.py` has it; remove from schema + execute signature.
- **Refs**:
  - `durin/agent/tools/memory_drill.py` — find `include_context` in `_PARAMETERS` and `execute`.
- **LOC**: ~10 (delete code + update test mocks).
- **Risk**: tests passing `include_context` → fail. Update them.

### P5.5 — Tool description audit script ✅ DONE differently (B4 divergence: shipped as `tests/memory/test_tool_description_sync.py` instead of `scripts/audit_tool_descriptions.py` — same outcome via CI test, simpler integration)

- **TYPE**: 🟢 module + CI (~50 LOC)
- **DoD**: Script `scripts/audit_tool_descriptions.py` extracts the descriptions of the 4 memory tools + the Memory block from `identity.md`; compares against the canonical strings in doc 06 §3 and §2; fails with specific diff if they differ. Wired in CI.
- **Refs**:
  - New: `scripts/audit_tool_descriptions.py`.
  - Parsing: doc 06 markdown — extract code blocks under `## 3.1`, `## 3.2`, etc.
  - Compare with `<tool>._PARAMETERS.description` and `<tool>.description` properties.
  - Test that exercises the script + CI step in `.github/workflows/`.
- **LOC**: ~50 (script + 1 test + 1 CI step).
- **Risk**: spec drift is inevitable — the spec evolves. Mitigation: the script shows a precise DIFF, the dev decides whether to update spec or code.

### P5.6 — Re-test the 3 skipped in commit `c820447` ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟣 test-migration (~50 LOC)
- **DoD**: The 3 tests skipped in `test_phase2_smoke.py` and `test_t1_wiring_e2e.py` (mentioned in commit message) are rewritten against the v2 surface: assertions about results (which hits emerge first) instead of internal details (which function was called).
- **Refs**:
  - `tests/memory/test_phase2_smoke.py::test_recall_vector_telemetry_fires` — verify v2 payload with real `hit_count`.
  - `tests/memory/test_phase2_smoke.py::test_vector_recall_does_not_regress_against_grep` — compare v2 strategy vs grep-only.
  - `tests/memory/test_t1_wiring_e2e.py::test_e2e1_memory_search_invokes_entity_aware_ranker` — verify telemetry `ranking="entity_aware"` post-search.
- **LOC**: ~50 (3 tests rewritten).
- **Risk**: requires running fastembed+lancedb locally (not in CI). Mitigation: explicit `@pytest.mark.local_only` marker.

---

## Phase 6 — Prompts v2 (4 pending items, ~100 LOC)

> **Wiring note (corrected 2026-06-01).** P4.3 / P6.1 / P6.2 are DONE, but
> they were planned to wire into `onboard.py::run_onboard`. The actual
> wiring landed in the default task-oriented wizard
> `durin/cli/onboard_wizard.py::_configure_memory` (P10, 2026-05-30) — which
> calls the `onboard_memory.py` prompt functions. `onboard.py` is the legacy
> `--advanced` field-walker, not the default path.

### P6.1 — Onboarding wizard: memory subsystem enable ✅ DONE (commit `572d5cf`, rewired P10)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: enable vector memory during onboarding. **Shipped as a toggle**, not the originally-planned `prompt_enable_memory_subsystem` confirm: `onboard_wizard.py::_configure_memory` shows an "Enable vector memory" choice (ON by default since 2026-06-01). The standalone `prompt_enable_memory_subsystem` + its `MEMORY_ENABLE_QUESTION_TEXT` were never wired and were removed as dead code (the toggle is the live path).
- **Refs**:
  - `durin/cli/onboard_wizard.py::_configure_memory` (the toggle + submenu).

### P6.2 — Onboarding: aux model for memory (doc 06 §6.4) ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟢 module (~30 LOC)
- **DoD**: `prompt_memory_aux_model(current_agent_model: str, current: str | None) -> str` offers "same / specify / skip". Sets `config.aux_models.memory`.
- **Refs**:
  - `durin/cli/onboard_memory.py::prompt_memory_aux_model`, called from `onboard_wizard.py::_configure_memory`.

### P6.3 — Tool description constants per doc 06 §3 ✅ DONE (commit `572d5cf`)

- **TYPE**: 🟡 refactor + 📄 docs sync (~50 LOC)
- **DoD**: The descriptions of `memory_search`, `memory_store`, `memory_ingest`, `memory_drill` in code match VERBATIM with doc 06 §3.1-§3.4. Verification is automatic via P5.5.
- **Refs**:
  - `durin/agent/tools/memory_search.py::_PARAMETERS`.
  - `durin/agent/tools/memory_store.py::_PARAMETERS`.
  - `durin/agent/tools/memory_ingest.py::_PARAMETERS`.
  - `durin/agent/tools/memory_drill.py::_PARAMETERS`.
- **LOC**: ~50 (text updates across the 4 tools).
- **Risk**: the spec may have wording that is worse for the LLM in practice. Mitigation: if bench shows regression (-5pp on LoCoMo), revert + adjust spec.

### P6.4 — `identity.md` ✅ DONE (commit `2bdafec`)

---

## Phase 7 — Telemetry v2 (3 pending items)

### P7.1 — Privacy truncation ✅ DONE (commit `2bdafec`)
### P7.2 — Retention / log rotation ✅ DONE (commit `2e7097a`)

- **TYPE**: 🟢 module (~80 LOC)
- **DoD**: Telemetry JSONL files > 30 days old get compressed to `.jsonl.gz`. Archives > 90 days are deleted. Job runs daily via the health-check cron (P2.4).
- **Refs**:
  - New: `durin/telemetry/retention.py`.
  - Walk `~/.cache/durin/telemetry/*.jsonl` (per `durin/memory/stats.py::DEFAULT_TELEMETRY_DIR`).
  - Hook into P2.4 health-check tick.
- **LOC**: ~80 (module + tests).

### P7.3 — HTTPS push opt-in ✅ DONE (PushSink: `2e7097a`; end-to-end wiring incl. secret store + config + tests: `b822b75` / audit A8)

- **TYPE**: 🟢 module (~120 LOC)
- **DoD**: When `telemetry.push_url` is set, events are additionally sent to the endpoint via POST batches (every 10 events or every 60s). Authentication via `telemetry.push_token`.
- **Refs**:
  - New: `durin/telemetry/push.py` (httpx async client).
  - Config: `telemetry.push_url`, `telemetry.push_token`, `telemetry.push_batch_size`.
- **LOC**: ~120 + 5 tests.
- **Risk**: privacy — verify P7.1 truncation is already applied before push.

---

## Phase 8 — Validation (todo) — wall-clock heavy

### P8.1 — LoCoMo bench run with v2 pipeline

- **TYPE**: 🔴 integration + 📄 report
- **DoD**: Run `scripts/benchmark/locomo_run.py` (exists) with per_category=25 → result documented. Bar: ≥ 64.7% (previous v2 baseline) without cross-encoder; ≥ 70% with cross-encoder.
- **Refs**:
  - Script: `scripts/benchmark/locomo_run.py`.
  - Results: appended in doc 28.
- **Wall-clock**: ~90 min per run.
- **Risk**: regression vs 64.7%. Mitigation: bench failure_breakdown per category (doc 28 §4) localizes what broke.

### P8.2 — Adversarial generalist sets (4 domains)

- **TYPE**: 📄 docs + tests (~300 QAs)
- **DoD**: 4 JSON files with 50 QAs each in `bench-results/adversarial/`: coder, sales, support, personal-assistant. Bar: ≥ 50% per domain.
- **Refs**:
  - New: `bench-results/adversarial/{coder,sales,support,assistant}.json` (50 QAs each).
  - Runner: extend `locomo_run.py` or create `adversarial_run.py`.
- **Wall-clock**: high (writing 200 QAs + running them).
- **Risk**: 50 quality QAs per domain takes hours. Consider LLM-assisted generation (human-verified oracle answers).

### P8.3 — 7-day soak test

- **TYPE**: 🔴 integration (script + observation)
- **DoD**: Script that simulates daily user activity for 7 days (cron) + verifies: Dream cost in $0.25-$1.50/day, no unjustified quarantines, index growth tracks workspace size, no silent retrieval misses.
- **Refs**:
  - New: `scripts/soak/run_soak.sh` + `scripts/soak/analyze.py`.
  - Metrics: read from `~/.cache/durin/telemetry/*.jsonl`.
- **Wall-clock**: 7 real days.

### P8.4 — Documentation lint pass

- **TYPE**: 📄 docs
- **DoD**: `grep -rn "(pending)" docs/architecture/memory/` returns nothing. All decisions marked with a resolution. Spec↔code discrepancies detected by P5.5 fixed.
- **LOC**: ~varies (depends on how much doc debt there is).

---

## Summary and recommended sequencing

**Block A — refinements + safety nets (~250 LOC)**:
- P2.2 schema-version check (small, high value)
- P5.4 memory_drill cleanup (10 LOC)
- P5.6 re-enable the 3 skipped tests (50 LOC)
- P6.3 tool descriptions sync (50 LOC)
- P5.5 audit script (50 LOC)

**Block B — Phase 4 cross-encoder (~250 LOC)**:
- P4.1 module → P4.2 integration → P4.3 onboarding → P4.4 webui

**Block C — operational (~350 LOC)**:
- P2.3 watcher + P2.4 cron + P7.2 retention + P7.3 push

**Block D — validation (wall-clock heavy)**:
- P8.1 bench → P8.2 adversarial → P8.3 soak → P8.4 docs lint

**Recommended**: Block A first (small, closes debts), then B (biggest unlock), then C (operational hardening), then D (final validation).

---

## How this format prevents the problems of the autonomous session

| Previous problem | How this format prevents it |
|---|---|
| "Ship cores, defer integration" | Each item has an **explicit TYPE** (module vs integration). You can't pass off integration as module. |
| Bullets of apparently uniform size | **LOC estimate** shows real disparity. P5.4 (10 LOC) vs P2.4 (150 LOC) are obviously different. |
| Vague DoD ("implemented") | **Observable DoD** ("file edited with vim appears in next memory_search"). Impossible to self-deceive. |
| No spec→code traceability | **Refs** point to files+lines. The spec→work translation is explicit. |
| Latent bugs packaged as "operational" | **Risk** spells out what can break. If absent, the item is not well analyzed. |

**Maintenance**: update this doc when each item closes. Each relevant commit should reference the item ID (P2.2, P4.1, etc.) in the commit message body.
