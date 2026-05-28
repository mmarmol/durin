---
title: Implementation roadmap
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing this system
depends_on: 00_overview.md and all 01-08 docs
---

# Implementation roadmap

This document is the bridge between specification (docs 00-08) and code. It defines the phases, their dependencies, done criteria, and verification approach for taking durin's memory subsystem from current state to the v2 target.

**Principle:** every phase ends in a working, mergeable state. No phase leaves the system broken. Most phases are independently shippable — you can pause between any two phases for as long as needed.

**This doc adds no new decisions** — it sequences the work that the other docs have already specified.

---

## 1. Scope

### In scope

- Phase boundaries with concrete done criteria.
- Dependencies (what must precede what).
- Test / verification approach per phase.
- Migration tactics for changes that touch user data (entity pages, indices).
- Risk register and mitigations.

### Out of scope

- Re-specifying anything from docs 00-08. This doc references; it does not redefine.
- Cost estimates in person-hours. Phases are bounded by deliverables, not time.

---

## 2. Phases overview

```
Phase 0 — Foundations (schema, parsers, walker)
   │
   ├─→ Phase 1 — Dream v2 (consolidator prompt package + JSON Patch + archive)
   │      │
   │      └─→ Phase 5 — Tools v2 (keywords, sectioned results, memory_drill update)
   │
   ├─→ Phase 1.5 — Hot layer assembly (spec verification + v2 rendering + telemetry)
   │
   ├─→ Phase 2 — Indexing v2 (FTS5 dual + indexer + watcher + auto-commit + health cron)
   │      │
   │      └─→ Phase 3 — Search pipeline v2 (RRF + entity-aware + sectioning)
   │             │
   │             └─→ Phase 4 — Cross-encoder integration (opt-in)
   │
   ├─→ Phase 6 — Prompts v2 (identity.md + tool descriptions + Dream prompt package)
   │
   └─→ Phase 7 — Telemetry v2 (new events + retention + privacy)
          │
          └─→ Phase 8 — Validation (bench + soak testing)
```

Phases can be worked in parallel where dependencies allow. The critical path is `0 → 2 → 3 → 4 → 8`. Phase 1.5 depends on Phase 0 (the v2 entity-page schema parser ships there, not in Phase 1). Phase 1.5 can run in parallel with Phase 1 and Phase 2/3 because hot layer is independent of Dream's apply pipeline and of the search pipeline.

---

## 3. Phase 0 — Foundations

**Goal:** core data plumbing that all later phases depend on.

### 3.1 Deliverables

- Entity page schema v2 (extend `EntityPage` dataclass with `attributes`, `relations`, `provenance` fields). Backward-compat: v1 pages parse with these as empty.
- `decay_half_life` and `evergreen` frontmatter fields supported in entries.
- Shared workspace walker `walk_memory(workspace, include_archive=False)` — single chokepoint.
- `memory/archive/` folder convention; helpers `archive_episodic(uri)`, `archive_entity(uri, into_uri)`.
- Slug normalization helper in `entities.py` (recursive splitter, NFC, transliteration, suffix on collision).
- Schema-version field in `meta.json` (for future migrations).

### 3.2 Done criteria

- All existing tests pass.
- Round-trip tests for v2 entity pages (with attributes/relations/provenance, CJK, URLs).
- v1 → v2 migration is non-destructive (parse a v1 page, write it back, byte-identical).
- Archive helpers tested with episodic and entity moves.
- Walker excludes archive in all callers (audit: indexer, entity_ranker, alias bootstrap).

### 3.3 Specs consumed

- `01_data_and_entities.md` §3.5 (entity v2 schema), §3.6 (archive), §4.5 (slug normalization), §8 (backward compat), §9 (YAML safety).
- `02_indexing.md` §6.5 (walker chokepoint).

### 3.4 Risk

- Existing tests for entity_page.py assume v1 schema. Schema extension may need test updates. Mitigation: keep v1 fields untouched; only add new optional fields.

---

## 4. Phase 1 — Dream v2

**Goal:** Dream emits JSON Patch + uses prompt package + archives consumed episodic.

### 4.1 Deliverables

