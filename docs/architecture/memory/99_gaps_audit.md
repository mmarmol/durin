---
title: Gaps audit — corpus vs code (working doc)
version: 0.1
status: actionable backlog
last_updated: 2026-05-27
audience: humans + LLMs working through remediation
purpose: One-stop list of gaps identified between docs/architecture/memory/ corpus (00-09) and actual durin code + prior docs. To be remediated item-by-item; this doc is the checklist.
---

# Memory corpus — gaps audit (working doc)

Audit performed 2026-05-27 against `/Users/marcelo/git_personal/durin/durin/` (code) and `/Users/marcelo/git_personal/durin/docs/*.md` (prior docs, especially 18, 20, 25, 28). This doc lists every gap that warrants remediation, prioritized by severity. Each item is an actionable unit — discuss, decide, apply, mark done.

**Usage:** when remediating, follow the order below. Each item ends in `Status: open | resolved | wontfix` and `Decision: <date> <note>`.

---

## Executive summary

- **Corpus structure is correct** at conceptual level.
- **Implementation details lag specification** by ~15-20%: several production-code components (HotLayer, DreamRunner production patterns, QueryRewriter details, EntityRanker RRF rationale) have insufficient spec.
- **3 contradictions** between code and docs (auto-absorb default, Dream system ownership, intent classification usage).
- **5 concepts from prior docs (18, 25, 28) are omitted** from the new corpus — most notably §2.F eager-inject, operational risks list, LoCoMo failure taxonomy.

20 gaps total. 5 HIGH severity (affect implementation directly), 9 MEDIUM (spec gaps with existing code), 6 LOW (cosmetic / clarifications).

---

## HIGH severity (5 items)

### H1. Two Dream systems coexist — pick the canonical one

**Evidence:**
- `durin/memory/dream.py::DreamConsolidator` — stateless, older design.
- `durin/memory/dream_runner.py::DreamRunner` — production runner with lock + throttle.
- Corpus (doc 05) describes the runner. Code has both.

**Question:** is `DreamConsolidator` deprecated? Is it still called by anyone? Should the consolidator be merged into the runner or kept as a thin utility?

**Action proposed:** audit callers of `DreamConsolidator` (vs. `DreamRunner`). If consolidator is only called from runner, mark consolidator as internal-only utility. If it's called from elsewhere (e.g., tests, scripts), document those paths in doc 05.

**Affects:** doc 05 §3, §4, §15.

**Status:** resolved
**Decision:** 2026-05-27 — audit was imprecise. Verified callers: `DreamConsolidator` is invoked only from `DreamRunner` (production) and from tests; nothing else in production uses it directly. Not two systems competing — two layers separated by concern (Consolidator = logic, Runner = operation). Added §3.0 "Components" subsection to doc 05 clarifying the relationship. No architectural change needed.

---

### H2. Auto-absorb default mismatch (code OFF, docs implicit ON)

**Evidence:**
- `dream_runner.py:91` `auto_absorb_judge_model: str | None = None` — auto-absorb is OFF unless model is configured.
- Doc 05 §8 describes the absorb-judge flow as if always active.

**Question:** which is the v2 intended state — opt-in OFF (current code) or always-on (current spec)?

**Trade-offs:**
- Default ON: every Dream pass spends LLM budget judging pairs. Cost grows with workspace size. Risk: bad merges if judge model is noisy.
- Default OFF (current): operators must configure `aux_models.memory` to enable. Safer; explicit. Aligned with cross-encoder pattern (also opt-in).

**Action proposed:** align doc 05 to opt-in OFF; mention it explicitly in doc 06 onboarding wizard.

**Affects:** doc 05 §8, doc 06 §6.

**Status:** resolved
**Decision:** 2026-05-27 — code is correctly conservative (default OFF, threshold 95, min_age 24h with explicit rationale in `AutoAbsorbConfig` comment); doc 05 §8 had been written as if always-on. Rewrote §8 with new subsections 8.1 "Why opt-in", 8.2 "Configuration" (settings table), 8.3 "Trigger (when enabled)", 8.5 "Judge decision and merge action", 8.6 "Merge action (when confidence passes threshold)", 8.7 "Self-consistency bias mitigation". Added §6.3 to doc 06 with onboarding wizard text for the operator (default no).

---

### H3. HotLayer has no dedicated spec section

**Evidence:**
- `durin/memory/hot_layer.py` is production code that injects identity + canonical pages + recent fragments into the agent prompt at every turn.
- Token budgets (~1900 total), canonical/fragment rendering logic, post-cursor filtering, canonical_cursors machinery — all in code but not in corpus.
- Doc 03 mentions "hot layer" in §2 (read path) but doesn't specify.

**Action proposed:** add a dedicated section in doc 03 (e.g., new §16 "Hot layer pre-fetch") covering: what gets injected, budget breakdown, marker convention, when it activates, telemetry. OR a new short doc `03b_hot_layer.md`.

**Affects:** doc 03, possibly new doc.

**Status:** resolved
**Decision:** 2026-05-27 — placed in doc 06 §8 (not doc 03) because the hot layer is prompt-level content (what the LLM sees without tools), not part of the search pipeline. Added 8 subsections covering: why it exists, composition + budgets (5 sections, ~1900 tokens total), section order + rendering, cursor logic for fragments, refresh cadence (cache-warmth rationale), relationship to tools (eager vs lazy), failure mode, and module decisions. Cross-reference added to doc 03 §18 pointing readers to doc 06 §8.

---

### H4. §2.F eager-inject pattern (from doc 25) is omitted

**Evidence:**
- Doc 25 §2.F documents proactive memory injection: before each agent turn, do an automatic memory_search with the user's message and wrap results into the prompt (Hermes/OpenClaw pattern).
- Gate (doc 25): "§2.E telemetry active + tool-call frequency shows silent retrieval miss". Memory `project_memory_eager_inject_2f_plan.md` records gate met 2026-05-24.
- Corpus describes only the lazy path (agent invokes memory_search via tool).
- HotLayer (H3) already does PARTIAL eager-injection but not the full §2.F.

**Question:** does §2.F enter MVP, or stays explicitly in backlog?

**Trade-offs:**
- In: closes silent-retrieval-miss class of failures (4-8 cases per 102 LoCoMo bench).
- Out: lazy path is simpler; HotLayer covers most of the eager-inject value already.

**Action proposed:** decide and either add to doc 04 / 03 (if in MVP) or add to doc 08 §4 backlog (if deferred) with explicit reasoning.

