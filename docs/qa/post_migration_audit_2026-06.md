# 11 — Post-Migration Audit & Remediation Tracker

> **Status:** ACTIVE — created 2026-06-06, after the legacy → entity-centric
> migration (deletion of the DreamConsolidator/DreamRunner cluster, the agent
> Dream class, MEMORY.md/USER.md injection, the episodic-consolidation pipeline).
>
> Surfaced by a 2-agent parallel audit (doc-vs-code gap analysis + dead-code
> sweep), with **every finding re-verified by hand** (grep/read of the actual
> execution path). Items already tracked in `08_scope_and_discarded.md`,
> `10_remaining_work.md`, `99_gaps_audit.md` are excluded.

## How we work this list

We take items **one at a time**. Before starting any item, run its pre-flight
protocol below, write down what the verification found, then decide. Do not act
on the audit's claim alone — the audit is a *hypothesis*.

**Global context first — for every item.** Trace how the functionality sits in
the whole memory flow (`write → index → search → dream → context/pin`) and what
else a change touches. No decision here is local: e.g. wiring `auto_absorb`
changes what the daily cron does to the vault; killing a "dead" module may
remove the only home for a behavior we still want. Record the cross-impact.

### Per-category pre-flight

- **(A) Implementation gap** — Confirm the gap is REAL by tracing the
  **end-to-end execution sequence in code**: entry point (tool call / cron /
  reactive trigger) → … → the exact spot where the spec'd behavior should
  happen. It is only a gap if a live path genuinely never reaches that behavior.
  Record the traced path (file:line → file:line → …). If the path *does* reach
  it, mark the finding ❌ (false positive).