- Dream prompt package in `durin/templates/dream/`:
  - `consolidator.md` (rewritten with input slots for `existing_schema`, `existing_uris`, `recent_history`)
  - `json_patch_reference.md`
  - `rules.md`
  - `commit_format.md`
  - `examples/` (6 few-shot files)
- Prompt builder that assembles the package and injects context blocks.
- Parser for new output format: `===PATCH===` + `===BODY_DELTA===` + `===COMMIT===` with `json_repair` tolerance.
- Apply pipeline using `jsonpatch` library: validate ops, apply over frontmatter dict, write back atomically with `.md.bak` rollback.
- Archive workflow: move consumed episodic to `memory/archive/episodic/`; update LanceDB to delete row.
- Quarantine logic: structural failures increment `dream_failure_count`; 3 in a row → 7-day quarantine.
- Commit message hybrid: skill instructs format, code auto-completes `Trigger:` + `Run-id:` and verifies/auto-fills the LLM trailers.
- Telemetry: `memory.dream.patch_applied`, `memory.dream.entity_failed` (new events).
- **`user_authored` protection filter:** in `_discover_pending_consolidations` (or equivalent), exclude entries whose frontmatter has `author: user_authored` from the consolidation batch. Same filter applied in `_maybe_auto_absorb` to skip absorb-judge over user-authored entity pages. Telemetry: emit `entries_skipped_user_authored` count in `memory.dream.end` event. Regression test: a `user_authored` stable entry survives a Dream pass untouched. Enforces the protection rule documented in `01_data_and_entities.md` §4.6.1.
- **Git history formatter** for the `{recent_history}` prompt slot (per `05_dream_cold_path.md` §5 and `06_prompts_and_instructions.md` §4.2): function that runs `git log --since='30 days ago' -- <entity_path>` on `memory/.git/`, parses each commit's subject + short diff, formats as a compact block for the prompt. ~50 LOC.
- **`absorb_judge.md` prompt template** verified or updated per `06_prompts_and_instructions.md` §5; tracked as part of the prompt package deliverable. Template content matches doc 06 verbatim.
- **v1 → v2 entity-page schema parser**: extend `entity_page.py::EntityPage.from_text` to parse v2 fields (`attributes`, `relations`, `provenance`) when present, default to empty when absent. Verify round-trip safety (v1 pages re-render without v2 sections; v2 pages preserve all fields). Tests cover both directions.
- **Onboarding Q6.3 (auto-absorb opt-in)**: per `06_prompts_and_instructions.md` §6.3, ship the wizard question text as part of this phase (not Phase 6), because it depends on absorb-judge being available. Default: N.

### 4.2 Done criteria

- Dream successfully consolidates 10 test entities with v2 prompt package, producing valid JSON Patch + commit.
- Apply pipeline handles malformed PATCH (recovers via `json_repair`) and unrecoverable PATCH (rolls back to `.md.bak`).
- Archive folder receives consumed episodic; default search no longer surfaces them.
- Quarantine triggers correctly after 3 structural failures on the same entity.
- All existing absorb-judge tests still pass.
- Git commits carry the structured trailers (verified via `git log --grep`).

### 4.3 Specs consumed

- `05_dream_cold_path.md` §5, §6, §7, §11, §12 (all of it).
- `06_prompts_and_instructions.md` §4 (Dream prompt package).
- `07_telemetry_and_observability.md` §6.

### 4.4 Risk

- LLM may not reliably emit JSON Patch syntax. Mitigation: 6 few-shot examples + `json_repair` tolerance + explicit error feedback. If still unreliable, fall back to constrained output via OpenAI structured outputs / Anthropic tool use.
- Round-trip safety must hold under JSON Patch. Mitigation: `.md.bak` restore on validation failure.

---

## 4b. Phase 1.5 — Hot layer assembly

**Goal:** the hot layer (canonical pages + recent fragments + identity + headlines + known-entities list) is injected into every agent prompt without any tool call, per `06_prompts_and_instructions.md` §8.