**Affects:** doc 03 or doc 04 (if in) or doc 08 (if deferred).

**Status:** resolved
**Decision:** 2026-05-27 — deferred to backlog with **telemetry-driven trigger** to activate. Rationale: HotLayer already covers ~70% of the silent-retrieval-miss problem, and the multi-query identity.md pattern shipped +3.9pp on LoCoMo v2 by teaching the agent to invoke search itself. Adding §2.F costs +50-130ms latency per turn + cache miss; only worth paying if the value is observable. Added entry in doc 08 §4 (with new sub-section §4.1 detailing mechanism, why deferred, activation trigger). Added telemetry event `memory.silent_retrieval_miss` in doc 07 §4.6 that detects re-asks / corrections / negations after agent answered without invoking search — aggregate rate becomes the data-driven decision point (>5% rolling 7-day → activate).

**Superseded 2026-05-28 (audit B9 + §2.11 + E7):** the telemetry event `memory.silent_retrieval_miss` was discarded because its three detection heuristics (substring overlap, negation tokens, correction patterns) don't generalise to multi-lingual workloads (durin's seed bench includes CJK and Spanish). Doc 07 §4.6 now points to doc 08 §2.11 explaining the discard. The §2.F revisit triggers were rewritten to use language-agnostic signals: explicit user feedback (thumbs-down/retry), bench failure clusters, and offline LLM judge over bench traces (post-hoc, not per-turn). See doc 08 §4.1.

---

### H5. Operational risks from doc 18 §9 are omitted

**Evidence:**
- Doc 18 §9 enumerates 6 named risks: R1 HyperMem SOTA gap, R2 mega-hub growth, R3 dream cost ceiling, R4 cross-system identity, R5 LLM entity resolution, R6 alias collision.
- New corpus (doc 08) lists discarded experiments and unadopted mechanisms but NOT these risks.

**Action proposed:** add §3 to doc 08 titled "Operational risks (from doc 18)" — for each risk, state: (a) what it is, (b) what mitigation/decision in v2 covers it, (c) status (open / mitigated / accepted).

**Affects:** doc 08 §3 (new section).

**Status:** resolved
**Decision:** 2026-05-27 — added §3 "Operational risks (from doc 18 §10)" mapping all 6 risks to v2 status: R1 accepted (operational coherence ≠ bench accuracy), R2 partially mitigated (relation cap; sub-paging deferred), R3 mitigated (telemetry + cost alarm), R4 accepted (manual aliases), R5 mitigated (absorb-judge OFF default + threshold 95 + 24h quarantine), R6 partially mitigated (slug suffix; alias one-to-many deferred). Two backlog items surfaced and added to §5: sub-paging by scope (R2) and alias one-to-many resolution (R6), each with concrete telemetry-based triggers. Renumbered §4-§7 to §5-§8.

---

## MEDIUM severity (9 items)

### M1. QueryRewriter implementation details under-documented

**Closed 2026-05-31:** the module was deleted (commit `482e2eb`) — the audit question "is QueryRewriter intended only as cold-path future tool, or could it surface again?" was answered "discarded, not deferred" after 5 days with zero callers. See doc 08 §2.1 for the closure narrative.

**Status:** resolved
**Decision:** 2026-05-27 — concluded that the module is mostly dead and the reusable pieces are short (~40 LOC total: json_repair wrapper + code fence stripping). Documenting full API would invite re-activation of a discarded approach. Instead: doc 08 §2.1 gained a "Maintenance plan" subsection stating that when Dream apply v2 (JSON Patch) is implemented, those two pieces should be extracted into a shared `durin/memory/_llm_parsing.py` and the rest of `query_rewriter.py` deleted in the same commit. Other utilities (CJK normalization, etc.) have no planned caller and get deleted along with the module. Importing from `query_rewriter.py` is discouraged in the meantime — trigger the cleanup rather than perpetuating a dead dependency.

---

### M2. EntityRanker RRF rationale missing

**Evidence:**
- `durin/memory/entity_ranker.py` implements RRF with K=60, list-length asymmetry, pre/post-cursor exclusion.
- Doc 03 §8 mentions "RRF" but does NOT explain K=60 (Cormack standard), asymmetry rationale (vector list typically longer than entity-match list), pre-cursor exclusion logic.

**Action proposed:** expand doc 03 §8 with subsections explaining K=60 choice + asymmetry + cursor logic. Cite Cormack 2009.

**Affects:** doc 03 §8.

**Status:** resolved
**Decision:** 2026-05-27 — expanded doc 03 §8 from 2 subsections to 6: §8.1 RRF constant K=60 (with Cormack 2009 citation), §8.2 Why RRF not multiplicative (LanceDB L2 distances are non-linear corpus-dependent), §8.3 List-length asymmetry deliberate (entity signal as nudge, not override — note G9), §8.4 Pre/post-cursor logic (pre-cursor excluded entirely to avoid canonical-page duplication), §8.5 Ordering for N>1 entities (preserves vector-sort order — note G13), §8.6 Output. Content transcribed from `entity_ranker.py` module docstring with light editing for prose flow.

---

### M3. DreamRunner production patterns under-documented

**Evidence:**
- `dream_runner.py` has PID-based lock recovery, STALE_LOCK_SECONDS=600 mtime check, daemon threading for triggers, auto-absorb config plumbing.
- Doc 05 §4 mentions lock + throttle abstractly but skips production scaffolding.

**Action proposed:** add subsections to doc 05 §4 documenting: lock file structure (JSON), stale recovery mechanism, daemon threading for trigger dispatch, auto-absorb config wiring. These are operational details that matter when debugging.

**Affects:** doc 05 §4.

**Status:** resolved
**Decision:** 2026-05-27 — narrowed scope to 2 targeted additions (rest lives in code docstrings). (1) Lock JSON schema was already documented in §4.1 from prior edits. (2) Added §2.1 "Threshold trigger dispatches asynchronously" explaining the daemon-thread pattern that wraps `DreamRunner.run` when called from `memory_store`/`memory_ingest`. Implications surfaced: tool returns before Dream completes; partial pass on process exit is safe (cursor only advances on full apply); other triggers (cron/session_close/post_compaction/manual) run foreground in their callers. Auto-absorb config plumbing covered already in §8 (H2 fix).

---

### M4. ThresholdTrigger logic under-documented

