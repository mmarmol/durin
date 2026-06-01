# Phase 0 → Phase 7 progress + decisions for review

**Authorship:** Claude (autonomous session 2026-05-28) + audit with human (same day).
**Branch:** `memory/phase-0-foundations`.
**State:** 2314+ tests passing, 1 skipped (conditional), 0 failing. Webui builds cleanly.

> **Audit refresh 2026-05-28** (audit B2): this doc was written as a pre-audit snapshot at ~02:00am. During the day audit A1-A11 + Block B were executed against the code and the docs; many decisions marked here as "deferred" or "next step" were executed. The current state lives in `11_audit_reconciliation.md` (more recent, granular per item) and the day's commits. This doc is kept as historical reference — read it as "what was known when written", not "what is now".

This original document lists:
1. What was completed (pre-audit snapshot).
2. Decisions taken without asking (with justification).
3. Deferred work (with reason + estimated scope).
4. Recommended next step.

---

## 1. Completed phases

### Phase 0 — Foundations (100%)
- Centralized `walk_memory` / `walk_class` walker, all callers migrated.
- `archive_episodic` / `archive_entity` helpers, top-level `memory/archive/<class>/` layout.
- `slugify_name` + `resolve_slug_collision` (NFC + unidecode + truncate 64).
- `EntityPage` v2: `attributes`, `relations`, `provenance` as typed fields.
- ~~`MemoryEntry`: `decay_half_life`, `evergreen` + `decay.py` with `half_life_for` and `CLASS_HALF_LIFE_DEFAULTS`.~~ Removed 2026-05-30 (doc 03 §10).
- `index_meta.py`: `<workspace>/.durin/index/meta.json` with `schema_version=2` + `embedding_model_id` + atomic save.

### Phase 1 — Dream v2 (modules d1-d12 all shipped as isolated units)
- `consolidator.md` rewritten to v2 spec + 6 examples (`01_new_entity` … `06_no_changes`).
- `rules.md`, `commit_format.md`, `json_patch_reference.md`.
- `dream_patch_parser.py` — splits `===PATCH=== / ===BODY_DELTA=== / ===COMMIT=== / ===END===` with `json_repair`.
- `dream_prompt_builder.py` — package assembler with slot substitution + truncation to 100 URIs.
- `dream_apply.py` — apply pipeline with jsonpatch, `.md.bak` rollback, telemetry
  `memory.dream.patch_applied` / `memory.dream.entity_failed`.
- `dream_archive_consumed.py` — moves consumed episodics to archive + deletes LanceDB row.
- `dream_quarantine.py` — structural failure counter + 7-day quarantine.
- `dream_commit_message.py` — finalizes the commit with canonical trailers.
- `dream_git_history.py` — formatter for the `{recent_history}` slot.
- `user_authored` filter in `_discover_pending_consolidations` + 5 test files updated with `_agent_created_scope` autouse.
- `absorb_judge` template + parser locked via test contract.
- `onboard_memory.prompt_enable_auto_absorb` with Q6.3 text verbatim from the spec.

### Phase 1.5 — Hot layer (100% via parallel worktree agent)
- `hot_layer.py` budgets / markers / cursor logic verified against spec.
- v2 rendering: `attributes` and `relations` as prose inside the CANONICAL block.
- Per-section failure handling + `memory.hot_layer.failure` telemetry.

### Phase 2 — Indexing v2 (core, missing watcher + cron + tool hooks)
- `fts_index.py` — sqlite FTS5 with dual table (`memory_fts` unicode61 + `memory_fts_trigram`) + `fts_meta` bookkeeping.
- `indexer.py` — `rebuild_fts_index` (bulk) + `reindex_one_file` (incremental) + `detect_index_staleness` (drift).
- `durin memory reindex [--target fts|lancedb|all]` CLI.
- 3 telemetry events: `memory.index.write`, `memory.index.rebuild`, `memory.index.staleness_detected`.

### Phase 3 — Search pipeline v2 (core, missing entity-aware rerank wiring + cross-encoder)
- `query_router.py` — CJK detection + NFC + routing to `UNICODE61` / `TRIGRAM` / `LIKE_SUBSTRING`.
- `rrf_fusion.py` — RRF k=60 cross-source + dynamic boost (`w_lexical = 2.5` when `keywords_provided`).
- `lexical_search.py` — executor connecting router → FTS index with quoting + emit.
- `sectioned_output.py` — rendering with markers `=== CANONICAL / FRAGMENT / SESSION / INGESTED ===` + per-source cap (default 3 corpus chunks/ingest_id).
- `search_pipeline.py` — end-to-end orchestrator with graceful degradation.
- 2 telemetry events: `memory.recall.lexical`, `memory.recall.rrf`.