Listed between §4 (Phase 1) and §5 (Phase 2) in document order, but its real dependency is **Phase 0** (the v2 entity-page schema parser ships there). It does NOT depend on Phase 1 — HotLayer reads whatever entity pages exist on disk, whether populated by Dream (Phase 1 output) or by manual user edits, and renders the v2 fields it finds. It also does NOT depend on the FTS5 indexer (Phase 2). HotLayer is a prompt-level concern, not search.

**Current code:** `durin/memory/hot_layer.py` exists. This phase verifies code matches spec and fills gaps where it doesn't.

### 4b.1 Deliverables

- Verify `hot_layer.py` budgets match `06_prompts_and_instructions.md` §8.2: `_IDENTITY_BUDGET_CHARS = 800`, `_CANONICAL_BUDGET_CHARS = 2400`, `_FRAGMENTS_BUDGET_CHARS = 1200`, `_HEADLINES_BUDGET_CHARS = 1200`, `_ENTITIES_BUDGET_CHARS = 600`, caps `_MAX_CANONICAL = 12`, `_MAX_FRAGMENTS = 8`, `_MAX_HEADLINES = 12`, `_MAX_ENTITIES = 50`.
- Verify section rendering format matches §8.3 (markdown H2 headings + intro sentences + CANONICAL/FRAGMENT marker blocks).
- Verify cursor logic for fragments (§8.4): only post-cursor episodic/stable; corpus and pending excluded.
- Verify refresh cadence (§8.5): `read_hot_layer()` called per prompt build (cheap disk read), changes at most once per Dream pass in practice → upstream prompt cache stays warm between dreams.
- Add telemetry event `memory.hot_layer.failure` (per `06_prompts_and_instructions.md` §8.7) for when disk read or parser fails — agent prompt continues without the section, no hard error.
- Add v2 fields support: when `01_data_and_entities.md` §3.5 v2 entity pages (with `attributes` + `relations` + `provenance`) are touched by Dream (Phase 1), the canonical block rendering should reflect them (rendered frontmatter as prose for the LLM to consume, NOT raw YAML dump).
- Documentation: tests that lock the rendered output format so a regression breaks loudly.

### 4b.2 Done criteria