**Evidence:**
- `durin/memory/threshold_trigger.py` counts episodic + corpus entries (not just episodic). Emits separate trigger labels (`threshold` from memory_store, `post_ingest_threshold` from memory_ingest). Dispatches via daemon thread.
- Doc 05 §2 says "threshold" trigger exists but doesn't detail this logic.

**Action proposed:** expand doc 05 §2 with explicit table of trigger labels and what each counts. Clarify daemon dispatching.

**Affects:** doc 05 §2.

**Status:** resolved
**Decision:** 2026-05-27 — (1) Trigger table updated from 5 to 6 labels, splitting `threshold` into `threshold` (store-path) and `post_ingest_threshold` (ingest-path) with separate Source column. (2) New §2.2 "Threshold counting logic" added explaining: per-entity counting (not workspace-wide), two contributions (episodic post-cursor + corpus tagged), why corpus counts without being consolidated (signal of "hot entity"), stable entries don't count, per-entity dispatch (entity_filter applied), multiple entities crossing produce separate daemon threads serialized by lock. Configuration knobs (`threshold_entries`, `min_seconds_between_runs`) documented inline.

---

### M5. `batch_last_ts` invariant (G2 correctness) not documented

**Evidence:**
- `dream.py::ConsolidationResult` line 79-80: `batch_last_ts` overrides whatever the LLM puts in `Cursor-after`. This is critical: if LLM stops mid-batch (didn't process all entries), the runner forces cursor to the actual last processed entry.
- Doc 05 §6 doesn't mention this.

**Action proposed:** add to doc 05 §6 (apply pipeline): "After applying ops, the cursor is set to `batch_last_ts` (the timestamp of the latest entry actually processed) regardless of what the LLM included in its COMMIT — this guarantees no data loss if the LLM truncates."

**Affects:** doc 05 §6.

**Status:** resolved
**Decision:** 2026-05-27 — added §6.1 "Cursor advance invariant (G2)" to doc 05. Documents: cursor is set to `batch_last_ts` (runner-known) NOT `Cursor-after:` (LLM-emitted); rationale is the LLM mid-batch truncation case where trusting LLM cursor would skip unprocessed entries forever; LLM's Cursor-after is preserved in git commit for audit; divergence between the two is logged at INFO as diagnostic signal; trade-off acknowledged (LLM-skipped entries within a batch are dropped from re-processing — rare, recoverable via next batch or manual run, not data loss).

---

### M6. LoCoMo failure taxonomy (from doc 28) not captured

**Evidence:**
- Doc 28 identifies 8 dominant failure modes from real bench runs: synthesis_overgeneration (30%), coverage_gap_list (26%), ranking_miss (21%), wrong_answer (16%), no_retrieval, format_mismatch, judge_error_possible, etc.
- New corpus has bench plan (doc 09 §11) but doesn't surface this taxonomy.

**Action proposed:** add to doc 09 §11 (Validation): "Failure modes to track" subsection listing the 8 categories with definitions, so the bench harness emits them and operators have a vocabulary to discuss failures.

**Affects:** doc 09 §11.

**Status:** resolved
**Decision:** 2026-05-27 — added §11.3 "Failure modes to track" to doc 09 with full 9-category taxonomy from doc 28 §4 (audit revealed 9, not 8 — `synthesis_overgeneration 30%, coverage_gap_list 26%, ranking_miss 21%, wrong_answer 16%, judge_strict 9%, no_retrieval 9%, temporal_aggregation 7%, multi_hop_chain_break 5%, iteration_limit 2%`). Each category has a definition and typical % from v2 baseline. Operational use spec: bench tags each failed QA; Phase 8 success requires no category grows > 5pp without documented cause; targeted improvements map to categories via doc 28 §5 impact-effort matrix. Renumbered §11.3 (Specs consumed) to §11.4.

---

### M7. Suggested entity types (from doc 18 §4) not captured

**Evidence:**
- Doc 18 §4 proposes 8 starter types: `person, place, project, topic, event, artifact, stance, practice`.
- New doc 01 §4.1 says "type is open" without suggesting a starter set.
- Dream needs to pick a type when creating a new entity — without suggestions, the LLM picks arbitrarily.

**Action proposed:** add to doc 01 §4.1 a "Suggested starter types" subsection listing the 8 with one-line descriptions. Mark as suggestions, not closed catalog. Dream prompt (doc 06 §4.2) references this list when LLM needs to pick a type.

**Affects:** doc 01 §4.1, doc 06 §4.2.

**Status:** resolved
**Decision:** 2026-05-27 — added §4.1.1 "Suggested starter types" to doc 01 with full 8-type table (person, place, project, topic, event, artifact, stance, practice), Tulving mapping, cross-profession examples, and "what is NOT a primary type" table (learning → topic/practice update; error → event with negative valence; decision → event+stance; file/symbol → artifact). Open vocabulary explicitly preserved. Added the suggested-types injection block to doc 06 §4.2 consolidator prompt so Dream sees the list when creating a new entity URI. Cross-reference between docs maintained.

---

### M8. `provenance.py` ContextVar (user vs agent authorship) not documented

**Evidence:**
- `durin/memory/provenance.py` has `_MEMORY_AUTHOR` ContextVar with values `agent_created | user_authored`. Used in `memory_store` to tag who created each entry.
- Doc 01 §4.6 (Provenance) talks about source_ref provenance but NOT authorship classification.

**Why it matters:** Dream MUST NOT touch user-authored stable entries (the user explicitly persisted them). Without documenting this contract, future Dream changes could accidentally modify user data.

**Action proposed:** add to doc 01 §4.6 a subsection on authorship — explain the ContextVar, the `agent_created | user_authored` field, and the rule that user-authored entries are read-only to Dream.

**Affects:** doc 01 §4.6.

**Status:** resolved
**Decision:** 2026-05-27 — added §4.6.1 "Authorship classification (separate from source_ref provenance)" to doc 01. Covers: two values (`agent_created`, `user_authored`); default `user_authored` (safe); ContextVar propagation across asyncio; `author_scope` context-manager API; persistence in frontmatter; **protection rule** (Dream and curator never modify `user_authored` entries — they may read as context but never overwrite/move/consume); where enforced in code (`dream.py::DreamConsolidator.apply()`, `dream_runner.py::_maybe_auto_absorb`); rationale for the rule. Also corrected a prior typo in doc 01 §3.3 — earlier draft listed `"agent_authored"` and `"dream"` as schema values; actual code has only `"user_authored"` and `"agent_created"`. Field declaration in §3.3 now matches code.

---

### M9. Intent classification (in QueryRewriter) — what to do with it

**Evidence:**
- `QueryRewriter` outputs `intent: factual_lookup | list | temporal | comparison | open_ended`.
- Doc 03 doesn't mention intent classification or how it would be used downstream.
- It's there in code, unused at runtime since rewriter is OFF.

**Question:** if we re-activate rewriter (cold-path future), should intent feed routing decisions? Or is it vestigial?

**Action proposed:** mention in doc 08 §2.1 that intent classification is preserved as a capability but not wired to anything in MVP. If/when needed, doc 03 (intent router) is the place to document its usage.

**Affects:** doc 08 §2.1.

**Status:** resolved
**Decision:** 2026-05-27 — chose option A (delete with the rest of the rewriter at Dream apply v2 commit). Reasoning: the intent router in `03_search_pipeline.md` §3 routes by lexical patterns (regex, CJK detection), not LLM classification — has no need for the rewriter's intent output. If LLM-based intent classification surfaces as a future need, reimplementing freshly (~30 LOC) is cleaner than carrying dead code. Doc 08 §2.1 maintenance plan updated to explicitly list intent classification among the deletable pieces alongside CJK normalization, `build_memory_llm_invoke`, and `QueryRewrite` dataclass.

---

## LOW severity (6 items)

### L1. SessionMdRender anchor stability contract

**Evidence:**
- `durin/memory/session_md.py` renders `.jsonl` sessions to deterministic markdown with stable `## turn-N` anchors. This is the basis for `session:<id>/turn-N` URIs used in `source_refs`.
- The anchor stability guarantee (numbering never changes despite consolidation) is critical but unwritten.

**Action proposed:** add a short note to doc 01 §3.1 about session markdown and the anchor convention. One paragraph.

**Affects:** doc 01 §3.1.

**Status:** resolved
**Decision:** 2026-05-27 — added paragraph to doc 01 §3.1 (Session current) documenting that `session_md.py` renders deterministic markdown with immutable `## turn-N` anchors that never change despite later consolidation/summary updates; `source_refs` like `session:<id>/turn-42` always point at the same content.

---

### L2. GraphApi (webui-facing)

**Evidence:**
- `durin/memory/graph_api.py` exposes 3 read-only endpoints (entity detail, edge detail, graph search).
- Used by the web dashboard.
- Not core to memory subsystem but part of its surface.

**Action proposed:** add a short subsection to doc 04 (tools) mentioning that webui consumes these endpoints (read-only), with one-paragraph description. Detailed shape lives in webui docs.

**Affects:** doc 04 §7 (configuration surface area).

**Status:** resolved
**Decision:** 2026-05-27 — added §7.1 "Read-only webui surfaces (informational)" to doc 04. Tabular reference to the 3 endpoints (`get_entity_detail`, `get_edge_detail`, `search_memory_api`) plus a 4th row for GraphBuilder (L3, batched together). Marked explicitly as NOT agent-facing — webui consumes them via HTTP. Read-only contract emphasized (mutations flow through agent tools or direct .md editing).

---

### L3. GraphBuilder (canvas)

**Evidence:**
- `durin/memory/graph.py` builds graph for Obsidian-style canvas (sessions + entities as nodes, co-mention edges).
- Caps: 500 nodes / 2000 edges.

**Action proposed:** also mentioned briefly in doc 04 alongside graph_api.

**Affects:** doc 04.

**Status:** resolved
**Decision:** 2026-05-27 — included in §7.1 of doc 04 alongside the graph_api endpoints (4th row of the table). Caps mentioned (500 nodes / 2000 edges). Batched with L2.

---

### L4. EmbeddingProvider abstraction

**Evidence:**
- `durin/memory/embedding.py` defines ABC + `FastembedProvider`. Single point for embedding model loading.
- Doc 02 §3.2 mentions MiniLM but doesn't mention the abstraction.

**Action proposed:** add a note to doc 02 §3.2 mentioning the abstraction so adding new providers is easy. One sentence.

**Affects:** doc 02 §3.2.

**Status:** resolved
**Decision:** 2026-05-27 — added paragraph to doc 02 §3.2 documenting the `EmbeddingProvider` ABC + `FastembedProvider` concrete impl (ONNX in-process, no GPU). Adding a new provider is a one-class change without touching the rest of the indexer. Mention of model identifier validation at construction + telemetry hooks for load + per-embed timings.

---

### L5. AliasIndex episodic bootstrap (G3.e)

**Evidence:**
- `durin/memory/aliases_index.py:75-80` (per audit) also bootstraps aliases from `entities:` field of episodic entries, not just from `aliases:` field of canonical pages.
- Rationale: makes entity_ranker useful even in cold workspaces.
- Not in corpus.

**Action proposed:** add a short note to doc 02 (or doc 01) about episodic-derived aliases having lower precedence than canonical aliases.

**Affects:** doc 02 or doc 01.

**Status:** resolved
**Decision:** 2026-05-27 — added paragraph to doc 01 §4.5 (Slug normalization) documenting the G3.e episodic bootstrap. AliasIndex.build() walks entities/ first (primary aliases) then episodic/ (lower-precedence aliases derived from `entities:` frontmatter field). Rationale: ensures entity_ranker activates in cold workspaces where Dream hasn't yet created canonical pages.

---

### L6. Memory class semantics scattered

**Evidence:**
- Which classes does Dream consolidate? threshold_trigger.py counts episodic + corpus. Code consolidates episodic + stable.
- Doc 01 §2 lists classes but doesn't state explicitly which feed Dream vs which are inert.

**Action proposed:** add a column to doc 01 §2 table: "Consumed by Dream? (yes / no / referenced-only)".

**Affects:** doc 01 §2.

**Status:** resolved
**Decision:** 2026-05-27 — added new "Consumed by Dream?" column to doc 01 §2 data classes table. Values per class clarify scattered semantics: Session=no (referenced as source_refs only); Ingested=no; Corpus=counts as trigger signal but not consumed into entity pages; Episodic=yes primary input (post-cursor consumed → archived); Stable=referenced as context but never consumed; Pending=no (intermediate); Entity=yes target (PATCH ops written here); Archive=no (terminal state).

---

## Remediation workflow

When working through this list:

1. Pick the highest-severity open item.
2. Read the linked sections in the corpus + the linked code.
3. Discuss any decisions needed (especially for HIGH items).
4. Apply the change to the appropriate doc.
5. Update the item's `Status:` to `resolved` and add a `Decision: <date> <one-line note>` line.
6. Cross-reference: if the change affects another doc, update its decisions table too.

When all HIGH items are resolved, move to MEDIUM. LOW can be batched.

---

## Notes from audit (2026-05-27)

- Audit performed via two parallel agent investigations: code-side (audit components in `durin/memory/` against corpus) and docs-side (audit prior `docs/*.md` for relevant concepts).
- Both agents returned structured findings; this doc consolidates and prioritizes.
- The 3 contradictions (H1 dream systems, H2 auto-absorb, intent classification M9) are the most urgent because they will cause inconsistency between operator expectations and runtime behavior.
- The omissions from prior docs (H4 §2.F, H5 risks, M6 LoCoMo taxonomy, M7 entity types) are less urgent but matter for completeness — these encode reasoning we did before but lost in the new corpus.

---

## Round 2 audit (2026-05-27, post-remediation)

After all 20 items above were resolved, a second audit ran to verify (a) corpus-vs-code coherence didn't introduce new contradictions, (b) the implementation roadmap (doc 09) is complete, (c) no significant memory/Dream concepts were left out.

22 new items identified. Same format as Round 1: HIGH (blocks correct behavior or major omission), MEDIUM (gap with planned trigger or known path forward), LOW (cosmetic / consolidation).

### Round 2 HIGH severity (5 items)

#### R2.1. Authorship `user_authored` protection not enforced in code

**Evidence:**
- Doc 01 §4.6.1 claims: "Dream and the curator **never** modify, archive, or consume entries with `author: user_authored`. Enforced in `dream.py::DreamConsolidator.apply()`."
- `provenance.py` defines `Author = Literal["user_authored", "agent_created"]` and the ContextVar mechanism.
- Grep for `user_authored` in `dream.py` and `dream_runner.py` returns no enforcement filter.
- The mechanism exists; the protection is documented; the code does not enforce.

**Why HIGH:** User-edited memory entries can be silently mutated by Dream consolidation passes. Real user-data integrity issue.

**Action proposed:** add explicit filter in `DreamConsolidator.apply()` (or `_discover_pending`) that drops `user_authored` entries before they enter the consolidation batch. Add a regression test. Update telemetry to emit `entries_skipped_user_authored` count in `memory.dream.end`.

**Affects:** `dream.py` code (primary), test suite, possibly doc 05 §6 (apply pipeline) to surface the skip.

**Status:** resolved
**Decision:** 2026-05-27 — chose option A (implement). Added explicit deliverable to doc 09 Phase 1 §4.1: filter `user_authored` entries in `_discover_pending_consolidations` and in `_maybe_auto_absorb` (skip absorb-judge over user-authored pages); telemetry `entries_skipped_user_authored` in `memory.dream.end`; regression test. Spec in doc 01 §4.6.1 is correct; the code work is queued in the roadmap (~30-50 LOC + tests). No spec change needed beyond the roadmap addition.

---

#### R2.2. Recovery (not just degradation) — code is degradation-only

**Evidence:**
- Doc 03 §14 specifies "Recovery > Degradation > Error". Recovery includes rebuild index, reload model, reconnect on crash.
- Code does graceful degradation (skip failed component, continue) but no automatic rebuild/reload/reconnect.
- No `rebuild_lancedb()` / `reload_fts5()` paths in `memory_search.py`.

**Why HIGH:** corpus describes a behavior the code doesn't have. Operator who reads doc 03 §14 expects search to recover from a corrupt index transparently; reality is they have to run `durin reindex` manually.

**Action proposed:** decide between (a) implement recovery as documented (more work, matches spec), or (b) tone down doc 03 §14 to "degrade gracefully; operator runs `durin reindex` for recovery" (matches code, easier).

**Affects:** doc 03 §14 OR code (depends on direction).

**Status:** resolved
**Decision:** 2026-05-27 — chose option C (hybrid): graceful degradation in hot path (no inline recovery), PLUS background health-check cron for async restoration. Pattern verified in OpenClaw's QMD sidecar. Reasoning: synchronous-in-hot-path recovery is over-engineered for the actual failure rate of index corruption (~rare); async restoration matches that cadence at ~80-150 LOC vs ~200-300 for inline. Doc 03 §14 rewritten with the two-tier model (§14.1 hot path degradation, §14.2 cold path cron, §14.3 eager trigger after failure within 30s, §14.4 escalation to critical, §14.5 config, §14.6 error response with `health_check_next_run_in_seconds`, §14.7 telemetry). Doc 09 Phase 2 gains health-check cron deliverable. Doc 07 §9 gains two new events: `memory.health_check` (per tick, pass/fail per component) and `memory.health.critical` (when retry budget exhausted). Config under `memory.health_check.*` with 15min default interval (configurable). Adapt later if usage shows different cadence.

---

#### R2.8. HotLayer has no implementation phase in doc 09

**Evidence:**
- Doc 06 §8 specifies the hot layer (100+ lines of spec, major prompt-tier component, ~1900 tokens of always-on context).
- Doc 06 §8.1 calls it "Phase 1.9 of the memory subsystem".
- Doc 09 phases are 1-8; no Phase 1.5 / 1.9 exists. HotLayer is not listed as a deliverable of any existing phase.
- Code (`hot_layer.py`) exists, so the component IS implemented — but the roadmap doesn't reflect it.

**Why HIGH:** without a phase tracking, future iterations on hot layer have no clear owner. Also implies doc 09 is incomplete relative to doc 06 spec.

**Action proposed:** add new Phase 1.5 "Hot layer assembly" between Phase 1 (Dream v2) and Phase 2 (Indexing v2), OR fold into existing phase. Either way, deliverable should be explicit.

**Affects:** doc 09.

**Status:** resolved
**Decision:** 2026-05-27 — chose option A (dedicated Phase 1.5). Added section §4b "Phase 1.5 — Hot layer assembly" between §4 (Phase 1 Dream v2) and §5 (Phase 2 Indexing v2). Deliverables (§4b.1): budget/cap verification vs `06_prompts_and_instructions.md` §8.2; section rendering format check; cursor logic check; refresh cadence verification; `memory.hot_layer.failure` telemetry event; v2 entity page rendering as prose (not raw YAML); regression tests locking output format. Done criteria + specs consumed + risks defined. Phases overview diagram in §2 updated to show Phase 1.5 between Phase 1 and Phase 5. Dependency note added: soft dependency on Phase 1 (v2 schema), independent of Phase 2/3 so they can run in parallel.

---

#### R2.17. Memory export / import not specified

**Evidence:** corpus mentions copying `workspace/` for portability but has no:
- Structured export format (e.g., JSON dump filterable by entity, scope, date range).
- Import procedure from another durin installation.
- Import from competing systems (mem0, letta).
- CLI command (`durin memory export`, `durin memory import`).

**Why HIGH:** real user need (portability, backup, migration). Memory is the asset most worth preserving; users will demand export.

**Action proposed:** add to doc 08 §5 backlog with trigger condition (e.g., "first user request for export OR before any breaking schema change"). For full design, defer to its own design doc when triggered.

**Affects:** doc 08 §5.

**Status:** resolved
**Decision:** 2026-05-27 — user clarified that no real external users exist yet, so formal export/import is genuinely deferred. The current need (copying config when installing locally on another machine) is handled informally by `cp ~/.durin/config.json` — no feature required. Added entry to doc 08 §5 backlog with explicit triggers: (1) first external user request for export, (2) before any breaking schema change. Documented `cp -r ~/.durin/workspace/` as the informal portability mechanism between same-version installations until then.

---

#### R2.18. Data deletion (GDPR-like) not specified

**Evidence:** no command, no flow, no doc for:
- "Forget everything about `person:X`" — implies cascading delete of entity page + provenance refs in other entities + episodic mentions + archived entries.
- Right to be forgotten if a user-via-channel (not the owner) requests data removal.

**Why HIGH:** real legal/ethical need. Memory contains personal info; users (and external interlocutors) will request deletion.

**Action proposed:** add to doc 08 §5 backlog with trigger (e.g., "first deletion request OR before public beta"). Document the cascading semantics required.

**Affects:** doc 08 §5.

**Status:** resolved
**Decision:** 2026-05-27 — user confirmed not relevant at this stage (no external users yet, no public exposure). Added entry to doc 08 §5 backlog with explicit triggers: (1) first external user via channel, (2) jurisdiction with right-to-be-forgotten laws, (3) any public/beta release. Until then, operator handles removal manually via file delete + git commits. Cascading semantics (entity page + archive + episodic mentions + provenance refs in other entities + LanceDB rows + FTS5 entries + git history considerations) documented in the backlog entry so the future design doc has a starting point.

---

### Round 2 MEDIUM severity (13 items)

#### R2.3. Dream JSON Patch is v2 target, code emits full page rewrite

**Evidence:** code at `dream.py::ConsolidationResult.page_text` returns full markdown; template `consolidator.md` outputs full page. No `jsonpatch` import. Doc 05 §5.2 describes JSON Patch as if implemented.

**Why MEDIUM:** doc 05 is correctly written as "v2 target" — `09_implementation_roadmap.md` Phase 1 covers this migration. Just needs an explicit cross-ref so readers don't confuse spec with current code.

**Action proposed:** add note at top of doc 05 §5.2 / §6: "**Status:** This section describes the v2 target. Current code uses full-page rewrites; see doc 09 Phase 1 for the migration."

**Affects:** doc 05 §5.2, §6.

**Status:** resolved
**Decision:** 2026-05-27 — implementation status note added at the top of doc 05 §5 covering §5 and §6 in one stroke. Points readers at doc 09 Phase 1 as the migration tracker.

---

#### R2.4. 7 new telemetry events not yet in `schema.py`

**Evidence:** doc 07 proposes `memory.recall.lexical`, `recall.rerank`, `recall.rrf`, `dream.patch_applied`, `dream.entity_failed`, `index.write`, `index.rebuild`, `index.staleness_detected`, `search.failure`. None exist in `schema.py` (verified by grep on `_REGISTRY`).

**Why MEDIUM:** known v2 target documented in doc 09 Phase 7. Just needs cross-ref.

**Action proposed:** add status note at start of doc 07 (after §1): "**Status:** events marked NEW are v2 targets; see doc 09 Phase 7 for implementation tracking."

**Affects:** doc 07.

**Status:** resolved
**Decision:** 2026-05-27 — implementation status note added at the very top of doc 07 (above §1). Points readers at Phase 7 for the deliverable.

---

#### R2.5. Per-source cap §12.4 specified but not implemented

**Evidence:** doc 03 §12.4 specifies max-3-per-`ingest_id` cap in sectioning. No grep match in `search.py` for this logic.

**Why MEDIUM:** v2 target. Doc 09 Phase 3 (search pipeline) should cover it — verify or add.

**Action proposed:** confirm doc 09 Phase 3 lists "per-source cap in sectioning" as deliverable; add if missing.

**Affects:** doc 09 Phase 3.

**Status:** resolved
**Decision:** 2026-05-27 — verified: per-source cap is already a deliverable in Phase 3 §6.1 ("Per-source cap in sectioning (max 3 corpus hits per `ingest_id`)"). Also corrected a stale "Recovery handling" reference in §6.1 / §6.2 that was obsolete post-R2.2 — updated to reference the cron-based async restoration from Phase 2.

---

#### R2.6. HotLayer refresh cadence wording ambiguous

**Evidence:** doc 06 §8.5 says "read fresh from disk on every prompt build" but the cache-warm narrative suggests "once per Dream pass". Code (`hot_layer.py`) reads disk in `read_hot_layer()` per call.

**Why MEDIUM:** documentation precision. The text says one thing, the cache-warmth reasoning implies another. Worth clarifying for readers.

**Action proposed:** verify call frequency in production. Update §8.5 to match exactly — either "every prompt build (cheap disk read)" or "once per turn but cache-stable between Dream passes". Add a one-liner showing the call site so readers can grep.

**Affects:** doc 06 §8.5.

**Status:** resolved
**Decision:** 2026-05-27 — rewrote §8.5 with the precise version: hot layer IS re-read on every prompt build (call site: `read_hot_layer(workspace)` in `hot_layer.py`); explicitly justified why ("simplicity + correctness; no cache-invalidation contract bugs"); then noted the **practical effect**: between Dream passes the rendered output is byte-identical → upstream prompt cache stays warm. Both facts true, no contradiction.

---

#### R2.7. `memory_drill.include_context` param unverified

**Evidence:** doc 04 §5.1 lists `include_context: boolean (default: false)`. `memory_drill.py` exists but parameter unverified by grep.

**Why MEDIUM:** small inconsistency risk if the param doesn't exist. Quick to verify.

**Action proposed:** grep the tool file. If missing, add it OR remove from doc 04.

**Affects:** doc 04 §5.1 OR `memory_drill.py`.

**Status:** resolved
**Decision:** 2026-05-27 — verified: `include_context` is NOT in `memory_drill.py`. Chose to **remove from doc 04** (align doc to current code) because the related-context need is already served by `memory_search` returning sectioned results. Updated §5.1 (params), §5.2 (return shape), §5.3 (tool description points readers to memory_search for related context), §9 #7 (module decisions table), §10 (implementation status — explicitly notes the proposed flag was dropped).

---

#### R2.9. Onboarding wizard — 4 questions, only 1 in roadmap

**Evidence:** doc 06 §6 defines Q6.1 (memory enable), Q6.2 (cross-encoder), Q6.3 (auto-absorb), Q6.4 (aux model). Doc 09 only explicitly covers Q6.2 in Phase 4. Q6.3 (auto-absorb) logically belongs to Phase 1 (when absorb-judge ships), not Phase 6.

**Why MEDIUM:** roadmap coverage gap. Onboarding may ship with Q6.1+Q6.4 sensible defaults; Q6.3 ordering matters (Phase 1 dependency).

**Action proposed:** doc 09 Phase 6 should explicitly list all 4 questions as deliverables, and move Q6.3 to Phase 1.

**Affects:** doc 09 Phase 1, Phase 6.

**Status:** resolved
**Decision:** 2026-05-27 — doc 09 Phase 6 §9.1 rewritten with explicit checklist for Q6.1/Q6.2/Q6.3/Q6.4. Q6.3 (auto-absorb opt-in) noted to live in Phase 1, not Phase 6, because it depends on absorb-judge shipping. Phase 1 §4.1 gained the Q6.3 deliverable explicitly.

---

#### R2.10. Web dashboard controls — partial roadmap coverage

**Evidence:** doc 06 §6.5 specifies dashboard controls for cross-encoder, threshold config, temporal decay summary. Doc 09 Phase 4 covers only cross-encoder.

**Why MEDIUM:** webui completeness.

**Action proposed:** extend doc 09 Phase 4 deliverables to enumerate all 3 controls, OR create a Phase 4.5 "Web UI memory settings".

**Affects:** doc 09 Phase 4.

**Status:** resolved
**Decision:** 2026-05-27 — extended Phase 4 §7.1 deliverable to "Web dashboard memory settings panel" with explicit 3 controls (cross-encoder toggle + model dropdown, consolidation threshold count, temporal decay summary). Same workspace-config backend. Did not create Phase 4.5 — fits cleanly in Phase 4.

---

#### R2.11. `durin reindex` CLI command not explicit deliverable

**Evidence:** doc 02 §5.1 mentions the command in passing; doc 09 Phase 2 doesn't list it explicitly.

**Action proposed:** add to doc 09 Phase 2 deliverables: "`durin reindex` CLI command with progress output."

**Affects:** doc 09 Phase 2.

**Status:** resolved
**Decision:** 2026-05-27 — Phase 2 §5.1 deliverable for `durin reindex` rewritten explicitly: progress output, error handling continues on per-row failures with summary at end, optional `--target lancedb|fts5|all` flag for selective rebuild.

---

#### R2.12. `recent_history` git formatter not explicit deliverable

**Evidence:** doc 05 §5 + doc 06 §4.2 specify the input slot `{recent_history}` (git log of entity). Doc 09 Phase 1 doesn't list the git-log formatter as a sub-deliverable.

**Action proposed:** add to doc 09 Phase 1: "Git history formatter: extract last 30 days of commits per entity, format as short diffs for prompt."

**Affects:** doc 09 Phase 1.

**Status:** resolved
**Decision:** 2026-05-27 — added explicit Phase 1 §4.1 deliverable for "Git history formatter" with implementation hint (~50 LOC: `git log --since='30 days ago' -- <entity_path>` + parse subject + short diff + format compact block).

---

#### R2.13. Absorb-judge prompt versioning not explicit in roadmap

**Evidence:** prompt template exists; doc 09 Phase 1 doesn't list `absorb_judge.md` as a tracked deliverable.

**Action proposed:** doc 09 Phase 1 should list "absorb_judge.md prompt template (updated if needed)".

**Affects:** doc 09 Phase 1.

**Status:** resolved
**Decision:** 2026-05-27 — Phase 1 §4.1 gained explicit deliverable: "`absorb_judge.md` prompt template verified or updated per doc 06 §5; template content matches doc 06 verbatim."

---

#### R2.19. Embedding model deprecation procedure

**Evidence:** doc 02 mentions refusal on model mismatch but no procedure for planned migration.

**Action proposed:** add to doc 02 a small subsection "Model migration procedure": (1) operator runs `durin embed-migrate --to <new_model>`; (2) full reindex with new model; (3) `meta.json::schema_version` bumped; (4) old model identifier preserved in `meta.json::previous_models` for debugging.

**Affects:** doc 02.

**Status:** resolved
**Decision:** 2026-05-27 — added §7.2.1 "Planned migration procedure" to doc 02 with 4-step procedure (backup → update model ID → run `durin embed-migrate` → smoke test). Includes: wipe LanceDB (different dims usually); preserve FTS5 (tokenizer-driven, not embedding-driven); bump schema_version; record previous model in `meta.json::previous_models` for audit trail. Reversal path documented (restore meta.json + memory/.git/ HEAD).

---

#### R2.20. Backup strategy

**Evidence:** `memory/.git/` is local; no remote-push or cloud-snapshot guidance.

**Action proposed:** doc 08 §5 backlog entry "Auto-backup of memory workspace" with trigger condition (e.g., "user enables `memory.backup.enabled = true` in config"); options listed (git remote push, encrypted snapshot to cloud, scheduled local backup directory).

**Affects:** doc 08 §5.

**Status:** resolved
**Decision:** 2026-05-27 — added backlog entry to doc 08 §5: "Auto-backup of memory workspace" with trigger (operator enables `memory.backup.enabled = true`). Until then, the operator can manually `git remote add` and `git push` `memory/.git/` (the workspace IS a normal git repo). For non-git backups, `rsync` or `tar` of `~/.durin/workspace/` works.

---

#### R2.21. Multi-channel user reconciliation

**Evidence:** R4 doc 18 says "cross-system identity has no universal solution; accept manual aliases". Doc 01 §1 Multi-user note acknowledges multiple channels. But the concrete flow ("same user from Telegram + Slack maps to `person:<name>` how?") is undocumented.

**Action proposed:** doc 01 §1 footnote pointing to a future design doc, OR a short subsection §1.1 "Cross-channel identity (current best-effort)" listing what the system does today (default: separate person entities per channel until manually merged via `durin memory absorb`).

**Affects:** doc 01 §1.

**Status:** resolved
**Decision:** 2026-05-27 — added paragraph "Cross-channel identity (current best-effort)" in doc 01 §1 after the multi-user note. Documents: default behavior (separate `person:<name>` entities per channel observation); reconciliation flow (manual or LLM-assisted via `durin memory absorb`); explicit cross-reference to R4 in doc 08 §3 where the accepted-limitation rationale lives.

---

### Round 2 LOW severity (4 items)

#### R2.14. Telemetry events checklist not explicit in Phase 7

**Action:** doc 09 Phase 7 should list all 9 new events as a checklist.

**Affects:** doc 09 Phase 7.

**Status:** resolved
**Decision:** 2026-05-27 — Phase 7 §10.1 now lists 13 events as explicit checklist (more than original 9 — added `memory.silent_retrieval_miss`, `memory.health_check`, `memory.health.critical`, `memory.hot_layer.failure` from later remediations). Each event references its doc 07 subsection. **Updated 2026-05-28 (E7):** `memory.silent_retrieval_miss` removed from the checklist (discarded — doc 08 §2.11); `memory.recall.decay` added (audit A9). Net count unchanged at 13.

---

#### R2.15. v1→v2 backward-compat parsing migration not explicit deliverable

**Action:** doc 09 should list "Schema v2 parser with v1 backward-compat" as Phase 1 deliverable.

**Affects:** doc 09 Phase 1.

**Status:** resolved
**Decision:** 2026-05-27 — added explicit Phase 1 §4.1 deliverable: "v1 → v2 entity-page schema parser: extend `entity_page.py::EntityPage.from_text` to parse v2 fields (`attributes`, `relations`, `provenance`) when present, default to empty when absent. Verify round-trip safety. Tests cover both directions."

---

#### R2.16. Tool descriptions code-vs-doc sync not in any phase

**Action:** doc 09 Phase 4 or 6 should include "Audit and reconcile tool descriptions in code constants vs doc 04 §2.3/§3.3/§4.3/§5.3".

**Affects:** doc 09.

**Status:** resolved
**Decision:** 2026-05-27 — added to Phase 7 §10.1 deliverable: "Tool description code-vs-doc audit script that compares `memory_*.py::DESCRIPTION` constants + `identity.md::Memory` section against doc 04 §2.3/§3.3/§4.3/§5.3. Fails CI on divergence." Placed in Phase 7 because it's about telemetry/observability of consistency. (Could also fit Phase 6 but Phase 7 absorbs documentation-consistency tooling.)

---

#### R2.22. CLI commands consolidated reference missing

**Evidence:** corpus mentions `durin reindex`, `durin dream run`, `durin archive show`, `durin memory absorb`, `durin memory export` (future), etc. No consolidated section.

**Action proposed:** add an appendix to doc 04 listing all operator CLI commands relevant to memory, with one-line description each.

**Affects:** doc 04 (new appendix).

**Status:** resolved
**Decision:** 2026-05-27 — added §11 "Appendix — Operator CLI commands (informational)" to doc 04. Table covers 8 commands (`durin reindex`, `embed-migrate`, `dream run`, `memory absorb`, `archive show/list`, `memory health`, `memory history`) each with purpose + doc reference. Future commands (export, import, forget) explicitly listed as deferred to backlog §5 of doc 08. Renumbered prior §11 (Cross-references) → §12.

---

### Round 2 audit notes (2026-05-27)

- Performed via two parallel agent investigations (code-vs-corpus + roadmap completeness) plus a manual pass for absent-from-corpus concepts.
- 5 HIGH items split: 2 are correctness issues (R2.1 enforcement, R2.2 recovery), 1 is a roadmap omission (R2.8 HotLayer), 2 are absent concepts (R2.17 export, R2.18 deletion).
- 13 MEDIUM items mostly track v2 features (clear path forward, just need explicit cross-refs).
- 4 LOW items are documentation consolidation tasks.

---

## Round 3 audit (2026-05-27, strict implementation-readiness check)

Performed under strict criteria (correctness-or-implementation-blocking only; no documentation polish; not already tracked). Expected at this point to find few gaps or none.

### R3.1. Phase 1.5 dependency wrongly attributed to Phase 1

**Evidence:** `09_implementation_roadmap.md` line 60 + §4b prose (lines ~147-148) state Phase 1.5 depends on Phase 1 ("v2 entity-page schema being in place — Phase 1 deliverable"). Reality: Phase 0 §3.1 line 71 ships the v2 entity-page schema parser. Phase 1 (Dream v2) populates pages with v2 fields but does NOT introduce the parser.

**Why HIGH:** the dependency diagram in §2 is the build-order source of truth. A reader following the roadmap would block Phase 1.5 on Phase 1 unnecessarily, when in fact Phase 1.5 can run as soon as Phase 0 is done — in parallel with Phase 1.

**Action proposed:** correct both prose locations (§2 paragraph after diagram + §4b intro paragraph) to attribute the dependency to Phase 0. Update the diagram so Phase 1.5 hangs off Phase 0 directly, not off Phase 1.

**Affects:** `09_implementation_roadmap.md` §2 and §4b.

**Status:** resolved
**Decision:** 2026-05-27 — corrected both prose locations and the §2 diagram. Phase 1.5 now hangs off Phase 0 directly. Explicit explanation added: HotLayer reads whatever entity pages exist on disk (whether populated by Dream or by manual user edits) and renders the v2 fields it finds; it does not depend on Dream's apply pipeline running first.

### Round 3 audit verdict

**Implementation-ready** modulo R3.1 (now resolved). No other contradictions, no broken cross-references, no implementation-breaking ambiguities. The diminishing-returns pattern is now explicit: Round 1 found 20 gaps, Round 2 found 22, Round 3 found 1. Further audits at this threshold are unlikely to uncover material problems — the next audit should be reactive (triggered by an implementation surprise) rather than proactive.