### Phase 5 — Tools v2 (partial: only `keywords` param)
- `memory_search` now accepts `keywords` (description matches doc 04 §2.3 + doc 06 §3.1).

### Phase 6 — Prompts v2 (partial: identity.md)
- `identity.md::Memory` section migrated to v2 text verbatim from doc 06 §2.
- `tests/memory/test_identity_memory_section.py` locks spec-anchor phrases against drift.

### Phase 7 — Telemetry v2 (partial: privacy truncation)
- `emit_tool_event` truncates free-text fields (`query` / `text` / `snippet` / `content` / `needle`) to 200 chars per doc 07 §13.

---

## 2. Decisions taken without asking

> If you think any are wrong, tell me and I'll revert.

### D1 — absorb_judge vocabulary (Phase 1 d11)
**Conflict:** doc 06 §5 spec said `merge | keep_separate | unsure`. The code (`absorb_judge.py:_VALID_VERDICTS`) uses `same | different | unclear`. The `absorb_judge.md` template also uses `same/different/unclear`.

**Decision:** I updated doc 06 §5 + doc 05 §8.4-8.6 to match the code.

**Justification:** The `same/different/unclear` vocabulary is **identity-judgement**, not action-prescription. Cleaner separation: LLM judges identity; runner maps verdict+confidence to the action (merge / log / defer). Changing the code + the template + retraining the LLM was the bigger cost, vs editing the doc which already warned "current implementation is solid; this doc doesn't redefine it".

**Locking test:** `tests/memory/test_absorb_judge_template_contract.py`.

### D2 — `user_authored` filter broke 23 existing tests (Phase 1 d9)
**Cause:** The filter (correct, per spec doc 01 §4.6.1) skips entries with `author=user_authored`. The default for `MemoryEntry.author` is `user_authored` (appropriate for manual human edits). Existing tests created entries with the default and expected Dream to see them.

**Decision:** Added autouse fixture `_agent_created_scope` in 6 test modules (test_dream_runner, test_dream_triggers_beta2, test_threshold_trigger, test_threshold_trigger_e2e, test_t1_wiring_e2e, test_memory_cmd) that wraps test bodies in `author_scope("agent_created")`.

**Justification:** In production, the agent's tool calls run under `author_scope("agent_created")`. The tests modelled agent observations; the wrap is semantically correct. The alternative (changing the default of `MemoryEntry.author` to `agent_created`) would have broken the protection that the spec wants.

### D3 — Phase 1.5 launched in isolated worktree
I initially launched the Phase 1.5 agent in the main workspace. Then I identified that both of us would touch `durin/telemetry/schema.py` and killed it + relaunched with `isolation: "worktree"`. The branch (`memory-phase-1.5-hot-layer`) was merged cleanly, worktree removed, branch deleted.

### D4 — Phase 1.9 deferred (v2 pipeline integration in DreamConsolidator)
**Current state:** All v2 modules (parser, builder, applier, archive, quarantine, commit, git_history) are built and tested in isolation. The old `consolidate_entity` still uses the legacy `===PAGE===` parser and the full-page-rewrite flow.

**Decision:** I did not do the wholesale integration.

**Reason:** The refactor touches `DreamConsolidator.consolidate_entity` + `apply` + migration of ~12 test stubs that return `===PAGE===` format. I estimate 200-400 LOC + careful test migration. I left it as **Phase 1.9** so you decide whether we do it together in a focused session or in chunks.