- Reading hot layer from a workspace with 20 entity pages + 30 episodic post-cursor + 1 IDENTITY.md produces output within the 1900-token budget.
- Marker formats `=== CANONICAL: <uri> (consolidated <ts>) ===` and `=== FRAGMENT: <path> (ts <ts>) ===` match doc 06 §8.3 verbatim.
- Sections with zero hits are omitted (no empty H2 headers).
- v2 entity pages render their `attributes` and `relations` as prose in the canonical block (not as YAML).
- Telemetry event fires on simulated disk error; agent prompt still builds (degraded) without crashing.
- Prompt cache remains warm across consecutive turns when no Dream pass intervenes (verify via prompt-hash assertion or upstream provider's cache-hit telemetry).

### 4b.3 Specs consumed

- `06_prompts_and_instructions.md` §8 (all of it).
- `01_data_and_entities.md` §3.5 (v2 entity page schema).
- `02_indexing.md` §3.6 (archive exclusion — hot layer also respects it).

### 4b.4 Risk

- Spec drift: `hot_layer.py` was implemented in an earlier phase ("Phase 1.9" per its module docstring) before doc 06 §8 was written. Some details may not match. Mitigation: spec-vs-code diff at the start of this phase, then ADAPT the spec to match working code OR adjust code to spec, decision per item.
- Budget overruns when v2 entity pages render attributes/relations as prose — they were larger than v1 body-only. Mitigation: include the rendered frontmatter inside the `_CANONICAL_BUDGET_CHARS = 2400` budget; tighter truncation per entity if needed.

---

## 5. Phase 2 — Indexing v2

**Goal:** FTS5 dual table + file watcher + auto-commit of user edits.

### 5.1 Deliverables

- SQLite FTS5 setup at `.durin/index/fts.sqlite` with two tables (`memory_fts` unicode61 + `memory_fts_trigram`).
- Indexer that builds both tables from `walk_memory(workspace)`.
- Re-index-on-write: every tool that writes a `.md` triggers sync re-derivation in LanceDB + both FTS5 tables.
- File watcher using `watchdog` library; detects manual edits, re-derives, auto-commits with `author: user`.
- LanceDB schema extension: store full `body` per row (v2 decision — confirmed user-side cost trade-off).
- **`durin reindex` CLI command** (explicit deliverable): wipes `.durin/index/`, walks `memory/` (excluding archive), batch-embeds (32 docs/batch), writes LanceDB + both FTS5 tables, updates `meta.json`. Progress output to stdout (entries walked / embedded / written). Error handling: continue on per-row failures with summary at end. Optional `--target lancedb|fts5|all` flag for selective rebuild.
- Schema version mismatch check at startup; refuse to operate if model/version differs.
- Telemetry: `memory.index.write`, `memory.index.rebuild`, `memory.index.staleness_detected`.
- **Health-check cron** for async restoration of corrupt indices (per `03_search_pipeline.md` §14.2): background scheduler runs every 15 min (configurable). Probes LanceDB, FTS5 tables, cross-encoder model presence (if enabled), file watcher process, disk free space. On failure → trigger background rebuild from `.md` using the existing `.dream.lock`. Eager trigger within 30s after `memory.search.failure` event. Escalation: 3 consecutive failures in 1h → emit `memory.health.critical` and pause that component's restoration until manual intervention. Telemetry: `memory.health_check` event per tick (pass/fail status per component). Config under `memory.health_check.*`. **~80-150 LOC** total (much less than synchronous-recovery would have cost).

### 5.2 Done criteria

- After Dream apply, both FTS5 tables contain the updated entity.
- Manual edit to a `.md` (via `vim memory/entities/person/marcelo.md`) is picked up by watcher within seconds; reflected in next search.
- `durin reindex` runs in < 60s on a 10k-entry workspace.
- Indexer skips `memory/archive/**` (verified via integration test).
- Cold-tier search returns body without disk read (verified via storage layer assertion).

### 5.3 Specs consumed

- `02_indexing.md` all of it.
- `01_data_and_entities.md` §3.6 (archive exclusion).

### 5.4 Risk

- `watchdog` polling on macOS may miss rapid file changes. Mitigation: combine with mtime poll fallback (`watchdog` supports this).
- FTS5 trigram table size may grow large. Mitigation: monitor via `memory.index.write` events; if size becomes an issue, add a `disable_trigram` config flag.

---

## 6. Phase 3 — Search pipeline v2

**Goal:** RRF cross-source merge + entity-aware rerank + sectioned output.

### 6.1 Deliverables

- Intent router: CJK detection, query pattern detection (email/URL/ID), output decision (which FTS5 table to use).
- Cross-source RRF: vector + lexical + grep merged with `k=60` and configurable weights.
- Dynamic boost: when `keywords` param is provided, `w_lexical` boosted to 2.5 for that call.
- Entity-aware rerank reused from `entity_ranker.py`; integrated downstream of RRF (not upstream).
- Per-source cap in sectioning (max 3 corpus hits per `ingest_id`).
- Sectioned output renderer: produces CANONICAL/FRAGMENT/SESSION/INGESTED blocks; omits empty sections.
- Telemetry: `memory.recall.lexical`, `memory.recall.rrf` (new events).
- Failure handling per §14 doc 03: graceful degradation in the hot path (no inline recovery). Recovery is async via the health-check cron from Phase 2 §5.1.

### 6.2 Done criteria

- Bench LoCoMo run with v2 pipeline shows ≥ baseline (60.8% → maintain or improve).
- Query "esposa de Marcelo" matches `spouse: susana` correctly (cross-lingual + sectioned).
- Query with `keywords="marcelo@mxhero.com"` returns the FTS5 exact match at rank 1.
- Failure path: kill LanceDB index file, issue a search → degraded result (lexical+grep only). Background health-check cron (Phase 2) restores the index within 15 min (default tick interval). Next search post-restore returns normal results.

### 6.3 Specs consumed

- `03_search_pipeline.md` all of it.
- `04_agent_tools.md` §6 (rendering).

### 6.4 Risk

- Tokens in sectioned output may exceed agent context budget for high `limit`. Mitigation: cap `limit` at 50; truncate snippets at 200 chars in warm mode.

---

## 7. Phase 4 — Cross-encoder integration (opt-in)

**Goal:** cross-encoder reranker available, opt-in via config + onboarding + dashboard.

### 7.1 Deliverables

- Cross-encoder runner module: loads model lazily, batches inputs, returns scored results.
- Default model: `jinaai/jina-reranker-v2-base-multilingual`. Config field for alternative models.
- Integration in search pipeline (step 5 per §9 doc 03).
- Onboarding wizard: question 6.2 per doc 06 ("Enable advanced reranker? [y/N]").
- **Web dashboard memory settings panel** with three controls per `06_prompts_and_instructions.md` §6.5: (a) cross-encoder toggle + model dropdown (jina-v2, bge-base, bge-v2-m3, qwen3-reranker-0.6b), (b) consolidation threshold count (read-write), (c) temporal decay summary (read-only). Shared workspace-config backend.
- Telemetry: `memory.recall.rerank`.
- Graceful degradation: model load failure → step is no-op, log warning, fused scores carry forward.

### 7.2 Done criteria

- With cross-encoder OFF (default), bench latency unchanged from Phase 3.
- With cross-encoder ON, bench shows ≥ +5pp on retrieval quality measure (LoCoMo or hand-coded set).
- Model auto-downloads on first use if remote allowed; fails gracefully if disk full.
- Web dashboard reflects config; toggling persists.

### 7.3 Specs consumed

- `03_search_pipeline.md` §9.
- `06_prompts_and_instructions.md` §6.2.

### 7.4 Risk

- Model download (~1.1GB) is slow. Mitigation: progress indicator during onboarding; user can defer; first agent query that requires model triggers download with informative message.

---

## 8. Phase 5 — Tools v2

**Goal:** tool API surface matches doc 04.

### 8.1 Deliverables

- `memory_search`: add `keywords` param, cap `limit` at 50, return `recovered_from` + `recovery_duration_ms` fields, `rendered` field contains sectioned output.
- `memory_store`: descriptions match doc 06 §3.2. Improved dedup warning.
- `memory_ingest`: recursive character splitter (preferred separators: paragraphs > lines > sentences > words > chars). Target ~1500 chars with ~200 char overlap.
- `memory_drill`: no `include_context` flag (removed per discussion). Reads `.md` and returns content. Description per doc 06 §3.4.
- Tool description constants in code match doc 06 §3.1-§3.4 verbatim.

### 8.2 Done criteria

- Existing agent code using `memory_search` continues to work (backward compat with `keywords: None`).
- New tests for `keywords` boost behavior.
- `memory_ingest` produces overlap-aware chunks via recursive splitter.
- Tool description audit script confirms code matches docs.

### 8.3 Specs consumed

- `04_agent_tools.md` all of it.
- `06_prompts_and_instructions.md` §3.

---

## 9. Phase 6 — Prompts v2

**Goal:** identity.md and tool descriptions reflect the canonical text in doc 06.

### 9.1 Deliverables

- `durin/templates/agent/identity.md::Memory` section per doc 06 §2.
- **All four onboarding wizard questions** from doc 06 §6:
  - §6.1 Memory subsystem enable (default Y) — Phase 0/6.
  - §6.2 Cross-encoder opt-in (default N) — covered by Phase 4; ensure the question text matches doc 06 §6.2 verbatim.
  - §6.3 Auto-absorb opt-in (default N) — **move to Phase 1** (when absorb-judge is shipped), not Phase 6. Note added here for traceability.
  - §6.4 Aux model picker (default: use agent's preset).
- Tool description constants per doc 06 §3 (memory_search, memory_store, memory_ingest, memory_drill).
- Audit script: compares strings in code/templates against doc 06; fails CI on divergence.

### 9.2 Done criteria

- Identity.md memory section matches doc 06 §2 verbatim.
- Bench LoCoMo with new identity does NOT regress from v2 result (60.8% → 64.7%).
- Tool description audit script passes in CI.

### 9.3 Specs consumed

- `06_prompts_and_instructions.md` all of it.

### 9.4 Risk

- Subtle wording changes might regress bench. Mitigation: re-run LoCoMo before+after; if regression, isolate the change.

---

## 10. Phase 7 — Telemetry v2

**Goal:** all new events emitted and consumed.

### 10.1 Deliverables

- Add new TypedDicts to `durin/telemetry/schema.py` for **13 new events** (explicit checklist):
  - `memory.recall.lexical` (doc 07 §4.3)
  - `memory.recall.rerank` (§4.4)
  - `memory.recall.rrf` (§4.5)
  - `memory.recall.decay` (§4.7) — audit A9 (2026-05-28)
  - `memory.dream.entity_failed` (§6.4)
  - `memory.dream.patch_applied` (§6.5)
  - `memory.search.failure` (§8.1)
  - `memory.index.write` (§9.1)
  - `memory.index.rebuild` (§9.2)
  - `memory.index.staleness_detected` (§9.3)
  - `memory.health_check` (§9.4)
  - `memory.health.critical` (§9.5)
  - `memory.hot_layer.failure` (per doc 06 §8.7)

  `memory.silent_retrieval_miss` (§4.6) was discarded — see doc 08 §2.11. The heuristics didn't generalise to multi-lingual workloads; replacement triggers for the §2.F revisit live in doc 08 §4.1.

- Wire emit calls in: search pipeline (lexical/rerank/rrf/decay/failure), index module (write/rebuild/staleness), Dream (patch_applied/entity_failed), health-check cron, hot layer assembly.
- Privacy: query truncation to 200 chars in `emit_tool_event` for memory events.
- Retention: log rotation at 30 days; compression after.
- Optional: HTTPS push of events (default off, opt-in via config).
- **Tool description code-vs-doc audit** (per `04_agent_tools.md` §8): script that compares `memory_*.py::DESCRIPTION` constants + `identity.md::Memory` section against doc 04 §2.3/§3.3/§4.3/§5.3. Fails CI on divergence.

### 10.2 Done criteria

- All emit sites produce events visible in `~/.durin/telemetry/*.jsonl`.
- Privacy truncation verified by integration test.
- Log rotation runs and produces `.jsonl.gz` after configured threshold.

### 10.3 Specs consumed

- `07_telemetry_and_observability.md` all of it.

---

## 11. Phase 8 — Validation

**Goal:** confirm the v2 system meets quality bar before declaring MVP done.

### 11.1 Deliverables

- LoCoMo bench run with the full v2 pipeline; report comparing to current 64.7%.
- **Relation-target recall slice** (audit G5, 2026-05-28): partition LoCoMo failures by whether the gold answer hinges on a relation target's full name (e.g. "Who is Marcelo's spouse?"). If recall on that slice is **≥ 2 pp lower** than the bench mean AND the per-failure trace shows the missing token IS the target entity's `name` field, ship alias resolution in `VectorIndex._render_frontmatter` per `02_indexing.md` §4.2 "Why slug-only and not the target's resolved name". If the slice matches the mean, mark the deferred item as decided-against and remove the open question from doc 02 §4.2 (it has been validated empirically).
- Hand-coded adversarial set for non-LoCoMo domains: 50 QAs each for coder, sales, support, personal-assistant. Tests generalist capability.
- Soak test: run durin against a workspace for 7 days with simulated daily user activity; confirm:
  - Dream cost stays within $0.25-$1.50/day.
  - No entity quarantine triggered without justification.
  - Index size growth tracks workspace size.
  - No silent retrieval misses (telemetry shows healthy `recall.strategy` distribution).
- Documentation pass: every doc in `docs/memory/` reflects implemented state.

### 11.2 Done criteria

- LoCoMo bench: ≥ 64.7% (v2 baseline) WITHOUT cross-encoder; ≥ 70% WITH cross-encoder.
- Adversarial generalist sets: ≥ 50% per domain (initial bar; tune up over time).
- Soak test reports clean (no anomalies in §11.1).
- Documentation lint: no `(pending)` markers remaining; all decisions show resolution.

### 11.3 Failure modes to track

The bench harness emits a structured failure category per failed QA so regressions are diagnosable. Categories (from `docs/28_locomo_results_and_sota_gap.md` §4 — based on real LoCoMo failure audit over 43 fails):

| Category | Definition | Typical % (v2 baseline) |
|---|---|---|
| `synthesis_overgeneration` | Agent retrieved the truth but over-elaborated — added extras the judge marked as wrong, or chose a wrong abstraction level | 30% |
| `coverage_gap_list` | Vector top-K missed items in a list-shape question | 26% |
| `ranking_miss` | Right answer exists in memory but not in top-K returned to agent | 21% |
| `wrong_answer` | Retrieved unrelated content / confused entities | 16% |
| `judge_strict` | Agent essentially right; judge marked near-miss as fail | 9% (artifact, not a system bug) |
| `no_retrieval` | 0 memory_search tool calls, or only filesystem grep — agent answered from cold recall | 9% |
| `temporal_aggregation` | Agent had dated facts but didn't filter/count/sort by date | 7% |
| `multi_hop_chain_break` | Agent got hop 1 of a multi-hop question, didn't pursue hop 2 | 5% |
| `iteration_limit` | Agent looped on tool calls without reaching answer | 2% |

**Operational use:**

- The judge categorizer (or post-bench script) tags each failed QA with one dominant category (multi-attribution flagged separately for the 4 cases where two causes are equally strong).
- Phase 8 success requires no single category to grow > 5pp from v2 baseline without an explicit cause documented.
- Targeted improvements map to specific categories. See `docs/28_locomo_results_and_sota_gap.md` §5 (Mitigation roadmap) for the impact-effort matrix.

### 11.4 Specs consumed

- All docs as reference for verification.

---

## 12. Cross-cutting concerns

### 12.1 Migration tactics

**For entity pages:**
- v1 → v2 is lazy (Dream upgrades on first touch). No bulk migration needed.
- Manual edits to v1 pages remain v1 until Dream sees the entity again.

**For sessions:**
- `_last_summary` field already exists; just needs vectorization on top.

**For indices:**
- LanceDB schema bump: stop and rebuild required (single `durin reindex`).
- FTS5 tables: new — no migration, just create.

**For config:**
- New `memory.search.*` fields: defaults applied if absent in user's `config.json`. No required user action.

### 12.2 Feature flags

For phased rollout in production-style installs:
- `MEMORY_V2_ENABLED` (default false during phase 0-7; flip to true after phase 8 passes).
- `MEMORY_CROSS_ENCODER_ENABLED` (default false always).
- `MEMORY_FTS5_TRIGRAM_ENABLED` (default true; allows disabling if storage becomes an issue).

### 12.3 Rollback plan

- Each phase commits to its own branch. Merge to main only after that phase's done criteria pass.
- Phase rollback: revert the branch's merge commit. Index re-build with `durin reindex` puts state back.
- Catastrophic rollback (corruption of `memory/`): `memory/.git/` provides git history; revert to a known-good commit.

---

## 13. Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| LLM unreliable with JSON Patch syntax | 1 | Few-shot examples + json_repair + structured output API as fallback |
| FTS5 trigram storage growth | 2 | Telemetry monitors size; can disable trigram via flag |
| Watchdog misses changes on macOS | 2 | Polling fallback included in watchdog config |
| Cross-encoder model download fails | 4 | Graceful degradation; clear error message to user |
| Bench regression after prompt v2 changes | 6 | Run bench before+after; isolate changes; revert if needed |
| User edits during Dream apply | 1, 2 | Lock + mtime comparison; user-author commits separated |
| Disk full during reindex | 2 | Pre-check free space; abort with informative error |
| Cost overrun (Dream LLM bills) | 1 | Telemetry tracks cost; alerting at $1.50/day; threshold knob exists |
| Privacy leak in telemetry | 7 | Query truncation enforced; tests verify; content never logged |

---

## 14. Cross-references

- Architecture and principles: `00_overview.md`.
- Data types and entity model: `01_data_and_entities.md`.
- Indexing: `02_indexing.md`.
- Search pipeline: `03_search_pipeline.md`.
- Agent tools: `04_agent_tools.md`.
- Dream cold path: `05_dream_cold_path.md`.
- Prompts and instructions: `06_prompts_and_instructions.md`.
- Telemetry: `07_telemetry_and_observability.md`.
- Scope and discarded: `08_scope_and_discarded.md`.