- **(B) Dead code** — Confirm it is REALLY dead: no non-test importer/caller,
  not reachable from any live entry point (tool loader, command router, cron,
  websocket). Then **decide the direction**: is it dead *by design* (the
  behavior is gone → delete it), or did we *forget to wire it* (the behavior is
  still wanted → wire it, don't kill it)? "Dead" ≠ "delete" — sometimes the fix
  is to connect it.

- **(C) Doc drift** — The doc says X, the code does Y. Determine **which
  happened**: (1) we *forgot to implement* X (→ implement it, or consciously
  reclassify to `08_scope_and_discarded.md`), or (2) we *intentionally
  changed/discarded* X during the migration (→ update the doc to match the
  code). Never edit a doc without first knowing which of the two it is — that's
  how a real missing feature gets silently "fixed" by deleting its spec.

### Status legend
🔲 pending · 🔍 verifying · 🔨 in progress · ✅ done · ❌ rejected (false positive / won't do)

---

## A. Implementation gaps (designed, not functional)

### A1 — `auto_absorb` config is not wired into the refine pass — ✅ DONE (2026-06-06)
**Severity:** High. **Tied to recent work:** the webui `auto_absorb` toggle (just
added) is therefore inert.

**Finding.** The daily cron calls `run_refine_pass(workspace, model=model)`,
which calls `run_refine(workspace, llm_invoke=…, model=model)` — passing
**neither** `confidence_threshold` **nor** any `auto_absorb.enabled` check. So
refine **always auto-merges** whenever `verdict == "same" and confidence >= 95`
(the hardcoded param default), regardless of the config. The conservative
opt-in default `AutoAbsorbConfig.enabled = False` is bypassed; tuning
`confidence_threshold` in config/webui does nothing.

**Evidence (verified):** `dream_passes.py:129` (`run_refine` call, no config),
`cli/commands.py:1348` (cron call), `refine_dream.py:85` (`confidence_threshold:
int = 95` default) + `:126` (the merge condition).

**Pre-flight (A):** trace cron job `memory_dream` → `run_refine_pass` →
`run_refine` → `absorber.absorb` and confirm no branch reads
`config.memory.dream.auto_absorb`. Also check the manual `durin memory dream`
(`memory_cmd.py`) and any reactive path.

**Cross-impact / open design question:** does refine *respect* `enabled`
(off ⇒ judge but DON'T merge, leave for manual `durin memory absorb`), or does
it *always merge* at the threshold (⇒ `enabled` is vestigial → remove it + the
webui toggle + the dead `min_age_hours`/`judge_model`)? This decides whether the
daily cron mutates the vault silently. Pick one and make config + webui + code
consistent.

**Decision (2026-06-06): Option (a) — respect `enabled`.** Pre-flight trace
confirmed the gap (cron → `run_refine_pass` → `run_refine`, no `enabled` gate,
config only written by the wizard, never read; manual `absorb-suggest`/`absorb`
path exists). **Fix:** `run_refine_pass` now takes `enabled` + `confidence_threshold`;
when disabled it skips (no judge, no merge — logs the manual path) and the cron
(`commands.py`) + manual `durin memory dream` (`memory_cmd.py`) pass both from
`config.memory.dream.auto_absorb`. The webui `auto_absorb` toggle is now live.
Test: `test_refine_pass_respects_auto_absorb_enabled`. **Follow-up (→ B3):**
`min_age_hours` + `judge_model` were NOT part of this decision and remain inert —
resolve in B3 (the new refine doesn't create entities, so the same-run-merge
quarantine `min_age_hours` guards against is largely moot → lean remove).

---

### A2 — References are not wired; `memory_ingest` still writes chunked `corpus/` — ✅ DONE (2026-06-06)
> **Decision: wire references (option a).** `memory_ingest` now stores the doc
> WHOLE as a reference (`_create_reference`) and indexes it for ALL THREE
> retrieval mechanisms: grep (warm), FTS (whole doc = lexical unit), and
> vector/embeddings (each token-aware ≤512 chunk, keyed `<ref>#<idx>` so a
> fragment hit resolves to the parent reference). Added
> `VectorIndex.upsert_reference_chunk`. The legacy chunked `corpus/` path was
> removed from ingest. **Why it shipped untested before:** the storage module
> had unit tests but there was NO integration test exercising
> `ingest → reference → index → search`. **Fixed:** `test_memory_ingest_makes_
> reference_searchable_grep_fts_vector` drives the real tool + real embedder +
> real FTS/grep/vector and asserts all three find the reference (it FAILS on the
> pre-fix code). 7/7 reference tests green.
**Severity:** High (the tool's LLM-facing description is false). Note: references
wiring was always a Phase-5 follow-on (`reference.py:11`), so this is
*known-incomplete*, not a migration regression — but the ingest tool now claims
behavior it doesn't have.

**Finding.** `memory_ingest` creates a chunked `memory/corpus/<id>.md` entry
(old ingestion model) while its description tells the LLM the document is "kept
whole as a REFERENCE." The new reference writer `ingest_reference()` has **no
production caller** (tests only); `reference.py` admits the FTS/vector chunk
wiring is a follow-on.

**Evidence (verified):** `memory_ingest.py:5,168` (corpus entry +
`_maybe_create_corpus_entry`), `ingest_reference` imported only inside
`reference.py`. Read path *does* handle `type_="reference"` (`indexer.py:563`,
`search.py:288`) — so the reader is ready, the writer isn't wired.

**Pre-flight (A):** trace `memory_ingest.execute` end-to-end → what file it
writes + what class_name + whether anything ever calls `ingest_reference`.
Confirm the description string vs the actual write.

**Cross-impact:** wiring references changes the ingest write path + the indexer
(whole-doc + token chunks vs the corpus 1500-char split) + search ranking. Also
reconcile with doc 06 §2 ("kept whole"). Decide: wire references into ingest, or
correct the tool description to say "chunked corpus" until references ship.

---

### A3 — Per-entity relation cap is not enforced — ✅ DONE (2026-06-06)
> **Decision: wire it, soft / alert-only "de momento" (option a).**
> `memory_writer.write_entity` now counts relations before/after the patches and
> calls `check_relation_cap`; crossing the soft cap (50) emits
> `memory.entity_relation_cap_warned`, crossing the hard cap (200) emits
> `memory.entity_relation_cap_rejected` — both with a log. **No write is blocked
> and no relation is dropped** (no data loss); enforcing the hard cap is a
> one-line flip in `_emit_relation_cap` when mega-hubs prove real. This
> RESOLVES the held dead-code: `entity_relation_cap.py` is now used (B1) and the
> 2 `MemoryEntityRelationCap*Event` schemas are back in `EVENTS` (B2). Tests:
> `tests/memory/test_entity_relation_cap.py` (logic + warn + hard-alert, each
> asserting no data loss); catalog test green.
**Severity:** Medium (data-integrity invariant absent; mega-hub protection).

**Finding.** `entity_relation_cap.py` (soft 50 / hard 200, doc 01 §4.4 + §10
decision 2) has **zero callers**. The new write path
`field_patch.apply_field_patch` appends relations with no cap check;
`memory_writer.write_entity` and the dreams don't call it.

**Evidence (verified):** 0 production callers of `entity_relation_cap` /
`check_relation_cap`; `field_patch.py:57-68` appends relations unchecked. The
telemetry events `memory.entity_relation_cap_warned/rejected` have no live
emitter (and were dropped from `EVENTS`).

**Pre-flight (A):** trace an entity write with many relations
(`memory_upsert_entity` → `apply_field_patch` → relation append) and confirm no
cap check fires. **(B) overlaps:** `entity_relation_cap.py` is also a dead
module — decide *wire it* (enforce the invariant in `apply_field_patch`) vs
*delete it* (drop the invariant).

**Cross-impact:** enforcing the cap at write time affects every entity write
(agent + dream); rejected/ truncated relations need telemetry + a clear failure
mode. Tie the decision to whether mega-hubs are a real risk in practice.

---

### A4 — `always_on` pinned-context half has no producer — ✅ DONE (2026-06-06)
> **Decision: build the full distillation pass (not just a producer).** New
> `always_on_dream.run_always_on_pass`: gathers feedback entities
> (stance/practice/feedback), an LLM judge ranks them and DROPS contradictions,
> the survivors are fitted into a **token budget** (`memory.dream.
> always_on_token_budget`, parameter, default 1500), and the selected refs are
> marked `always_on` (the rest unmarked). No entity is ever deleted — only the
> flag flips — so a pruned/contradicted item returns when the budget frees or
> the conflict resolves. Wired into the daily cron + manual `durin memory dream`
> (after refine). Telemetry `memory.dream.always_on` + log. Tests:
> `tests/memory/test_always_on_dream.py` (budget-fit, contradiction-drop,
> no-LLM fallback, unmark-over-budget — each asserting no data loss). The pin
> producer is the agent authoring feedback entities; the dream owns the
> always_on flag (matches design §2.11 "the dream marked always_on").
**Severity:** Medium.

**Finding.** `principal.mark_always_on()` has **zero producers**; no dream or
tool ever marks an entity/feedback `always_on`. So `list_always_on()` always
returns `[]` and the "always-on guidance" block of the pinned context
(`principal.py` §2.11) is permanently empty. The principal-entity pin itself IS
wired (`agent/context.py:236-256`).

**Evidence (verified):** 0 production callers of `mark_always_on`.

**Pre-flight (A):** trace `_build_pinned_memory` → `build_pinned_context` →
`list_always_on` and confirm it can only ever be empty. **(B) overlaps:** decide
*wire it* (who marks always_on? the refine dream? a user command?) vs *drop the
feature* (remove `mark_always_on`/`list_always_on` + the empty block).

**Cross-impact:** an always-on block is injected EVERY turn → token cost +
prompt-cache stability. If wired, it needs a budget + a producer policy.

---

### A5 — Extract prompt omits anti-duplication context (`existing_uris`) — ✅ DONE (2026-06-06)
> **Decision: accept + kill (not a gap).** Verified the new extract enriches
> entities BY the agent's explicit `memory_upsert_entity` ref (it never
> creates from scratch), so it cannot introduce a duplicate that
> `existing_uris` would prevent. Dedup is covered elsewhere: the agent is
> told to `memory_search` first (author-time) and the refine pass merges
> alias-overlap duplicates (A1). `existing_uris` was the OLD consolidator's
> need — redundant now. Killed `entity_inventory.py` (dead, 0 callers).
**Severity:** Low/Medium (refine compensates).

**Finding.** Doc 05 §5.1/§10-7 specifies the write prompt include an
`existing_uris` (top-N by recency) block so the dream extends existing entities
instead of duplicating. The new `extract_dream.build_extract_prompt` includes
only the entity's own attributes + body + turns; `entity_inventory`
(`existing_uris_by_recent_mtime`) is not imported.

**Evidence (verified):** `extract_dream.py` build prompt; `entity_inventory.py`
0 callers (also a B dead-module item).

**Pre-flight (A):** trace the extract prompt build and confirm no existing-URI
context. **(B) overlaps:** `entity_inventory.py` is dead — wire it into the
extract prompt vs delete it. Note duplication is now caught downstream by
refine, so this may be acceptable — quantify before acting.

---

## B. Dead code (migration leftovers)

> For each: pre-flight **(B)** — confirm truly dead, then *kill vs wire*. Several
> B-items are the same code as an A-gap (the module is dead **because** we forgot
> to wire the feature) — resolve those together with their A item.

### B1 — Orphaned modules (0 production callers) — ✅ DONE (2026-06-06)
> `dream_commit_message.py` ✅ killed (the new commit path in `memory_writer` +
> `absorption` builds its own messages inline — confirmed not forgot-to-wire).
> `entity_inventory.py` ✅ killed (A5 — redundant in the new model).
> `entity_relation_cap.py` ✅ revived/wired (A3).
- `durin/memory/entity_inventory.py` — fed the deleted DreamConsolidator's
  `existing_uris`. **Tied to A5** (wire vs kill).
- `durin/memory/entity_relation_cap.py` — **Tied to A3** (wire vs kill).
- `durin/memory/dream_commit_message.py` — test-only; assembled the deleted
  runner's commit message. Likely pure delete (the new passes commit via
  `memory_writer`/`absorption`). **Pre-flight:** confirm the new commit path
  doesn't need it.

### B2 — Orphaned telemetry TypedDicts (defined, not in `EVENTS`) — ✅ DONE (2026-06-06)
> ✅ Killed 6 dead defs (`MemoryDreamSkipped/BudgetExhausted/LegacyStart/
> LegacyEnd/LegacySkipped/EntityFailed`Event) + their `__all__` entry. Also
> fixed the stale FIELDS of the 3 events the new passes reuse
> (`MemoryDreamStart/End/PatchApplied`Event described the deleted DreamRunner's
> shape; now match the new emit — telemetry accuracy). The 2
> `MemoryEntityRelationCap*Event` defs were RE-ADDED to `EVENTS` + emitted (A3
> wired the cap, alert-only); their docstrings corrected to match (C3).
`MemoryDreamBudgetExhaustedEvent`, `MemoryDreamLegacyStartEvent`,
`MemoryDreamLegacyEndEvent`, `MemoryDreamLegacySkippedEvent`,
`MemoryDreamSkippedEvent`, `MemoryEntityRelationCapWarned/RejectedEvent`
(the relation-cap pair dies with A3/B1, or is re-emitted if we wire A3).
**Pre-flight:** confirm none are in `EVENTS` and none are emitted → delete the
class defs. (The schema-catalog test already enforces EVENTS↔emit; these are
just leftover defs.)

### B3 — Dead config fields — ✅ DONE (2026-06-06)
> **Decision (b): wire `min_age_hours`, kill `judge_model`.** `min_age_hours`
> is now honoured by `run_refine` — a candidate pair is quarantined (skipped,
> `memory.absorb.skipped` reason=quarantine) when either entity is younger
> than the window (created_at, falling back to updated_at). Wired from config
> through the cron + manual. `judge_model` removed (marginal bias knob; only
> docstrings referenced it → B4). Test: `test_refine_quarantines_fresh_
> entities_min_age_hours` (quarantined under a large window, merged with 0).
`AutoAbsorbConfig.min_age_hours` + `judge_model` (0 reads — only docstrings).
**Resolve with A1:** if we wire `auto_absorb`, these may become live (the judge
quarantine window + judge model); if we drop `auto_absorb`, they go too. Do NOT
delete before A1 is decided.

### B4 — Stale comments/docstrings referencing deleted modules — ✅ DONE (2026-06-06)
> Fixed the docstrings/comments that described the CURRENT dream in present
> tense via deleted classes (MemoryConfig.dream, absorb_judge module doc,
> absorption, aliases_cache, memory_search, model_resolve → now name the
> extract/refine passes). Remaining mentions are accurate HISTORICAL
> migration notes ('this REPLACED the legacy DreamRunner…') — kept as
> intentional context, not debris.
~20 live files mention `DreamConsolidator`/`DreamRunner`/`dream_apply`/
`threshold_trigger`/`dream_quarantine` in comments/docstrings (e.g.
`absorption.py:266,298`, `config/schema.py` MemoryConfig docstring,
`memory_ingest.py:85`, `telemetry/schema.py` legacy-event docstrings,
`store.py`/`provenance.py` "curator and dream"). Behavior-neutral debris; sweep
last, after the A/B decisions settle the surrounding code.

---

## C. Doc drift (docs lag the code)

> The doc says X, the code does Y. **Pre-flight (C) per section:** was X *forgotten*
> (→ implement or reclassify to `08_discarded`) or *intentionally changed* (→
> rewrite the doc to match the code)? Most of C is "intentionally changed during
> the migration" — but confirm each, because some overlap with A (a doc still
> describing a feature we *meant* to keep but didn't wire).

### C1 — Doc 04 (agent tools) describes the wrong write surface — ✅ DONE (2026-06-06)
> Rewrote doc 04 to the live toolset: documented `memory_upsert_entity` (the
> write tool), marked `memory_store` DISABLED, fixed ingest→references (A2)
> and the FRAGMENT/INGESTED markers (N3). Aligned with doc 06 §3. Sync test
> 7/7. (Verified against the code.)
Doc 04 documents `memory_store` (now `enabled()=False`, hidden from the agent)
and never mentions `memory_upsert_entity` (the real entity-write tool). Live
surface = `memory_search, memory_upsert_entity, memory_ingest, memory_drill,
memory_forget`. **Likely "intentionally changed"** → rewrite doc 04 + doc 06 §3
to the real tools. Cross-check with A2 (ingest description) before finalizing.

### C2 — Doc 05 (dream cold path) describes the deleted system — ✅ DONE (2026-06-06)
> Rewrote doc 05 (720→~470 lines) to the four-pass model (extract / refine /
> skill / always_on), triggers (cron = all four; reactive = extract only,
> verified in commands.py; manual), config, and the two-track model. Removed
> the threshold trigger, consolidator prompt, JSON-Patch apply pipeline, and
> per-entity cursor. Kept §8 absorb-judge (verified). Each old guarantee was
> cross-checked against a new-model equivalent (A1/A3/N3/N4).
The entire doc describes the DreamConsolidator/DreamRunner/threshold pipeline
(episodic → JSON-Patch → archive, lock/throttle/cursor, quarantine) that was
deleted and replaced by `extract_dream`/`refine_dream`/`dream_passes` (sessions
→ attributes via CAS). **Mostly "intentionally changed."** → rewrite doc 05 to
the new model. **BUT** while rewriting, confirm each old guarantee has a
new-model equivalent or a conscious drop (cross-ref A1 auto_absorb, A3 cap, A5
existing_uris, and the throttle/cap we just restored). This is where a "forgot
to implement" can hide inside "doc drift."

### C3 — Stale implementation-status tables + scattered legacy refs — ✅ DONE (2026-06-06)
> doc 01 §11 status table + legacy refs fixed (agent); doc 05 status/telemetry
> fixed. Also fixed stale CODE docstrings: relation_cap events (A3 alert-only)
> + sectioned_output fragment intro (N3). REMAINING: doc 00 (overview) still
> describes the legacy dream / memory_store / JSON-Patch (lines 16, 70, 141-
> 148, 194, 207-208) + 3 cosmetic [V2] labels in doc 01.
Doc 00/01/02 prose + the "Implementation status" tables in doc 01 §11 / doc 05
§15 cite deleted files (`dream_apply.py`, `dream_git_history.py`,
`dream_prompt_builder.py`, etc.) and the two-track `dream` cron / MEMORY.md story
(`00 §16`, `05 §0`). Pure doc fixes once C1/C2 land.

---

## Working order (proposed — confirm per item)

1. **A1** (auto_absorb) — highest leverage + closes the inert webui toggle.
2. **B1/B2/B3** (dead code) — but only the items NOT tied to an undecided A
   (i.e. `dream_commit_message`, the orphaned TypedDicts; hold
   `entity_inventory`/`entity_relation_cap`/auto-absorb config until A5/A3/A1).
3. **A3, A4, A2, A5** — feature wiring decisions, each with its cross-impact.
4. **C1 → C2 → C3** — doc rewrites, last, so they describe the settled code.

Each row flips to ✅ only after: pre-flight done + decision recorded + (if code)
tests green + (if behavior) live-verified.

---

## D. Completeness re-audit (2026-06-06) — specs not implemented, found by a whole-docs vs code pass

A second comprehensive doc-vs-code pass (all of docs 00-07,10) after A1-A5/B1-B4,
run because rewriting the docs (C) could silently erase the spec of an
unimplemented feature. Every item verified by reading the code.

### N1 — Human edits clobbered by the system's hard-reset ff — ✅ DONE (2026-06-06)
> `_fast_forward_working_tree` does `porcelain.reset(root, "hard")` (its own TODO
> admitted the gap). A user's uncommitted hand edit was clobbered by the next
> system write. **Fix:** `memory_writer._commit_dirty_as_user` commits dirty
> working-tree `.md` edits with `author:user` before any system write touches git.
> Tests: `test_user_edit_guard.py` (survives, author:user, no-op on clean). Also
> corrects the phantom ✅ on `10_remaining_work` P2.3.

### N2 — Nothing re-indexes the vector reactively (entity pages vector-stale) — ✅ DONE (2026-06-06)
> The watcher's `reindex_one_file` is FTS-only; `memory_upsert_entity` + the
> extract dream never embed; `upsert_entity_page` has ONE caller (absorption
> merge). So every new/edited entity is vector-stale until a merge or full
> `durin memory reindex` — not just user edits. Fix: the reactive index path
> re-embeds (FTS + vector).

### N3 — Per-entity `dream_processed_through` cursor never advanced — ✅ DONE (2026-06-06)
> **Decision: remove it (two-track model, user-confirmed).** Investigation of
> the fragments answered the question: the only LIVE fragment producers are
> `/remember` (episodic, user-authored "curator never touches") and session
> close (session_summary). They are a SEPARATE raw track, never meant to be
> consolidated into entity pages — the new extract dream builds entities from
> SESSIONS, not fragments. So the per-entity cursor (the deleted
> DreamConsolidator's fragment→page graduation tracker) is genuinely dead:
> nothing advances it, and nothing should. Removed the field + all readers
> (hot_layer, entity_ranker pre/post partition → renamed signal `tagged_rank`,
> absorb_judge, graph_api, search_pipeline) + `load_cursors_from_entities_dir`
> / `_is_pre_cursor` / `_parse_cursor`. Behaviour-neutral (cursor was always
> null). Docs 01/03/06 updated. Deleted/updated the cursor tests across 7
> files. 992 memory tests green. **Follow-up (→ C):** the deleted-consolidator
> flow diagrams in doc 01 §ascii (lines ~578-650) + doc 05 still describe
> fragment→page consolidation — fold into the doc-rewrite pass.
> Nothing writes it (absorption only copies on merge; extract advances a per-
> SESSION cursor). Hot-layer "graduation" + entity_ranker pre/post-cursor logic
> run against a null cursor. May be obsolete-by-redesign (decide wire vs doc).

### N4 — No automatic episodic→archive consolidation — ✅ ACCEPT (2026-06-06)
> **Decision: accept — by design (two-track model, same as N3).** Episodic is
> the raw user track (`/remember` facts the curator must never touch + session
> summaries). Auto-archiving it would destroy the user's explicit memory.
> Fragments are NOT consolidated into pages, so there is no "consumed →
> archive" lifecycle to run. `archive_episodic` stays a manual operation
> (`memory_forget` / webui). Volume is low (memory_store disabled). If volume
> ever matters, a size/age CAP is the lever — NOT auto-archive. Doc 01 §5.3 /
> doc 05 §7 (the old consume-and-archive lifecycle) → fold into the C pass.
> `archive_episodic` only called by forget/webui (manual). Episodic accumulates
> (low volume: memory_store disabled). Doc 05 §7 / doc 01 §5.3.

### N5 — `durin memory reindex` doesn't write meta.json; no embedding-model-change detection — ✅ DONE (2026-06-06)
> **Decision (a): wire both.** `record_built_model` (index_meta) records the
> embedding model + keeps an audit trail; `durin memory reindex` now uses the
> CONFIGURED model (was the default) and calls it after the vector rebuild
> (N5a). `ensure_index_fresh(embedding_model=...)` detects a stored-vs-config
> model mismatch and rebuilds the VECTOR index + records the new model (N5b) —
> a same-dimension swap no longer returns silently stale results. Wired from
> the search tool (`self._embedding_model`). TDD: test_index_meta_model_change
> (record + detect-change-rebuilds + no-rebuild-when-matching).
> `cmd_reindex` never calls `save_index_meta`; `ensure_index_fresh` checks only
> schema_version, not the model id. Same-dim model swap is silent. Doc 02 §7.

### N6 — Tool-description sync test guards the DISABLED memory_store, not the live tools — ✅ DONE (2026-06-06)
> **Decision (a): full fix.** Root cause was deeper than the test — doc 06 §3
> itself documented the OLD toolset (search/store/ingest/drill) with NO
> section for the live `memory_upsert_entity` / `memory_forget`, so there was
> no canonical text to sync against. Added doc 06 §3.5 (`memory_upsert_entity`)
> + §3.6 (`memory_forget`) with their exact `.description` text, marked §3.2
> `memory_store` DISABLED, renumbered the sync requirement → §3.7. Added the
> two sync tests; the live tools' descriptions are now doc-governed + drift-
> guarded. 7/7 sync tests green.
> `test_tool_description_sync` omits `memory_upsert_entity` + `memory_forget` →
> their descriptions can drift undetected. Doc 04 §8 / doc 06 §3.5.

### N7 — absorb-judge mtimes not passed by refine; no search-time staleness filter — ✅ DONE (2026-06-06)
> **N7a wired, N7b accepted.** `refine_dream._page_mtime` now passes each
> page's file mtime to `judge_pair` (was always rendering "(unknown)") so the
> judge's staleness reasoning works. Test: test_refine_judge_mtime. **N7b
> accept:** the 15-min health-check cron already reconciles staleness; a
> per-search 60s mtime-lag filter adds a stat() per hit for marginal gain —
> not worth the latency.
> `refine_dream` doesn't pass canonical/absorbed mtimes to `judge_pair`; no
> per-search 60s mtime-lag filter (the 15-min cron covers it on a slower cadence).

### N8 — `rebuild_from_workspace` skipped references (found by the live e2e) — ✅ DONE (2026-06-06)
> The live end-to-end verification (forcing all entity types through a full
> reindex) caught it: `VectorIndex.rebuild_from_workspace` walked entries +
> entity pages + skills but NOT `memory/references/`, so a `durin memory reindex`
> (or the N5 model-change rebuild) silently dropped reference SEMANTIC search —
> only the ingest-time vector indexing survived, until the next reindex wiped it.
> Fixed: added a references pass (re-embed each reference's token-aware chunks).
> Test: `test_rebuild_from_workspace_indexes_reference_chunks`. Also fixed doc 01's
> session storage layout (it showed nested `sessions/<id>/<id>.jsonl`; the code —
> `SessionManager._get_session_path` + every reader — is FLAT `sessions/<key>.jsonl`).

### N9 — Webui Memory config missing the newest params (found by the live webui check) — ✅ DONE (2026-06-06)
> Headless-browser verification of the dashboard caught it: the curated Memory >
> Dream settings exposed the core lifecycle params (enabled, cron, post_compaction,
> on_session_close, auto_absorb toggle, throttle, max_seconds) but NOT the params
> added later in this audit — `always_on_token_budget` (A4) and the `auto_absorb`
> sub-knobs `confidence_threshold` + `min_age_hours` (A1/B3). They were settable
> only via "Todos los ajustes" (raw). Added all three as curated controls in
> `MemorySettings.tsx` (+ es/en i18n). Re-verified live: the Dream section now
> renders 10 controls with correct titles/descriptions/values (95 / 24 / 1500).
> Also confirmed the graph view renders correctly (7 nodes · 6 edges · 1 phantom,
> matching the entity-integrity check) with per-type filters.

### N10 — acquire-on-gap Path B orphaned by the migration, re-homed (found while doc-auditing skills) — ✅ DONE (2026-06-06)
> The skills doc-accuracy pass surfaced it. `skill_acquire_seed` (`_scopes={"dream"}`)
> + `acquire_safe_seed` — the autonomous skill-acquisition primitives (acquire-on-gap
> **Path B**, spec `2026-06-03-skill-acquire-on-gap-design.md`, originally BUILT +
> live-verified in the **2h Dream's phase-2**) — were ORPHANED when the entity-centric
> migration deleted that phase-2. No agent loads `scope="dream"`, and the replacement
> skill-extract pass registered only Read/Edit/SkillWrite, so the dream could only author
> from scratch — a **silent feature regression**, not dead code (the user caught the
> mischaracterization). Re-homed: `dream_passes._build_skill_extract_tools` now
> hand-registers `skill_search` + `skill_acquire_seed` (config allowlist) and
> `_SKILL_EXTRACT_PROMPT` drives search → acquire-safe-seed → author-fallback. Tests:
> `tests/memory/test_skill_extract_acquire.py` (5 tool names wired + allowlist carried).
> **Live-verified:** a real skill-extract run called `skill_search → skill_acquire_seed
> ×3 → skill_write` (empty allowlist → seeds safe-rejected → authored from scratch).