**Risk:** While this is not wired, the v2 `consolidator.md` is passed to the LLM BUT the response is attempted to be parsed as v1 → all real Dream runs would fail (the LLM stub in tests returns v1 format, so the suite doesn't catch this bug). **I recommend not running a real Dream pass until this is wired**.

### D5 — Phase 2/3 scope deliberately bounded
Phase 2 watchdog file watcher, health-check cron, tool re-index hooks, and schema-version startup check are NOT implemented. Phase 3 entity-aware rerank is not wired downstream of the orchestrator.

**Reason:** Time + context + the critical path (search pipeline working) is already covered by the core modules. The deferred items are operational / opt-in.

### D6 — `keywords` param added but not yet wired into the search path
The `keywords` parameter is exposed on `memory_search`, parsed in `execute`, but **NOT passed to the lexical search** (because the current memory_search's lexical search goes through grep + LanceDB, not `run_search_pipeline`). When the tool is migrated to `run_search_pipeline`, the full thread connects automatically via `keywords_provided=True` in `fuse_rrf`.

---

## 3. Pending work (with estimated scope)

### Phase 1.9 — Wire v2 pipeline in DreamConsolidator
- Refactor `consolidate_entity` to parse with `parse_dream_output`.
- Refactor `apply` to use `apply_dream_output` + `archive_consumed_episodic` + `record_failure`/`clear_failures` + `finalize_commit_message`.
- Migrate `_well_formed_response()` and ~12 dream tests to v2 format.
- Scale: ~200-400 LOC + careful test migration.

### Phase 2 — remaining
- File watcher (`watchdog`) — operational, manual edits are rare today. ~100 LOC.
- Health-check cron — operational, restores corrupted indices every 15min. ~150 LOC.
- Re-index-on-write hooks in `memory_store` / `memory_ingest` / Dream apply — needed for FTS to stay current without running `durin reindex`. ~30 LOC + tests.
- Schema-version mismatch check on startup. Has infrastructure (`index_meta.py`); missing hook. ~20 LOC.
- LanceDB body column extension (doc 02 §5.1) — optional, for search level=cold without disk reads. ~50 LOC.

### Phase 3 — remaining
- Entity-aware rerank wiring downstream of the orchestrator (the `entity_ranker.py` module exists; only the call is missing). ~30 LOC.
- Intent router for email/URL/ID patterns. ~50 LOC.
- Grep fallback wired to the orchestrator. ~30 LOC.

### Phase 4 — Cross-encoder (todo)
- `cross_encoder.py` module with lazy load.
- Integration in `search_pipeline.py` step 5.
- Config `memory.search.cross_encoder.*`.
- Webui dashboard panel.
- ~200-300 LOC.

### Phase 5 — Tools v2 remaining
- Wire `memory_search` to `run_search_pipeline` (replace old grep+vector path or coexist via flag).
- `memory_search` `recovered_from` + `recovery_duration_ms` fields.
- `memory_ingest` recursive character splitter.
- `memory_drill` remove `include_context` flag.
- Tool description audit script.
- ~150 LOC.

### Phase 6 — remaining
- 3 onboarding questions (memory enable, cross-encoder, aux model) — only Q6.3 (auto-absorb) is ready.
- Tool description constants per doc 06 §3.
- Audit script comparing strings in code vs doc 06.

### Phase 7 — remaining
- Retention (log rotation to 30 days + gzip).
- HTTPS push opt-in.
- 11 remaining telemetry events per roadmap §10.1 (most are already emitted; some sites need wiring).

### Phase 8 — Validation
- LoCoMo bench run with v2 pipeline.
- 200 hand-coded adversarial QAs (50 each in coder, sales, support, personal-assistant).
- 7-day soak test.
- Documentation pass.

---

## 4. Recommended next step

**Phase 1.9 — Wire v2 pipeline in DreamConsolidator**. It's the critical closure of Phase 1 + blocks Phase 8 validation (we can't bench the new flow if the wiring is missing). Estimated 1-2 focused work sessions.

After: **Phase 5 d1** (wire memory_search to `run_search_pipeline`) — unblocks Phase 8 and starts delivering FTS+RRF to the real agent.

---

## 5. Repo state

```
Branch: memory/phase-0-foundations
Commits since main: ~15
Tests: 4885 passing, 16 skipped, 0 fail
Webui: builds clean
```

Last commits (newest first):

- `77e8cfc` feat(memory): memory_search exposes `keywords` parameter (Phase 5 d1 partial)
- `f2c?????` feat(memory): identity.md v2 + telemetry privacy truncation (Phase 6 + Phase 7)
- `f???????` feat(memory): Phase 3 orchestrator — search_pipeline ties FTS + RRF + sectioning
- `?` feat(memory): Phase 3 core — query router, RRF, sectioned output, lexical executor
- `?` feat(memory): FTS5 dual index + indexer + reindex CLI (Phase 2 core)
- `?` feat(memory): absorb_judge template lock + onboarding Q6.3 (Phase 1 d11+d12)
- `1ec05db` feat(memory): Dream v2 archive/quarantine/commit/telemetry (Phase 1 d5-d10)
- `?` feat(memory): Dream apply pipeline w/ jsonpatch + rollback (Phase 1 d4)
- `f64ec75` feat(memory): Dream v2 prompt package + parser + builder (Phase 1 d1-d3)
- `?` Phase 1.5 merge — hot layer v2 via worktree agent
- `b13fdd4` feat(memory): entity_page v2 schema + decay/evergreen + index meta.json
- `782fc47` refactor(memory): walker swap + archive rename + slug normalization
- `9a8bab0` refactor(memory): absorption uses top-level archive via archive_entity()
- `980e36c` feat(memory): archive helpers + bug tracker (Phase 0 deliverable 5)
- `be8fe01` feat(memory): shared workspace walker (Phase 0 deliverable 1)

When you come back tomorrow, `git log --oneline memory/phase-0-foundations` gives you the details.
