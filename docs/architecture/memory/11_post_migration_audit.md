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

### A4 — `always_on` pinned-context half has no producer — 🔲
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

### A5 — Extract prompt omits anti-duplication context (`existing_uris`) — 🔲
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

### B1 — Orphaned modules (0 production callers) — 🔨 PARTIAL (2026-06-06)
> `dream_commit_message.py` ✅ killed (the new commit path in `memory_writer` +
> `absorption` builds its own messages inline — confirmed not forgot-to-wire).
> `entity_inventory.py` held for A5, `entity_relation_cap.py` held for A3
> (wire-vs-kill depends on those decisions).
- `durin/memory/entity_inventory.py` — fed the deleted DreamConsolidator's
  `existing_uris`. **Tied to A5** (wire vs kill).
- `durin/memory/entity_relation_cap.py` — **Tied to A3** (wire vs kill).
- `durin/memory/dream_commit_message.py` — test-only; assembled the deleted
  runner's commit message. Likely pure delete (the new passes commit via
  `memory_writer`/`absorption`). **Pre-flight:** confirm the new commit path
  doesn't need it.

### B2 — Orphaned telemetry TypedDicts (defined, not in `EVENTS`) — 🔨 PARTIAL (2026-06-06)
> ✅ Killed 6 dead defs (`MemoryDreamSkipped/BudgetExhausted/LegacyStart/
> LegacyEnd/LegacySkipped/EntityFailed`Event) + their `__all__` entry. Also
> fixed the stale FIELDS of the 3 events the new passes reuse
> (`MemoryDreamStart/End/PatchApplied`Event described the deleted DreamRunner's
> shape; now match the new emit — telemetry accuracy). The 2
> `MemoryEntityRelationCap*Event` defs are held for A3 (re-added to EVENTS if we
> wire the cap, deleted if we drop it).
`MemoryDreamBudgetExhaustedEvent`, `MemoryDreamLegacyStartEvent`,
`MemoryDreamLegacyEndEvent`, `MemoryDreamLegacySkippedEvent`,
`MemoryDreamSkippedEvent`, `MemoryEntityRelationCapWarned/RejectedEvent`
(the relation-cap pair dies with A3/B1, or is re-emitted if we wire A3).
**Pre-flight:** confirm none are in `EVENTS` and none are emitted → delete the
class defs. (The schema-catalog test already enforces EVENTS↔emit; these are
just leftover defs.)

### B3 — Dead config fields — 🔲
`AutoAbsorbConfig.min_age_hours` + `judge_model` (0 reads — only docstrings).
**Resolve with A1:** if we wire `auto_absorb`, these may become live (the judge
quarantine window + judge model); if we drop `auto_absorb`, they go too. Do NOT
delete before A1 is decided.

### B4 — Stale comments/docstrings referencing deleted modules — 🔲
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

### C1 — Doc 04 (agent tools) describes the wrong write surface — 🔲
Doc 04 documents `memory_store` (now `enabled()=False`, hidden from the agent)
and never mentions `memory_upsert_entity` (the real entity-write tool). Live
surface = `memory_search, memory_upsert_entity, memory_ingest, memory_drill,
memory_forget`. **Likely "intentionally changed"** → rewrite doc 04 + doc 06 §3
to the real tools. Cross-check with A2 (ingest description) before finalizing.

### C2 — Doc 05 (dream cold path) describes the deleted system — 🔲
The entire doc describes the DreamConsolidator/DreamRunner/threshold pipeline
(episodic → JSON-Patch → archive, lock/throttle/cursor, quarantine) that was
deleted and replaced by `extract_dream`/`refine_dream`/`dream_passes` (sessions
→ attributes via CAS). **Mostly "intentionally changed."** → rewrite doc 05 to
the new model. **BUT** while rewriting, confirm each old guarantee has a
new-model equivalent or a conscious drop (cross-ref A1 auto_absorb, A3 cap, A5
existing_uris, and the throttle/cap we just restored). This is where a "forgot
to implement" can hide inside "doc drift."

### C3 — Stale implementation-status tables + scattered legacy refs — 🔲
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
