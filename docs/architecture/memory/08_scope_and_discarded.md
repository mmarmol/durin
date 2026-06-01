---
title: Scope, non-goals, and discarded approaches
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md
related: all other 0X docs
---

# Scope, non-goals, and discarded approaches

This document records what the memory system **does not** do (and why), what we **tried and abandoned**, and what we explicitly **deferred to backlog**. It exists so future maintainers (humans or LLMs) don't re-attempt patterns we've already evaluated and rejected, and so we have a written rationale when someone asks "why isn't durin doing X?".

**Principle:** every "no" is a decision. Decisions deserve rationale. Without this doc, the corpus reads like a wish-list of what we built; with it, the boundary between MVP and not-MVP is clear.

---

## 1. Non-goals (recap from overview)

Reproduced from `00_overview.md` §2 with one-line rationale per item:

| # | Non-goal | Rationale |
|---|---|---|
| 1 | Not a classical knowledge graph (RDF/SPARQL) | Academic, rigid, hostile to LLMs. We use markdown + indices instead. |
| 2 | Not a reasoning system | Retrieval and structure are our scope; reasoning is the final LLM's job. |
| 3 | Not multi-tenant | Single-workspace per installation. Multiple users interact but memory is shared. |
| 4 | No LLM in hot path | Hot path is deterministic. LLMs only on cold path (Dream, ingestion). |
| 5 | Not a replacement for the context window | We provide material; LLM synthesizes. |
| 6 | No history rewriting | Sessions are immutable; synthesis goes on top. |

---

## 2. Discarded experiments

These were tried (in code or as proposals), evaluated against reality, and removed or rejected.

> **Numeric order note (audit E34, 2026-05-28).** Sub-section numbers are chronological by addition, not strictly sorted: §2.11 appears before §2.10 because audit B9 (which added §2.11) landed before audit A4 (which added §2.10) on the same day's audit pass. External references (doc 07 §4.6 → §2.11, etc.) are stable, so the numbers stay. Readers looking for the most recent discard should scan from the bottom.

### 2.1 G3.b — LLM query rewriter on hot path

**What we tried:** an LLM call before every `memory_search` that generated 5 paraphrases, extracted entities and predicates, and merged results via RRF (`durin/memory/query_rewriter.py`).

**Why it failed:**
- Saturated rate limits. Each agent turn → N searches → N LLM calls just to rewrite.
- The hot path is the wrong place for an LLM operation. It's like "writing to the DB on every SELECT" (user's articulation 2026-05-26).
- Smoke test showed 8/10 initial but bench-scale runs broke z.ai with empty responses + timeouts.

**Lesson:** when considering adding LLM in any frequent path, ask first what upstream weakness the LLM is compensating for. The rewriter compensated for: (a) frontmatter not entering the embedding, (b) 1500-char body truncation, (c) MiniLM-L12 being a small model, (d) no aliases in entries, (e) cross-lingual limits of the embedding model. The right fixes are all upstream (entered MVP via §6.6 doc 02 + alias expansion); the rewriter was a downstream patch.

**Status:** discarded 2026-05-26, module deleted 2026-05-31 (commit `482e2eb`). The post-discard "preserve as library" period accumulated zero callers in 5 days and created false signal of "ready to use"; deleted to match real intent.

The Dream apply v2 work (`05_dream_cold_path.md` §6 / `durin/memory/dream_patch_parser.py`) was the only concrete future user of the module's `_lenient_json_loads` + code-fence helpers; in practice it went with `json_repair` directly (see `dream_patch_parser.py:34`), so the planned extraction never had a real demand.

The `aux_models.memory` config field — originally added to give the rewriter its own model knob — was repurposed (commit `70912b4`) to select the model for both dreams (`05_dream_cold_path.md`, `durin/memory/model_resolve.py`). That's its only role now.

### 2.2 Closed predicate catalog for attributes

**What we tried (proposal stage):** define a fixed catalog of attribute keys (email, phone, lives_in, spouse, works_at, etc.) and force Dream to extract only into this catalog.

**Why it failed:**
- CRM-only worldview. Useless for coders ("file:lives_in" is silly), salespeople ("deal:stage"), support ("ticket:resolution"), students ("class:notes"), makers ("project:materials").
- User feedback 2026-05-26: "necesito pensar esto como una solucion generalista, para estudiantes, coders, makererts, vendedores, soporte, asistente personal, GENERAL."

**Decision:** open attributes + relations (`01_data_and_entities.md` §4). Drift control via existing_schema in Dream prompt (per-entity, not workspace-wide catalog).

### 2.3 Body in LanceDB rows

**What we considered:** storing the full markdown body inside each LanceDB row to avoid disk reads in cold-tier retrieval.

**Why rejected:**
- ~2x storage cost (10MB → 30MB typical for medium workspace).
- Disk reads for the body are <5ms; not the bottleneck.
- User feedback 2026-05-27: confirmed disk reads are acceptable for an assistant.

**Decision:** body NOT in LanceDB. Cold tier reads `.md` on demand (`02_indexing.md` §3.1, §10 #3).

### 2.4 MMR (Maximal Marginal Relevance) for top-K diversity

**What we proposed:** a step in the search pipeline that re-selects top-K balancing relevance and diversity, to prevent top-10 having 5 hits that say the same thing.

**Why deferred:**
- Archive of consolidated episodic (§3.6 doc 01) eliminates the primary source of duplication. Post-archive, the typical pattern is `entity (canonical) + 0-3 fragments + 1 session` — that's triangulation, not redundancy.
- Mainstream systems (mem0, graphiti, hermes, letta, cognee) don't implement MMR.
- Costs: ~50 LOC + tuning λ + test surface.
- Corpus chunks from the same source are addressed differently via per-source cap (§12.4 doc 03).

**Status:** not in MVP. Standalone algorithm, can be added later if bench shows residual duplication.

### 2.5 SQLite structural / analytical index

**What we proposed:** a SQLite table with parsed `attributes` and `relations` columns for analytical queries (COUNT, JOIN, GROUP BY).

**Why discarded** (audit B-5, 2026-05-28, upgrading the original "deferred" classification to "decided against"):

- For N entities < 500 (MVP scale), grep + parse on-the-fly is fast enough.
- FTS5 over rendered frontmatter (from doc 02) covers "find entities with attribute X" queries.
- Adds: another derived index to maintain, schema migration, sync coordination, drift risk if any sync hook fails.
- Mainstream systems (mem0, graphiti, hermes, letta, cognee) **all ship without a structural SQLite layer.** Conscious choice across the field — not a gap in coverage.
- The agent itself is the analytical layer when one is needed: the LLM generates the grep / parse it needs from the .md files at query time. Building a structural index would replace ad-hoc LLM-driven traversal with a separate query language the agent has to learn and the operator has to maintain — wrong direction for an LLM-in-the-loop system.
- Cost when triggered: ~400 LOC + schema migration per workspace + sync hooks on every write path + drift detection. The cost-to-benefit ratio is unattractive even at N > 500 because the alternative (grep + LLM-as-analyzer) scales with the LLM, not with a fixed schema.

**No trigger keeps this open.** Audit B-5 (2026-05-28) reviewed it with the `feedback_stop_soft_deferrals` filter and confirmed there is no observable failure mode whose occurrence would justify the work — "N > 500" is a workspace-size proxy, not a quality signal; "frequent analytical queries" can be served by the agent without the index. The original 2026-05-26 wording "deferred" is upgraded to **discarded**.

**Status:** decided against. Removed from §5 backlog. Cross-corpus decision #1 stands: indices are vector + lexical only.

### 2.6 Pin-by-modality (exact-match hits guaranteed visibility)

**What we proposed:** a mechanism where exact-match hits from grep are pinned to top of their section regardless of RRF score.

**Why rejected (audit B-12, 2026-05-29, upgrading the original "Not in MVP — reconsider if `keywords` is under-used" wording to decided-against):**

- **The auto-keyword detection + RRF boost path already handles the exact-match case.** P3.3 (audit E14, 2026-05-28) shipped `_detect_auto_keywords` in `durin/memory/query_router.py`: it scans the query for email / URL / UUID / file-path patterns and surfaces them in `RoutingDecision.auto_keywords`. The search pipeline then sets `keywords_provided=bool(keywords or decision.auto_keywords)` and the RRF fuser bumps `w_lexical` from 0.7 to 2.5. A query like `marcelo@mxhero.com` does not need the LLM to set `keywords` explicitly — the literal is detected and the lexical boost elevates the exact match naturally.

- **The `keywords` parameter on the tool is the explicit path for everything that auto-detection cannot recognise.** The LLM still has a knob (`memory_search(query=..., keywords="<literal>")`) when the identifier shape is unusual or domain-specific. Pin-by-modality would either bypass this knob (taking the choice away from the agent) or duplicate it (two mechanisms doing the same thing).

- **The pin mechanism carries a real downside.** Pinning exact matches to the top of every section regresses queries where the semantic match is actually better. A query like "founder of durin" should surface `person:marcelo` even if some old episodic literally contains the word "founder" in an unrelated context. Pin-by-modality would prefer the episodic; the current bi-encoder + boosted BM25 prefers the entity page.

- **No mainstream LLM-in-the-loop system implements pin-by-modality.** mem0, letta, hermes, openclaw, graphiti — all rely on weight tuning between dense and sparse paths rather than hard pin rules. The pin pattern lives in classical IR systems where the modality (e.g. "URL search") is a separate UI mode; in an agent that picks its own query shape per turn, pin is the wrong layer.

- **The right response if Phase 8 shows exact-match misses is tuning the existing knob, not a new architecture.** `DEFAULT_W_LEXICAL_BOOSTED = 2.5` is configurable; if `marcelo@mxhero.com` queries still miss at 2.5, the operational fix is to lift the value (or expose it in config) — not to add a parallel pin path. Same pattern as B-10 (SPLADE/ColBERT was the wrong layer; the right answer was "swap the dense model").

**Status:** decided against. The original "reconsider if bench shows `keywords` is under-used" wording is removed — the auto-keyword path means `keywords` no longer carries the whole burden, and bench evidence would tune the boost weight rather than introduce pinning. Removed from §5 backlog.

### 2.7 Cross-encoder ON by default

**What we initially proposed:** cross-encoder reranker enabled by default in MVP.

**Why changed to opt-in:**
- All multilingual cross-encoder models add 300-1500ms latency on CPU. Default-on breaks the search budget.
- Comparable systems (mem0 opt-in, graphiti opt-in) ship the same way.
- The default RRF + entity-aware rerank already produces useful retrieval.

**Status:** in MVP as opt-in, OFF by default. Default model when enabled: `BAAI/bge-reranker-base` (~100M, MIT, lower RAM); `jinaai/jina-reranker-v2-base-multilingual` remains a curated alternative the operator can configure.

---

### 2.8 `memory_ingest` URL fetch + inline content branches

**What we initially proposed** (doc 04 §4.1 v1, doc 06 §3.3 v1): `memory_ingest(source=...)` accepting three kinds of source — local file path, URL, or the literal `"inline"` with a separate `content` parameter. Auto-chunking either way.

**Why removed:**
- **URL fetch would duplicate `web_fetch`.** `durin/agent/tools/web.py::WebFetchTool` already handles URL → markdown extraction with the hard parts done well: Jina Reader primary, readability-lxml fallback, SSRF protection (resolved-IP allowlist on every redirect via `durin/security/network.py::validate_url_target`), image passthrough, content-type sniffing, 15-30 s timeouts, max redirects, `_UNTRUSTED_BANNER` marker on returned text. Reimplementing those policies inside `memory_ingest` would either (a) duplicate the code and drift, or (b) call `web_fetch` internally — at which point exposing URL as a `memory_ingest` parameter just hides a two-step workflow behind a flag.
- **Inline content is `memory_store(class_name="corpus")`.** When the agent already has the text in context, persisting it goes through `memory_store` with no extra surface area. The only `memory_ingest`-exclusive capability would have been auto-chunking, which is separable — `durin/memory/text_splitter.py::split_text` is a public function any caller can use.
- **`memory_ingest`'s remaining value is local-file-specific.** It preserves the original artifact under `ingested/<id>/source.<ext>` (idempotent by content hash) and chunks the parsed content into `memory/corpus/*.md` entries. Both of those are only meaningful when there is an original file on disk — for web/inline content there is no "original" worth preserving as a separate artifact.

**The composition rule:**

| Workflow | Tools |
|---|---|
| User drops a markdown file in a folder, asks the agent to remember it | `memory_ingest(path=...)` |
| Agent finds an article on the web that should be remembered | `web_fetch(url=...)` → `memory_store(content=markdown, class_name="corpus", source_refs=[url])` |
| Agent has text already in context that the user wants persisted | `memory_store(content=..., class_name="corpus")` |

**Genealogy / lesson** (so the same error doesn't repeat): the URL/`inline` branches lived in the prospective spec (doc 04 + doc 06) before the tool was implemented. When the tool descriptions were "synced" to the doc in commit `572d5cf` (P6.3, 2026-05-28 09:28 +0200), the descriptions were copied verbatim *without verifying the schema implemented the promised parameters*. The drift was caught the same morning during the audit (`bce9092`, ~1 hour after the sync commit), so the LLM-facing lie was short-lived — but only by accident. The `test_tool_description_sync.py` test passed throughout because it compares strings, not behaviour. **The fix for "sync" tests in general: exercise the behaviour, not just the string.**

**Status:** `memory_ingest` accepts only `path` (local file). URL and inline workflows go through `web_fetch` + `memory_store(class_name="corpus")` respectively. Recorded in doc 11 reconciliation A1.

---

### 2.9 `memory_store` parameter surface — `valid_from` and `pending` class

**What we initially proposed** (doc 04 §3.1 v1):
- `valid_from` as an optional ISO-date parameter on the tool ("for time-bound facts").
- `class_name` enum recorded as `stable | episodic` (trimmed from the 4 actual values of `MEMORY_CLASSES`).
- `body` (vs `content`) as the param name.
- `headline` required (vs auto-generated).
- `force` undocumented (despite shipping in commit `d34b337`).

**What survived audit A2:**

1. **`valid_from` NOT exposed as tool parameter.** Verified facts:
   - The field IS a real part of `MemoryEntry` ([`schema.py:40`](../../durin/memory/schema.py)) — declared in doc 01 §3.3.
   - `store_memory(*, valid_from=date | None)` accepts it and defaults to `date.today()` when omitted ([`store.py:90`](../../durin/memory/store.py)).
   - Used downstream by hot_layer cursor compare, entity_ranker pre/post-cursor logic, fragment sort, and search result display.
   - The one consumer that actually needs to back-date — the LoCoMo benchmark harness ([`scripts/benchmark/locomo_harness.py:227-233`](../../scripts/benchmark/locomo_harness.py)) — calls the pure `store_memory` function directly, NOT the tool, because seeding 1000s of turns through the agent loop would be orders of magnitude slower.
   
   So the tool-facing surface stays minimal: 99% of agent-in-conversation stores observe facts "now", `date.today()` is correct. The 1% back-dating case is either (a) seeding from outside the agent (handled by the pure function) or (b) post-hoc `.md` edit by the user (the file watcher under P2.3 reindexes the change). Adding a `valid_from` parameter to the tool would expose a knob the LLM would default-fill 99% of the time, while the legitimate back-date workflow already has its path.

2. **`pending` excluded from the agent-facing class enum.** The exclusion is structural, not aesthetic: `MEMORY_CLASSES` has 4 values, but `paths.py::walk_memory` skips `memory/pending/**`, `indexer.py` skips it for FTS + LanceDB, and `file_watcher.py` skips it for reindex. An entry written to `memory/pending/<id>.md` is invisible to every retrieval path. Letting the LLM write there would be silent data loss. The tool's enum is now `["stable", "episodic", "corpus"]`; internal callers that legitimately use `pending` (e.g. compaction intake) keep working via the pure `store_memory` function.

3. **`body` is the persisted field, `content` is the tool parameter.** Doc 04 §3.1 v1 conflated the two planes. The asymmetry is deliberate: the LLM action is "store this content"; the persisted entry has a `body` field. We document the mapping (doc 04 §3.1 v2 calls it out) and keep `content` as the parameter name.

4. **`headline` stays optional.** Auto-gen via `_auto_headline = " ".join(words[:10])` ([`store.py:106-109`](../../durin/memory/store.py)) is functional for LLM-generated content (the model tends to lead with the topic sentence). Forcing `required` would add latency to every store call to no clear benefit; if the agent has a sharper headline in mind, it can pass one.

5. **`force` is documented.** Added in commit `d34b337` for the dedup near-duplicate flow (cosine ≥ 0.95 returns a warning; `force=true` overrides). Doc 04 v1 omitted it by oversight.

**Lessons** (so the same errors don't recur):

- **Enum values can be traps.** Don't blindly mirror an internal constants tuple into a tool-facing enum without checking whether the rest of the system honors all members. `pending` was in `MEMORY_CLASSES` but excluded from every retrieval pipeline — a write-only black hole.
- **Tool param name ≠ persisted field name.** When they differ, document both planes explicitly so future readers don't conflate them like doc 04 v1 did.
- **Default behavior often beats new tool params.** Before exposing a knob, ask: who actually needs it? If the answer is "an internal pipeline" (LoCoMo seeding via the pure function), the tool surface stays clean.

**Status:** the tool schema is now: `content` (req) + `class_name` ∈ {`stable`, `episodic`, `corpus`} + `headline`/`summary`/`source_refs`/`entities`/`force` (opt). `valid_from`/`pending` deliberately not exposed. Recorded in doc 11 reconciliation A2.

---

### 2.11 `memory.silent_retrieval_miss` heuristic detection (audit B9)

**What we initially proposed** (doc 07 §4.6 v1): a telemetry event emitted in turn N+1 when the agent's turn-N response didn't include `memory_search` AND the user's next message looks like a re-ask, negation, or correction. Three heuristics for detection:

1. Substring overlap > 60% with turn N's user message.
2. Starts with negation tokens (`no,`, `wrong,`, `actually,`).
3. Contains correction patterns (`I said X, not Y`, `you forgot…`).

Aggregate metric `silent_retrieval_miss_rate` would gate activation of §2.F (eager pre-fetch).

**Why discarded** (not deferred — see lesson below):

- **Heuristic (1) is cross-lingual but unreliable.** Substring overlap doesn't require parsing the language, but legitimate refinement turns ("OK and what about X?", "Same thing but for project Y") have high overlap with the prior message. The false-positive rate would drown the signal.

- **Heuristics (2) and (3) are inherently English-shaped.** The token lists (`no,`, `wrong,`, `actually,`) and the correction patterns (`I said X, not Y`) don't translate — Chinese puts negation markers adjacent to the verb (not at the start of the sentence); Japanese / Korean use sentence-final particles in positions that "starts with" doesn't catch. Multi-lingual maintenance would require per-language detector code (or a per-language LLM classifier — see next bullet). Durin's LoCoMo seed uses CJK plus Spanish; English-only detectors would miss those workloads entirely.

- **An LLM classifier breaks the telemetry budget.** The only path to language-agnostic detection of "this is a correction" is a small LLM judge — but every user turn would incur an extra LLM call just to compute a telemetry metric. Telemetry events that cost per-turn LLM calls are a different category from the cheap structured-event stream the rest of `07_telemetry_and_observability.md` describes.

- **The consumer (§2.F) is deferred too.** Even a reliable signal wouldn't have a consumer until §2.F ships (`08_scope_and_discarded.md` §4.1). Shipping the detector now would produce data with no decision to feed.

**What we'd do instead if a future use case actually surfaces:**

- LLM-based classifier as a background pass over recent turns (not per-turn). Same accuracy, amortised cost.
- Explicit user-feedback signals (thumbs-up / thumbs-down, retry button) — slower data collection but unambiguous signal.
- Post-hoc analysis on bench traces (LoCoMo / EverMemBench) — controlled environment, ground-truth labels.

**Lesson** (so the same pattern doesn't recur): heuristic detectors with language-specific token lists are a red flag for any subsystem that has to serve multi-lingual workloads. If the spec's "detection rule" looks like a literal pattern match against English idioms, ship the LLM judge or skip the feature — don't ship the English detector and hope it generalises.

**Status:** event removed from doc 07 §4.6 — that section now points here. The TypedDict was never created; `EVENTS` registry does not include the event. If a future change re-introduces it under a different design, that change should also restore the §4.6 documentation with the new approach.

---

### 2.12 `durin archive show / list` CLI commands

**What was proposed**: dedicated operator-facing CLI subcommands to inspect archived content from the shell, listed alongside `durin memory reindex`, `durin memory dream run`, `durin memory absorb` etc. as part of the operator-facing CLI suite (doc 04 §11). Audit F2 (2026-05-28) initially marked them "deferred to backlog with concrete-operator-workflow trigger".

**Why we are not implementing them** (audit G2, 2026-05-28 — correcting the F2 defer): the recovery surface is already saturated by three other paths and a dedicated CLI command would be redundant work without a unique use case:

1. **Agent-visible recovery**: `memory_search(scope='archive')` (shipped audit F2) walks `memory/archive/**` on demand, parses each entry, returns results to the agent. The LLM can recover archived content semantically without the operator typing a command.

2. **Per-entity recovery**: `durin memory expand <entity>` (shipped earlier) renders the canonical entity page plus its archived predecessors. Exactly the case "show me what was consolidated into person:marcelo".

3. **Direct lookup**: `cat memory/archive/<class>/<id>.md` and `find memory/archive -name '*.md'` are standard shell idioms. An operator running CLI commands already speaks shell; a dedicated `durin archive show <uri>` would only save typing the relative path.

**The concrete trigger that would change this**: an actual operator workflow where (1) and (2) and `cat`/`find` are demonstrably worse than a dedicated command. We have not seen one. The F2 "deferred until concrete operator workflow surfaces" wording was effectively a way to keep the items on the to-do list as a soft promise — but with no failure mode that would produce that workflow, the items would have sat in the backlog indefinitely. Honest classification: discarded.

**Lesson** (recorded in personal memory, [feedback-stop-soft-deferrals](../../../../.claude/projects/-Users-marcelo-git-personal-durin/memory/feedback_stop_soft_deferrals.md)): "deferred until concrete trigger" without a written failure mode is the same as discarded — except it leaves a phantom to-do that returns each audit pass. When a feature is covered by existing surfaces and has no unique use case, mark it discarded with the reasoning, not deferred.

**Status**: removed from doc 08 §5 backlog (was added in F2, removed in G2). Doc 04 §11 lists them as "not implemented — covered by …" instead of strikethrough deferred. Doc 01 §3.6 + §10 row 4 updated to point at the three existing surfaces.

---

### 2.13 `existing_uris_cap` Dream-prompt config knob

**What was proposed**: lift `DEFAULT_EXISTING_URIS_CAP = 100` (in `durin.memory.entity_inventory`) and the parallel `_EXISTING_URIS_CAP = 100` (in `durin.memory.dream_prompt_builder`) into config so an operator could tune how many recent entity URIs land in the Dream consolidator's prompt to discourage duplicate entity creation. Audit F17 (2026-05-28) deferred this with the rationale "Hard-coded; lifting it into config is straightforward if operators with very large workspaces ask."

**Why we are not implementing it** (audit G4, 2026-05-28 — correcting the F17 defer):

1. **No failure mode would produce the ask.** Duplicate entity creation is invisible to operators: the Dream LLM either creates a duplicate or does not, and the cap is one of many inputs (also: the page content, the entries being consolidated, the model temperature). There is no telemetry that measures "duplicate avoided thanks to existing_uris signal", so operators cannot detect "cap too low" empirically.

2. **The 100-most-recent is a strong signal where duplicates actually happen.** Duplicate creation occurs typically around recently-active entities (the LLM sees `person:marcelo` in a fragment and emits a new `person:marcelo_marmol` because it doesn't realise the existing slug). Old, forgotten entities are not where duplicates come from — and raising the cap to 200+ pulls more old entities into the prompt without addressing the typical failure.

3. **Two caps in series.** `entity_inventory` and `dream_prompt_builder` both cap at 100. Lifting just the producer to config silently leaves the renderer's cap in effect; lifting both requires coordinated config + threading from `DreamConsolidator._build_prompt` to two modules. The work-to-benefit ratio is poor without a concrete trigger.

4. **The operator who really needs to tune can patch the constant.** It is one line of code in `entity_inventory.py`. The defer would replace a one-line patch with ~60 LOC of config plumbing and a TDD surface, for a knob no telemetry would tell the operator to turn.

**The concrete trigger that would change this**: telemetry that tags duplicate entity creations as such and attributes them to "missed in existing_uris signal" — i.e., a dashboard showing "N duplicates created last week, K of them had the canonical in the workspace but outside the top-100 by mtime". We have neither the telemetry nor any signal that the rate is non-zero.

**Status**: removed from the deferred list. Doc 06 §2 (existing_uris slot) and doc 05 §5.1 row drop the "lift to config when asked" note. F17 entry in doc 11 annotated with this G4 correction.

---

### 2.10 `body` column in LanceDB (P2.5, reverted by A4)

**What we briefly did**: commit `a266344` (P2.5, 2026-05-28 09:10) added a `body` column to the LanceDB row schema so cold-tier queries (`memory_search(level="cold")`) could return full bodies without N disk reads. The commit message framed it as a "trade-off explicit": doubled index size in exchange for eliminating per-cold-hit file opens.

**Why we reverted it (audit A4, same day)**:

1. **Violates the architectural principle of single source of truth.** The `.md` on disk is canonical; LanceDB is a derivable, disposable cache that can be rebuilt at any time. Storing the body in LanceDB makes the cache hold content the disk also holds — and now there are two places to keep consistent.

2. **The optimisation was not measured against a bottleneck.** Cold-tier disk reads are ~5-10 ms total for 10 hits on a modern SSD. The next LLM call downstream takes seconds. P2.5 saved ~10 ms in an operation that is otherwise gated on multi-second latency. Premature optimisation per `feedback_no_wait_and_measure`.

3. **Drift window.** The file watcher (P2.3) reindexes `.md` changes asynchronously. Between a `vim` edit and the next reindex tick, LanceDB returns a stale body. Without P2.5, the body is always read from disk via `_enrich_body` at query time, so the freshly-edited version is always what the LLM sees.

4. **Doubles index size at scale.** A 10k-entry workspace with average body 1500 chars (corpus chunks) gains ~30 MB; 100k entries gain ~300 MB. Not catastrophic but consistent with the broader pattern of "let's just store it again, disk is cheap" that leads to multi-source confusion later.

5. **Breaks symmetry with FTS5.** FTS5 indexes the composed `text` (headline + summary + entities + body) for BM25 ranking, but it never returns `text` in query results — consumers go to disk for the content. The two indices were aligned on that principle; P2.5 broke it for LanceDB alone, with no corresponding gain over FTS5's approach.

**What we kept (the legitimate small metadata in LanceDB)**: `id`, `class_name`, `summary`, `headline`, `valid_from`, `entities`, `path`. Each is small (kilobytes per row at most), each is needed during retrieval (warm-tier results, ranker inputs, sort keys, hit attribution). Reading them from disk per query would be the real bottleneck. The body is different — it can be megabytes, it's only needed in cold-tier queries, and disk reads are fast.

**The implementation**:
- `vector_index.py` no longer writes `body` to the row dict (entries or entity pages).
- `search_pipeline._resolve_meta` no longer threads `body` from vector hits.
- `_cross_encoder_rerank` uses `snippet`+`headline`+URI for its (uri, doc_text) pairs instead of body. If benchmarks ever show that snippet-only CE rerank is materially worse, the fix is a CE-specific top-N body fetch inside the rerank function — NOT a column in LanceDB.
- `CURRENT_SCHEMA_VERSION` bumped 2 → 3 so v2 LanceDB tables (carrying the now-orphaned `body` column) trigger a clean rebuild on the next `MemorySearchTool.execute` via `ensure_index_fresh` (P2.2).
- `tests/memory/test_vector_index_no_body_column.py` asserts the post-A4 invariant — if a future change reintroduces the column, that test fails loudly.

**Lesson** (so the same mistake doesn't recur):

- **An optimisation that violates an architectural principle must be justified by measurement, not intuition.** "Avoids N disk reads" sounds compelling but is meaningless without knowing whether those disk reads were the bottleneck. They weren't.
- **The fix for a slow consumer is local to that consumer, not a schema change.** If cross-encoder rerank is slow when reading body from disk, optimise the CE step (top-N fetch, async I/O, caching). Don't add a body column to LanceDB that 95% of queries don't need.
- **Symmetry between indices is a feature.** FTS5 and LanceDB both being "metadata + index, content on disk" makes the system easier to reason about. Breaking symmetry in one place leaks complexity everywhere.

---

### 2.14 `summary` slot in entity-page embedding text

**What was proposed**: insert a Dream-generated `summary` between `rendered_frontmatter` and `body` in the entity-page embedding text, intended to replace the truncated body in the centroid when body exceeded the 1500-char budget. Audit E9 (2026-05-28) deferred it with the wording "if bench shows recall regression on long-body entity pages, restore the slot."

**Why we are not implementing it** (audit G6, 2026-05-28 — closing the E9 defer): the defer was built on a factual error and the alternative paths cover the use case.

1. **The data model does not support the slot.** `EntityPage` has no `summary` field; Dream produces zero summaries for entity pages today. The E9 wording "Dream produces a `summary` attribute only sometimes" was wrong — Dream never produces one, because the dataclass does not accept it. Shipping the slot would require modifying `EntityPage`, the Dream prompt, the `dream_apply` logic, and the composer — not "restoring" a feature that was there.

2. **Only the vector path is bounded by the 1500-char cap.** FTS5 indexes the full composed text without truncation (doc 02 §5.2 "BM25 text truncation: None"); the grep fallback reads the file from disk. A query whose match is at char 7000 of a 10000-char body is found by the lexical path and by grep. The canonical surfaces in the result set even when the vector centroid does not have those tokens.

3. **G6 closed the body-recovery loop**. Pre-G6 `drill()` could not resolve the canonical URI `memory/entity_page/<type>:<slug>` that `memory_search` emits; every canonical hit was undrillable. G6 fixed that. The agent that gets a canonical hit with a truncated snippet now drills to the full body in one tool call. The `summary` slot would have addressed a symptom that G6 cured at the source.

**The concrete trigger that would change this**: a Phase 8 bench result showing recall regression on entity pages with body > 1500 chars, AND failure-trace attribution to "vector path missed because canonical centroid lacked tokens" (i.e. FTS5 + grep also failed to surface the page). No bench evidence supports this; the corner case where all three paths fail is narrow.

**Estimated implementation cost when triggered** (so a future audit does not re-litigate the size of the work): add `summary: str = ""` to `EntityPage`; modify the Dream consolidator prompt and apply path to emit and persist it; modify `_compose_entity_page_text` to substitute body for summary when summary is present; bump `CURRENT_SCHEMA_VERSION` and force a rebuild. Roughly 100-150 LOC plus reindex.

**Status**: removed from the deferred list. E9 entry in doc 11 annotated with the G6 reclassification. Doc 02 §4.2 carries the full "decided against" reasoning so the next audit pass does not silently re-propose it without new evidence.

---

### 2.15 Full unification of `hot_layer` and `sectioned_output` renderers

**What was proposed**: collapse the two renderers in `durin/memory/` that produce `=== CANONICAL: ... === END CANONICAL ===` blocks into a single module. Audit F4 (2026-05-28) unified the two renderers used by `memory_search` (`Result.render_block` and `sectioned_output._render_block`), then noted in passing that `hot_layer._render_canonical_block` is a third renderer and was "deferred — different use case".

**Why we are not implementing it** (audit G7, 2026-05-28 — closing the F4 defer): the two renderers are intentionally separate because they serve different consumers with structurally different inputs and outputs.

| Aspect | `hot_layer._render_canonical_block` | `sectioned_output._render_block` |
|---|---|---|
| When invoked | Eager pre-injection into every agent prompt | Lazy search-result rendering for `memory_search` |
| Input data | `EntityPage` dataclass with full structure | `SectionedHit` — a search-hit row |
| Inner content | `<name> (aliases: ...)`, `Attributes: k1 is v1; k2 is v2.`, `Relations: <type> of <to> (since N).`, body excerpt | summary > body > snippet preference, optional `Entities: ...` tail |
| Body cap | `_CANONICAL_BODY_PER_PAGE` (200 chars) — tight, hot-layer budget | typically the full hit body up to caller's budget |
| Purpose | "Here is the canonical state of this entity — ground truth" | "Here is a search hit — title + summary" |

Forcing either into the other's shape produces a regression: the search renderer would gain attributes/relations noise per hit (waste — the agent does not need a per-relation breakdown on every search result), and the hot layer would lose the structured page representation (the `Attributes: k1 is v1; ...` line is what makes the eager-injection useful as ground truth).

**What we DID share** (audit G7): the marker convention itself — the `=== KIND: <ref> ===` and `=== END KIND ===` strings — moved to a single source of truth at `durin.memory.section_markers`. Both renderers now call `canonical_marker(ref, ts=...)`, `fragment_marker(path, ts=...)`, etc. instead of building the strings independently. This eliminates the drift surface (~20 LOC of helper code) without merging the renderers' body logic.

**The concrete trigger that would change this**: a documented case where the agent's behaviour materially benefits from the hot-layer-style structured rendering inside a search result (or vice versa). We have not seen one and the use cases point in opposite directions.

**Estimated cost of full unification when triggered** (so a future audit does not re-litigate the size): introduce a unified `render_block(mode: Literal["search", "hot_layer"], ...)` with mode-specific body composition. ~150-200 LOC. Doubles the test surface because every behaviour now has to be re-verified under both modes. The G7 marker-helper refactor (~20 LOC) gets us most of the drift-protection benefit at ~10% of the cost.

**Status**: removed from the F4-deferred list. F4 entry in doc 11 annotated with the G7 reclassification. The shared marker helper lives at `durin/memory/section_markers.py`; if a future change touches marker format, that one file is the source of truth.

---

### 2.16 `commit_sha` field in `memory.dream.patch_applied`

**What was proposed**: include the git commit SHA produced by `dream.py::apply()` in the `memory.dream.patch_applied` telemetry event payload. The v1 spec listed it as one of the canonical fields. Audit F8 (2026-05-28) dropped it from doc 07 §6.5 with the rationale "telemetry should not couple to git internals; dashboards join via `entity_ref + cursor_after`."

**Why we are not implementing it** (audit G8, 2026-05-28 — correcting the F8 reasoning, not the F8 conclusion):

1. **The F8 join-via-trailers argument was fragile.** The proposed join — query `memory.dream.patch_applied` events for `(entity_ref, cursor_after)` pairs, then scan `memory/.git/log` for commits whose `Entities-touched:` and `Cursor-after:` trailers match — requires shell access to `memory/.git/`, brittle commit-message parsing, and breaks when two entity touches in the same Dream pass share a cursor. The "you can just join" framing was hand-waving.

2. **But the F8 conclusion still holds for a different reason.** The realistic consumers of `commit_sha` are:

   - **Operator forensics** ("when did Dream change this page?"): runs `git log memory/entities/<type>/<slug>.md` directly. The file path is known from the event's `entity_ref`. Operator does not need the SHA in telemetry.
   - **Audit / change review** ("show every Dream change to `person:marcelo` last week"): runs `git log --since=1week memory/entities/person/marcelo.md`. Same answer; the trailers in commit messages already carry `Trigger`, `Sources`, `Cursor-after`. Operator does not need the SHA in telemetry.
   - **Debug dashboards** ("dream commit latency p95 by entity"): genuinely benefits from `commit_sha`, but no such dashboard exists and nobody has asked for the metric. The use case is hypothetical.

3. **The implementation cost is non-trivial because of ordering.** `_emit_apply_telemetry` fires INSIDE `dream_apply.py` (line 119) BEFORE the git commit happens in `dream.py::apply()` (line 630). To emit a SHA, the telemetry call would have to move to `dream.py::apply()` after the commit returns — ~50 LOC of restructuring, plus a new dependency on commit success (or a separate "commit failed but apply succeeded" variant event). The cost is not enormous but it serves nobody.

4. **The path forward if the debug dashboard ever materialises** (so a future audit does not re-litigate the work): introduce a new event `memory.dream.commit_recorded` fired from `dream.py::apply()` after `repo.commit(...)` returns, carrying `entity_ref + commit_sha + duration_from_apply_emit_ms`. Joining the new event to `memory.dream.patch_applied` on `(session_key, iteration, entity_ref)` gives the dashboard everything it needs without restructuring the existing apply telemetry path.

**The concrete trigger that would change this**: a documented dashboard or telemetry consumer that needs commit SHAs at scale (every commit, not on-demand). Until that consumer exists in code or in a written operational ask, the cost is for nobody.

**Status**: removed from the spec. Doc 07 §6.5 already drops the field per F8; G8 only corrects the rationale in doc 11 F8 entry so the next audit pass sees the operator-forensics-uses-git-log reasoning rather than the fragile join argument.

---

### 2.17 Cross-encoder reranker default ON

**What was proposed**: flip `MemorySearchConfig.cross_encoder.enabled` from `false` to `true` so the cross-encoder rerank step runs by default for every search. Doc 08 §5 backlog listed the trigger "Bench shows opt-in OFF is significantly worse" — earlier wording without the tight form.

**Why we are not implementing it** (audit B-2, 2026-05-28):

1. **Default OFF is the right choice for the system's primary deployment shape.** durin is a personal assistant running on the operator's own hardware (laptop / small server). Cross-encoder adds 300-1500 ms p95 latency in CPU and additional resident memory for the model — the shipped default `BAAI/bge-reranker-base` (~100M, lower RAM) is lighter than the curated `jina-reranker-v2-base-multilingual` alternative (278M, ~1.1 GB), but the latency cost alone still justifies opt-in. Default ON would break the search budget (~30-130 ms p95 today, doc 03 §13) and the RAM budget for embedded / edge deployments without giving the operator a choice.

2. **The configurability the operator needs already exists.** Three surfaces let the operator turn the cross-encoder on without code changes:
   - **Onboarding wizard** asks at install time whether to enable cross-encoder (with the latency + RAM cost stated explicitly).
   - **Web dashboard** Memory Settings panel (audit P4.4) has a toggle that writes `memory.search.cross_encoder.enabled` and a model dropdown for the supported reranker IDs.
   - **Workspace config** `~/.durin/config.json` (or TOML equivalent) accepts the same field for shell-based setup.

3. **No use case requires the default to flip.** An operator who values quality over latency turns it on via any of the three surfaces during setup or later. An operator who values latency keeps it off. Flipping the default would force the latency cost on operators who never asked for the quality bump, and would surprise users upgrading from a prior version with a latency regression they did not opt into.

**What this resolves**: the doc 08 §5 backlog row "Cross-encoder default ON" is removed. The original "bench shows opt-in OFF is significantly worse" trigger was hand-waving — even a real bench gap does not justify forcing latency on operators who explicitly chose the lower-latency mode. The decision is structural (operator choice), not empirical (bench number).

**Status**: removed from §5 backlog. Default stays OFF. If a future deployment shape (e.g. always-on server with quality SLA) wants a different default, that ships as a separate config profile, not a global default flip.

---

### 2.18 Temporal decay enabled for more classes by default

> **Superseded 2026-05-30**: temporal decay was removed entirely from the search pipeline (see doc 03 §10 for the removal rationale, triggered by LoCoMo conv-5-q20). The discussion below is preserved as historical context — the "extend defaults vs keep per-class" debate became moot once the whole step was removed. Search no longer pre-judges recency; the LLM does it from `valid_from` on each hit.

**What was proposed**: extend the default `CLASS_HALF_LIFE_DEFAULTS` in `durin/memory/decay.py` to apply decay to `stable`, `entity` / `entity_page`, and `corpus` — classes that currently carry `None` (no decay). Doc 08 §5 listed the trigger "Workspace > 1 year old shows obsolete-info regressions".

**Why we are not implementing it** (audit B-4, 2026-05-28):

1. **The current per-class default reflects the semantic of each class, not an oversight.**
   - `stable` carries explicit user-asserted facts ("my email is X") that do not get stale by age — they get invalidated by explicit contradiction. Aging them out by time would erase persistent ground truth.
   - `entity` / `entity_page` are canonical entity pages that Dream UPDATES on every consolidation pass; old pages are consolidated, not stale. Aging them out would penalise the most-distilled signal in the system.
   - `corpus` is ingested documents (PDFs, articles, research notes). A PDF does not become stale by elapsed time — it becomes stale when the operator removes it or when a newer document supersedes it. Time is the wrong axis.
   - `episodic` (90 d) and `session_summary` (120 d) DO decay because they represent recent observations and recent conversations, both of which lose relevance as they age out of working memory.

2. **The configurability the operator needs already exists.** Audit G1 (2026-05-28) shipped `memory.search.temporal_decay.class_half_life_overrides` — an operator with a multi-year workspace who wants `entity_page` to decay over two years sets `{"entity_page": 730}` in config. The override accepts integers (days) for any class and `null` to disable for a class. The "deferred to enable for more classes" item is effectively about flipping the default; the per-workspace knob is already operator-accessible.

3. **The trigger as written is not measurable.** "Workspace > 1 year old shows obsolete-info regressions" requires defining what "obsolete" means per query. LoCoMo's seed is < 1 year, so Phase 8 cannot evaluate it. No telemetry tracks "I retrieved an obsolete fact" because the system has no way to know the fact is obsolete unless the user contradicts it explicitly — and at that point the operator either edits the page or sets the override.

**What this resolves**: the doc 08 §5 backlog row is removed. The decision is structural (the per-class defaults match the semantics of each class), not empirical. The operator who wants a different decay shape uses G1's override field — same pattern as B-2 (cross-encoder default).

**Status**: removed from §5 backlog. Defaults stay as A9 set them. If a future class is added to `MEMORY_CLASSES` with a different decay semantic, its default goes into `CLASS_HALF_LIFE_DEFAULTS` at that time.

---

### 2.21 Auto-backup of memory workspace

**What was proposed**: a system-managed backup mechanism for `~/.durin/workspace/` — push `memory/.git/` to a remote, encrypted snapshot to cloud, or scheduled local backup directory. Doc 08 §5 row's trigger was "operator enables `memory.backup.enabled = true` in workspace config" — a config field that does not exist; the trigger was effectively "if we ship this feature, the operator can use it", which is a tautology.

**Why we are not implementing it** (audit B-18, 2026-05-29):

- **The workspace IS a normal git repo.** `memory/` contains a `.git/` directory. An operator who wants off-host durability runs `git remote add origin <url>; git push origin main` once. That is the entire feature, and it ships in every git installation already. Building a parallel `memory.backup.enabled` mechanism would replace a documented git remote with a custom configuration surface that does the same thing through a less standard interface.

- **For non-git backups, standard shell tools cover every backup shape an operator might want.** `rsync -av ~/.durin/workspace/ /backup/durin/` for incremental copies. `tar czf durin-$(date +%Y%m%d).tar.gz ~/.durin/workspace/` for snapshots. `restic backup ~/.durin/workspace/` for deduplicated encrypted backups against any S3-compatible store. None of these need durin to know about backups; durin's job is to keep the workspace consistent, which it already does.

- **The trigger is tautological.** "operator enables `memory.backup.enabled = true`" is the feature itself. There is no observable failure mode — no telemetry detects "operator did not back up". The only signal that would justify shipping is the operator asking for it. They have not.

- **Adding auto-backup to the system introduces failure modes the system did not have.** Scheduled push to remote means: handling network failure (retry policy), credential management for remote stores, recovery from corrupted backup state, "backup latest-success" telemetry. Every one of those is a new failure surface for a workflow git push handles inline.

- **Mainstream LLM-in-the-loop systems do not ship managed backup tiers.** mem0, letta, hermes — the operator is responsible for their own data durability. This is the right division of concerns: the system maintains the working state, the operator maintains the backups.

**Status**: decided against. Removed from §5 backlog. The workspace's git repo is the supported portability and durability mechanism; `rsync` / `tar` / `restic` cover non-git workflows; no system-managed backup tier in MVP or post-MVP.

---

### 2.20 Data deletion (GDPR-like cascading delete)

**What was proposed**: a "forget everything about `person:X`" operation that cascades a delete across the entity page + archived predecessors + episodic mentions in other entities + provenance references in unrelated entities + LanceDB rows + FTS5 entries + git history rewriting. Doc 08 §5 backlog row tied the trigger to "exposing durin to external users via channels" — Telegram/Slack bot, beta release, or right-to-be-forgotten jurisdiction.

**Why we are not implementing it** (audit B-17, 2026-05-29):

- **durin is single-operator by architecture.** Doc 00 §2 non-goal #3: "Not multi-tenant. Single-workspace per installation. Multiple users interact but memory is shared." The operator owns the workspace; there is no second user whose data lives in durin and whose right-to-be-forgotten the operator would have to honour. The trigger "first external user interacts via Telegram/Slack bot" presupposes a deployment shape (multi-tenant, hosted, user-segregated memory) that durin is explicitly not built for. Implementing GDPR cascading deletion would commit to a deployment shape the system was designed away from.

- **The git history honesty problem is unsolvable in the proposed form.** Cascading delete that "honestly handles git history" means rewriting `memory/.git/` — `git filter-repo`-class operations that change every commit hash from the touched point forward, break any external clone of the workspace, and silently corrupt anyone with a reference to a pre-rewrite SHA. The operator can do this for their own workspace if they accept the corruption, but the system enforcing it would propagate the corruption to anyone the operator had shared the workspace with — exactly the kind of irreversible action P7 ("Reversible decisions; Archive instead of delete") was written to prevent.

- **The triggers conflate three different deployment changes that need separate decisions.**
  - Telegram/Slack bot exposure: each user IS the operator interacting from a different surface, not a tenant whose data is segregated from the operator's. The bot use case does not introduce a GDPR subject.
  - Right-to-be-forgotten jurisdiction (EU GDPR, California CCPA): applies when durin is hosted as a service for someone other than the operator. durin is not that.
  - Public/beta release: would change the deployment shape from "I run durin for myself" to "durin is a product". That decision deserves its own design pass, not a backlog row promising cascade-delete that the architecture rejects.

- **The escape hatch already exists at the operator level.** An operator who wants to remove specific data does `rm memory/entities/person/X.md memory/archive/...; git commit -m 'remove X'`. They lose recoverability, they take responsibility for what is gone, and the workspace is internally consistent. No cascading framework is needed; the operator has shell access to their own files.

- **Building cascade-delete invites a security antipattern even at single-operator scale.** A system that knows how to "forget everything about person:X" is exactly the system that gets misused to forget things the operator did not mean to forget — the same operation the operator runs once on real data could be triggered accidentally by a malformed prompt that asks the agent to "remove person:Y from memory". The shell-level `rm` requires the operator to think; the agent-callable `forget(person:X)` removes that friction.

**Status**: decided against. The §5 backlog row is removed. If the deployment shape ever changes (durin becomes a hosted multi-tenant service or a public product), GDPR / right-to-be-forgotten gets a dedicated design pass at that point — but inside the architectural rework that change requires, not as a feature added to the single-operator MVP.

---

### 2.19 Dedicated archive index (parallel vector / FTS5 / SQLite over `memory/archive/`)

**What was proposed**: build a parallel index over `memory/archive/**` so that `memory_search(scope='archive')` queries do not require walk + parse on demand. Options ranged from a small SQLite metadata table (uri, headline, archived_into, archived_at) to a full duplicate of the LanceDB + FTS5 stack scoped to the archive folder. Doc 08 §5 backlog row listed the trigger as "Frequent archive queries from operators".

**Why we are not implementing it** (audit B-11, 2026-05-29):

- **"Recovery is rare by design" is a structural commitment, not an empirical assumption.** Doc 01 §3.6 (and §283) state explicitly that archive is the recovery / diagnostic surface and that walking the folder on demand is the acceptable latency for the expected frequency. Frequent archive queries would not be a workload to optimise for — they would be a sign that the operator or the agent is misusing the recovery surface for searches that should target the active `memory/` instead. The right response in that case is to teach the search ("use `scope='all'` with more specific keywords") not to build a parallel index that legitimises the misuse.

- **The trigger "Frequent archive queries" is unbounded in either direction.** The system has the telemetry today — `memory.recall` events emit with `strategy='archive'` when `scope='archive'` was passed (F2). An operator could grep their telemetry log and count those events. But no threshold has been written ("frequent = ≥ N/week"), and writing one would only formalise a wait-and-see promise that the design intent already rules out. This is the soft-defer shape `feedback_stop_soft_deferrals` warns against: keeping a backlog row for a use case the design says should not exist.

- **The current on-demand walk is fast enough at MVP scale.** A workspace with 1,000 archived entries walks + parses in well under a second; 10,000 entries in a few seconds. Both are acceptable for a recovery surface that is hit a handful of times per week at most. The implementation cost to skip that latency would be ~150-300 LOC (SQLite metadata + sync hooks at every `archive_episodic` / `archive_entity` call site + schema migration) for an output that already exists.

- **Mainstream LLM-in-the-loop systems do not have archive-as-separate-tier at all.** mem0, letta, hermes, openclaw delete or overwrite rather than archive. There is no "archive index" comparison to make because there is no archive concept. durin chose archive over delete (principle P7); having chosen it, building a parallel index over the recovery surface duplicates infrastructure for a non-hot path.

- **The escape hatch already exists in shell.** An operator who genuinely needs fast archive querying can `find memory/archive -name '*.md' | xargs grep -l '<pattern>'` — works on any workspace, no schema migration, no index to keep in sync, no risk of drift. Same shape as the G2 rationale for not building `durin archive show / list` CLI commands.

**Status**: decided against. Removed from §5 backlog. `memory_search(scope='archive')` keeps the on-demand walker shipped in F2. If the design intent ever changes such that archive becomes a hot path (it should not), the same audit should reconsider the F2 surface AND the P7 archive-instead-of-delete commitment together, not just bolt an index on.

---

### 2.22 Unified `compose_embedding_text` dispatcher (F12, reverted)

**What was proposed**: a single public `VectorIndex.compose_embedding_text(item, ...)` classmethod as "the single source of truth for embedding composition", routing on input type to the two specialised composers. Audit F12 (2026-05-28) added it because doc 02 §4 promised that public name but only the two private specialists existed — F12 read the gap as "doc promises a function the code lacks" and closed it by adding the function.

**Why it was reverted** (QA review 2026-06-01): the dispatcher was never called — not once in its entire git history, no caller in `durin/`, `tests/`, or `webui/`, and it is not in `vector_index.__all__`. It could not have unified anything meaningful: an `EntityPage` and a `MemoryEntry` embed structurally different fields, so the two composers are genuinely divergent, not two implementations of one rule. Every real caller (all internal to `vector_index.py`) already holds a concrete type, so routing through an `isinstance` dispatcher is pure indirection — slower and less clear than calling the specialist directly.

**What the anti-drift intent really requires**: F12's underlying concern — "the entity-page path and the entry path must not drift" — is satisfied structurally by having exactly **one composer per indexable type** (`_compose_entity_page_text`, `_embed_text`), each the sole authority for its type. Doc 02 §4 now documents the per-type rules as the source of truth instead of pointing at a unified function. This is the same shape as §2.15: share the genuinely-common surface, do not merge divergent per-type logic behind one entry.

**The concrete trigger that would change this**: an *external* caller (outside `vector_index.py`) that must compose embedding text without knowing the item's concrete type. None exists; if one ever does, add the dispatcher back at that point — not preemptively to satisfy a doc string.

**Status**: dispatcher removed from `vector_index.py`; doc 02 §4 rewritten to document the per-type composers directly. The F12 entry in the archived audit (`docs/archive/32_memory_audit_reconciliation.md` §F12) is now superseded by this reversal.

---

## 3. Operational risks (from doc 18 §10)

The entity-centric memory design carries known operational risks. They were enumerated in `docs/archive/35_entity_centric_plan.md` §10 before the corpus was written. This section maps each risk to its status in v2 and identifies what (if anything) the corpus does to mitigate it.

| # | Risk | Status in v2 | Mitigation reference |
|---|---|---|---|
| **R1** | HyperMem (SOTA LoCoMo) achieves 92.73% without entity nodes; entity-centric design may not pay off on bench accuracy alone | **Accepted.** The promise of durin's memory is **operational coherence across sessions** + human-editable corpus + cross-system identity persistence — axes LoCoMo doesn't test. Bench is secondary; we use it for retrieval-quality regression detection, not as the primary success metric. | `09_implementation_roadmap.md` §11 (validation); doc 18 §11 outcomes |
| **R2** | Mega-hub: `person:user` and `project:durin` will accumulate hundreds-to-thousands of claims over months | **Partially mitigated.** Per-entity relation cap (soft 50 / hard 200 reject — doc 01 §4.4); archive of consolidated episodic (those entries don't count toward claims since they're moved out). **Sub-paging by scope is NOT implemented** — deferred to backlog (§5). Trigger: telemetry sees any entity with claim count > N. | doc 01 §4.4, doc 01 §3.6 archive, §5 backlog (sub-paging) |
| **R3** | Dream cost is unmeasured at scale | **Mitigated.** Doc 05 §13 provides an estimated range ($0.25-$1/day at typical pass rates with glm-5.1). Doc 07 telemetry captures `llm_input_tokens_total` and `llm_output_tokens_total` per pass. Operator alarm at `dream_llm_cost_per_day_usd > $5/day`. | doc 05 §13, doc 07 §6.2, §11 |
| **R4** | Cross-system identity (email vs git author vs conversational nickname) has no universal solution | **Accepted.** Aliases are declared manually (via Dream extraction from observations, or via human edit of the entity page). The system does not auto-resolve identity across external systems. | doc 01 §4.5 (slug normalization), doc 05 §5 (existing schema includes existing aliases) |
| **R5** | LLM-driven entity resolution can mis-merge | **Mitigated.** Absorb-judge defaults: OFF (master switch), 95/100 confidence threshold, 24h quarantine, recovery via `git revert`. The cascade is: deterministic match first (slug + aliases exact), LLM only in the gray zone — and only when operator opts in. | doc 05 §8 (all subsections) |
| **R6** | Alias collision: common names (`marcelo`, `María`, `juan`) become ambiguous as the workspace grows; alias_index assumes one-to-one | **Partially mitigated.** Numeric suffix in slugs prevents file-system collision (doc 01 §4.5). Absorb-judge handles cases where two pages should actually be merged. **BUT one-to-many alias resolution (`alias → N candidate entities` with disambiguation at read-time) is NOT implemented** — deferred to backlog (§5). Trigger: telemetry detects ≥2 entities sharing an alias in their `aliases:` field. | doc 01 §4.5, doc 05 §8, §5 backlog (alias one-to-many) |

**Reading guidance:** "accepted" means we know the limit and the system documents it; "mitigated" means active code reduces the risk; "partially mitigated" means there is a structural gap left for backlog.

---

## 4. Mechanisms in other systems NOT adopted

We surveyed mem0, Letta/MemGPT, Zep, Graphiti, Cognee, Hermes-Agent, OpenClaude, OpenClaw, OpenHands, GAAMA. These mechanisms exist in those systems but we explicitly did NOT adopt them.

### 3.1 HyDE (Hypothetical Document Embeddings)

**What it does:** LLM imagines a hypothetical document that would answer the query, embeds THAT, searches with the imagined-document embedding (often closer semantically to the actual answer than the literal query).

**Why not adopted (audit B-13, 2026-05-29, upgrading the original "future direction but not MVP" wording to decided-against):**

- **The hot-path variant is the same anti-pattern as G3.b** (LLM-in-retrieval). Audit 2.1 already discarded query rewriting on the hot path with the same reasoning — LLM call per search saturates rate limits, breaks the deterministic-retrieval invariant, costs latency the system optimised away.

- **The cold-path variant ("pre-generate embeddings of hypothetical documents during Dream") solves a problem the durin design does not have.** Dream already consolidates raw observations into entity pages — the canonical text the cold path produces IS what we embed and retrieve against. Layering HyDE-style hypothetical-document pre-generation on top would be Dream-generating-Dream-input: more LLM calls, more storage, more sync invariants, no observable failure mode it would fix.

- **Mainstream LLM-in-the-loop agent systems do not use HyDE.** mem0, letta, hermes, openclaw, graphiti — all ship without it. HyDE lives in research IR (BEIR-style benchmarks) where the retrieval system has no LLM downstream; in agent-memory, the LLM-in-the-loop reading already does the synthesis HyDE tries to compress into the index.

- **The trigger "Need for cold-path query enrichment surfaces" is not observable.** No telemetry would distinguish "the embedding model could not retrieve" from "the LLM could not synthesise from what was retrieved". The agent-memory architecture treats those as the same problem and routes both to the LLM-in-the-loop read.

**Status:** decided against, both hot-path and cold-path variants. Removed from §5 backlog. If a future workload genuinely needs query enrichment, the cheaper path (already considered) is to teach the agent to issue multiple `memory_search` calls with paraphrases — that already shipped +3.9pp on LoCoMo v2 via the identity.md prompt pattern (doc 11 LoCoMo v2 prompts result).

### 3.2 Reflection / pattern detection (GAAMA, Zep)

**What it does:** beyond fact consolidation, a periodic process detects recurring patterns ("X tends to postpone PRs in long sprints") and emits "reflection" nodes.

**Why not adopted (audit B-7, 2026-05-28, upgraded from the previous "backlog" wording to decided-against):**

- **Dream tier 1 + LLM-in-the-loop reading covers pattern queries at acceptable quality.** When the agent answers "what patterns does Marcelo show?", it loads `person:marcelo`'s entity page (already consolidated by Dream tier 1) plus the post-cursor episodic fragments, and the synthesis step at the LLM produces the pattern read. The pre-computed reflection node version would substitute a stored summary for an inline LLM read — savings on the LLM call, not on the answer quality, and only when the same pattern query repeats often enough to amortise the precomputation. We have no evidence that pattern queries are frequent enough to justify that amortisation.

- **Reflection nodes add a new schema we do not have.** Today the system has entity pages, entries (episodic/stable/corpus), and session summaries. Reflection nodes would be a fifth class (`memory/reflections/<id>.md`?) with its own write path, its own indexer hooks, and its own sync invariant (a reflection node about `marcelo` becomes stale when `marcelo`'s page changes; we would need an invalidation graph). That schema + sync work is ~300-500 LOC of complexity carried for every workspace whether or not the pattern query case actually fires.

- **Mainstream agent-memory systems do not ship reflection nodes.** mem0, letta, hermes, openclaw, graphiti — none have a pattern-detection tier. GAAMA and Zep do, but both are academic / research-oriented systems with different deployment shapes (GAAMA's hypergraph is the retrieval mechanism itself; Zep ships reflection as part of a research benchmark). For an LLM-in-the-loop personal-assistant deployment, reflection-as-precomputed-node is solving a problem the system architecture does not have.

- **The trigger as written is not measurable.** "Generalist use cases show pattern queries fail" requires defining what a pattern query is, what failure looks like, and which generalist use case is the source of truth. LoCoMo is factual recall (not pattern recall); the Phase-8 non-LoCoMo adversarial set could include pattern queries, but even there the comparison is "Dream-tier-1 + LLM read" vs "reflection node", and the realistic delta is per-token-cost not per-quality.

**Status:** decided against. Removed from §5 backlog. If a future deployment surfaces a concrete query pattern with measurable cost (e.g. "the agent re-reads the same entity page 50× per session to answer the same pattern question and the LLM-cost per session is observable in telemetry"), the right response is to **cache** the LLM-generated pattern read at the agent loop, not to introduce a fifth memory class. The cache is a much smaller surface than reflection nodes and stays inside the agent's existing read path.

### 3.3 Concepts as first-class entities (GAAMA hypergraph)

**What it does:** abstract concepts (e.g., `durin`, `rlhf`, `agile`) are first-class graph nodes that mediate retrieval via Personalized PageRank. A query "what does Marcelo think about RLHF" walks the graph `person:marcelo → topic:rlhf → other_entities_mentioning_rlhf` rather than running a flat retrieval.

**Why not adopted (audit B-9, 2026-05-29, upgraded from the previous "Backlog" wording to decided-against):**

- **Concepts already exist as first-class entities in durin's schema.** The `topic` entity type (`topic:rlhf`, `topic:agile`, `topic:bayesian_inference`) is on the same footing as `person`, `project`, `place`. An agent can search the topic page directly, follow its relations, or surface the topic from any entity that mentions it. The thing durin does NOT do is propagate retrieval through topics via PageRank — and that propagation IS the GAAMA mechanism, not the concept-as-entity part.

- **Hypergraph mediation is a different retrieval paradigm, not an additive feature.** durin's retrieval is RRF over vector + FTS5 + grep, with entity-aware boosting at rank time. Adopting GAAMA-style mediation would require: building a hypergraph index over entity pages + topics; choosing whether PageRank runs offline (added stale state) or per-query (added latency); replacing RRF fusion with PageRank-mediated traversal; tuning the damping factor for the workspace; rebuilding the entire test surface around the new ranking semantics. This is a ~2000+ LOC architectural rework — the doc 08 §5 row even labelled the "trigger" as `architectural rework`, which is itself an admission that the item is not a defer but a fork.

- **Mainstream LLM-in-the-loop systems do not use hypergraph mediation.** mem0, letta, hermes, openclaw, graphiti — all ship without it. GAAMA is the outlier and is research-shaped (academic benchmark deployment, not personal-assistant deployment).

- **The trigger "concept-level queries fail consistently" is not measurable.** What counts as a concept-level query, and what counts as failure? "What does Marcelo think about RLHF" is answered today by reading `topic:rlhf` (if it exists), reading `person:marcelo`'s recent fragments, and synthesising at the LLM. The "failure" mode the trigger implies is "the LLM could not synthesise the answer without PageRank" — but the LLM-in-the-loop pattern is exactly the analytical layer (same argument as B-7 reflection nodes, B-8 cross-entity scans). There is no telemetry that would distinguish "could not retrieve" from "could synthesise from what was retrieved".

- **The LLM agent is the analytical mediator.** durin's design is markdown source of truth + derived indices + LLM-in-the-loop reading. Concept-level queries are answered at the LLM, not at the index. Building a hypergraph to replace the LLM's role as mediator is solving a problem the deployment shape does not have.

**Status:** decided against. The `topic` entity type stays as a concept-bearing entity but does not become a graph mediator. Removed from §5 backlog. If a future workload genuinely needs hypergraph retrieval, that ships as a different system, not as a backlog item on durin's MVP.

### 3.4 Multiple memory tools per modality (e.g., semantic_search vs keyword_search)

**What others do:** some designs separate tools by retrieval modality. We considered this in `03_search_pipeline.md` §1.

**Why not adopted:** mainstream pattern is single tool with internal routing (mem0, hermes, openclaw, cognee — 4 of 5 systems). LLMs don't reliably choose between specialized search tools. We use one `memory_search` tool with optional `keywords` for explicit literal signaling.

### 3.5 LLM exposed weights / fusion params

**What others do:** none we surveyed expose RRF/BM25 weights to the LLM (verified in repos).

**Why not adopted:** LLMs don't have intuition for numeric weights. Pass burden to operator config + onboarding wizard / dashboard.

### 3.6 Mode/type enum in search tool (cognee `search_type`)

**What it does:** cognee exposes `search_type: GRAPH_COMPLETION | RAG_COMPLETION | CODE | CHUNKS | FEELING_LUCKY`, agent picks the retrieval mode.

**Why not adopted:** adds complexity to tool description. Test results suggest LLMs pick the wrong mode often. Auto-routing by query pattern (intent_router) achieves similar effect without burdening the LLM.

### 3.7 Embedding hybrid (SPLADE / ColBERT)

**What it does:** sparse + dense hybrid embeddings (SPLADE — learned vocabulary expansion) or multi-vector with late interaction (ColBERT — one vector per token, max-sim aggregation). Outperforms bi-encoder + BM25 in some IR benchmarks (BEIR).

**Why not adopted (audit B-10, 2026-05-29, upgraded from the previous "Backlog" wording to decided-against):**

- **durin already runs a dense + sparse hybrid.** Today's retrieval is MiniLM-L12-v2 (dense bi-encoder, 384-dim) fused via RRF with FTS5 BM25 (sparse) — the classic combination that SPLADE/ColBERT improve marginally in academic benchmarks. We are not in the "dense-only, missing sparse" regime; we are already at the configuration mainstream LLM-in-the-loop systems ship with.

- **The "embedding model" lever is already configurable.** `MemoryEmbeddingConfig.model` lets the operator swap MiniLM-L12 for `multilingual-e5-large` (1024-dim, ~430MB, better cross-lingual recall), `bge-m3` (multilingual + multi-functional), or any fastembed-supported model. Doc 02 §3.2 lists the typical alternatives. The expected response if Phase 8 shows a recall plateau is "switch the dense model to a stronger one via config + rebuild" — five minutes of operator work, not 1000-2000 LOC of architectural change.

- **SPLADE/ColBERT are paradigm shifts, not model swaps.**
  - SPLADE outputs a sparse vector with learned vocabulary expansion. LanceDB stores dense vectors; using SPLADE would require either replacing LanceDB with a sparse vector store or running a parallel sparse index.
  - ColBERT outputs one vector per token. Storage grows 10-100× per document. Retrieval requires a specialised index (PLAID) with non-trivial deployment requirements.
  - Both break the current LanceDB-and-FTS5 setup that the rest of the system (rebuild path, schema versioning, dim guards, cold-tier disk reads) is built around.

- **Mainstream LLM-in-the-loop systems do not use SPLADE/ColBERT.** mem0, letta, hermes, openclaw, graphiti — all ship with dense bi-encoder + BM25. SPLADE/ColBERT live in academic IR (BEIR benchmark, research deployments), not in agent-memory deployments where the LLM-in-the-loop reading covers a lot of the recall slack.

- **The trigger "Recall@10 plateau with current models" conflated two distinct interventions.** If Phase 8 shows a recall plateau, the empirical response is "swap the dense model" (already supported in config); the architectural response (SPLADE/ColBERT) is solving the wrong problem at the wrong layer. Keeping a backlog row that says "if recall plateaus, do paradigm shift X" reads as a defer but really wires the bench output to the most expensive possible response — exactly the soft-defer pattern `feedback_stop_soft_deferrals` warns against.

**Status:** decided against. Removed from §5 backlog. If Phase 8 surfaces a recall regression, the supported intervention is `MemoryEmbeddingConfig.model = <bigger model>` plus `durin memory reindex --target lancedb`. The architectural paradigm shift (SPLADE/ColBERT) is not on durin's roadmap.

### 3.8 Versioning as a separate tool

**What others might do:** dedicated `memory_history` MCP tool for git log queries.

**Why not adopted:** git history is exposed to Dream internally (its prompt includes `recent_history`) and to the human via any git CLI. No dedicated agent-facing tool. Cross-corpus decision #4.

### 3.9 Active forgetting policies (delete or compress old entries)

**What others do:** mem0 has lifecycle policies (delete after N days for low-importance memories). Letta has explicit memory management tools.

**Why not adopted (audit B-6, 2026-05-28, upgraded from the previous "backlog" wording to decided-against):**

- **Archive of consolidated episodic already handles the primary case** (§3.6 doc 01). Post-consolidation, the episodic file is MOVED to `memory/archive/episodic/<id>.md` with `archived_at` + `archived_into` frontmatter. It leaves all the active retrieval surfaces (vector index, FTS5, default grep) so it no longer competes for ranking, but it stays on disk so any decision the LLM made about it remains reversible. That covers the "stop letting old chatter dilute search" use case.

- **Deeper forgetting (compress 100 old episodic into 1 summary, delete the originals) violates principle P7** of `00_overview.md`: "Reversible decisions; Archive instead of delete; Provenance is always traceable." Deleting the 100 to keep one summary would make the consolidation irreversible — if Dream summarised wrong, the source observations are gone and the operator cannot inspect or recover them. That trades a real safety property for disk savings that the operator can already buy with a one-line shell command.

- **Disk is the cheap axis here.** Archive scales linearly with workspace age, not exponentially. LanceDB embeddings are small per row; FTS5 BM25 indexes do not bloat dramatically. The operator who hits an actual disk pressure threshold runs `rm -rf memory/archive/<class>/` themselves — explicit, conscious choice with full knowledge that recovery is gone after that point. The system enforcing the deletion would replace that conscious choice with implicit policy the operator has to discover and trust.

- **Compression-without-deletion is a different feature.** If "compress 100 episodic into 1 summary" is wanted with the originals still archived for recovery, that is reflection / Dream tier 2 (item B-7 in this batch) — not "active forgetting". The B-6 item specifically meant the destructive variant.

- **Mainstream stops at archive when they have it.** mem0 / letta's lifecycle policies exist because those systems do not archive — they delete or they keep, with no middle state. durin's archive IS the middle state, so the lifecycle layer those systems need is structurally unnecessary here.

**Status:** decided against. Removed from §5 backlog. Operator-side disk hygiene (manual `rm` on archive subdirs) remains the supported path for reclaiming space; the system does not auto-forget. Compression-without-deletion is tracked separately under "Reflection / Dream tier 2" (B-7 / §5).

### 3.10 Trust scoring per source

**What others might do:** user-provided memories rank above LLM-inferred memories.

**Why not adopted:** durin's classes (stable vs episodic) already encode this implicitly — stable means "user/agent explicitly marked durable". Not enough distinct trust tiers to justify a separate scoring system in MVP.

### 3.11 Tool call history as structured memory

**What others might do:** structure agent's own tool-call history as a queryable layer.

**Why not adopted:** sessions already contain tool calls. Grep over `sessions/<id>.jsonl` covers ad-hoc retrieval. No dedicated structured layer in MVP.

### 3.12 Cross-entity consistency checks (scheduled scan)

**What was proposed:** a periodic process that walks all entity pages and flags inconsistencies between them — relation reciprocity gaps (`marcelo.spouse = susana` exists, `susana.spouse = marcelo` missing), attribute conflicts across non-aliased entities, temporal contradictions in role / location history. Doc 08 §5 backlog row "Cross-entity consistency checks" listed the trigger as "Drift between entities observed".

**Why not adopted (audit B-8, 2026-05-29):**

- **The major class of cross-entity inconsistency — duplicates — is already handled by absorb-judge.** `durin/memory/absorb_judge.py` runs `judge_pair` on every alias-overlap candidate that survived the alias index check and decides merge / keep-separate / unclear with confidence. The "person:marcelo and person:marcelo_marmol are the same person" case is exactly what absorb-judge auto-merges (when confidence ≥ threshold). Adding a separate cross-entity scan would duplicate the work absorb-judge does on the most common failure mode.

- **Relation reciprocity is not a system invariant.** "Marcelo knows X" does not imply "X knows Marcelo"; "Marcelo attended event Y" does not imply "event Y attended Marcelo". Relations carry direction in durin's schema (doc 01 §4) and the LLM that writes them is the source of truth for direction. A consistency check that flags reciprocity gaps would generate false-positive noise on the cases (most of them) where the gap is intended.

- **Other inconsistencies are surfaced at LLM read time, where they matter.** When the agent loads `person:marcelo` to answer "what is Marcelo's email?", it sees the entity page's current attribute. If the user follows up with "but you said something different last week", the agent re-reads, notices the conflict in `git log memory/entities/person/marcelo.md`, and can reconcile. The conflict surfaces in the conversation where the user actually cares about it, not in a scheduled scan that emits warnings nobody reads. This is the same LLM-in-the-loop pattern that B-7 (reflection nodes) was discarded against.

- **Implementation cost is high and the action policy is awkward.** A real scan would need: periodic walker over all entity pages (~50 LOC); detection rules per inconsistency type (each LLM-or-deterministic, 50-100 LOC each); action policy (emit warning? suggest merge? auto-fix?). Auto-fix violates P7 (reversibility). Warning-only fills an event log nobody reads. Suggest-merge is what absorb-judge already does for the duplicate case. ~300-500 LOC total for an output the operator can already produce on demand by asking the LLM "are there any contradictions in my workspace for X?".

- **Mainstream systems do not ship cross-entity consistency scans.** mem0, letta, hermes — none have a dedicated scan tier. Graphiti has bidirectional relation invariants because it IS a graph DB and relations are structural; durin's markdown + indices design treats relations as data, not as graph edges, so reciprocity is not architectural.

- **The trigger "drift between entities observed" is not measurable.** No telemetry counts contradictions because the system has no contradiction detector — adding one is the proposed feature. Operators do not "observe drift" in the abstract; they observe specific bad answers, which the LLM read path then debugs by reading git log on the affected entity. The trigger is a tautology of the feature.

**Status:** decided against. Removed from §5 backlog. Absorb-judge handles duplicate detection; LLM-in-the-loop reading handles the rest at the moment that matters; the `durin memory history <uri>` command (doc 04 §11) gives the operator the audit trail when they actually want one. No cross-entity scan tier in MVP or post-MVP.

---

## 5. Features explicitly deferred to backlog

These are NOT discarded; they're queued for post-MVP. When and how they enter depends on observed need.

| Feature | Trigger to revisit | Likely doc to update |
|---|---|---|
| **§2.F eager pre-fetch** (query-specific memory injection into user message before LLM call, hermes/openclaw pattern) | **Single concrete trigger** (audit B-1, 2026-05-28, applying the G5 tight form): Phase 8 LoCoMo run reports a failure cluster of ≥ 5 questions where the per-failure trace shows BOTH (i) the agent answered without invoking `memory_search` AND (ii) the gold answer required content that lives in memory and is not in the static HotLayer (`docs/architecture/memory/06_prompts_and_instructions.md` §8). Earlier wording proposed thumbs-down feedback channels and "operator reports" — both were dropped here because the channel does not exist and "operator reports" is not measurable. **Counterfactual**: if Phase 8 shows < 5 such failures, §2.F is decided-against and moves from §5 backlog to §2 discarded with the empirical evidence from the Phase 8 run. **Why current behaviour might already suffice** (the question Phase 8 settles): HotLayer eager-injects canonical entity pages + recent fragments + entity name list on every prompt today, so query-specific re-injection would be incremental on top of an already-eager surface; and the multi-query identity.md prompt pattern (shipped 2026-05-25) bumped LoCoMo v2 by +3.9pp by teaching the agent to invoke `memory_search` itself on relevant turns. **Estimated implementation cost when triggered**: ~150 LOC (memory_search call in `AgentLoop._build_messages` pre-LLM step, wrapper `<memory-context>` block, ephemeral insertion that does NOT persist to session, failure-mode handling), + telemetry event `memory.eager_prefetch_invoked`, + bench harness change to measure the +pp gain. See §4.1 below for the mechanism detail. | `06_prompts_and_instructions.md` new §9, `04_agent_tools.md` §6 |
| MMR (Maximal Marginal Relevance — diversity re-selection in top-K) | **Single concrete trigger** (audit B-3, 2026-05-28, applying the G5 tight form): Phase 8 LoCoMo run reports ≥ 10% of recall-success queries (questions where the gold answer DID surface in top-10) carrying > 2 near-identical hits — measured by per-pair cosine similarity ≥ 0.92 between SectionedHit summaries. Earlier wording "bench / user reports show duplication" was vague and not measurable. **Counterfactual**: if Phase 8 shows < 10% of successful queries hit the duplication threshold, MMR is decided-against and moves from §5 backlog to §2 discarded with the Phase-8 evidence cited inline. **Why current behaviour might already suffice**: archive of consolidated episodic (doc 01 §3.6) removes the primary duplication source — most "marcelo email" hits used to surface 4× near-identical episodic mentions that are now consolidated into one entity page; the per-source cap (doc 03 §12.4) handles the secondary case where one ingested PDF dominates the top-K via chunks of the same `ingest_id`; and MMR carries a real downside (pushes the strongest exact-match hit out of top-K in favour of diversity) that hurts queries like "exact email of X". Mainstream systems (mem0, graphiti, hermes, letta, cognee) ship without MMR. **Estimated implementation cost when triggered**: ~80 LOC standalone algorithm in `durin/memory/mmr.py` (no dependencies on the rest of the pipeline), wired as a step in `run_search_pipeline` between the entity-aware rerank and `apply_per_source_cap`, with one `λ` (relevance vs diversity weight) added to `MemorySearchConfig` defaulting to a value tuned in the same Phase-8 run. Telemetry: extend `memory.recall` payload with `mmr_drops` (count of hits dropped for diversity). TDD: 4-5 cases. | `03_search_pipeline.md` §11 |
| **Sub-paging by scope (R2 mitigation)** | **Single concrete trigger** (audit B-14, 2026-05-29, tightening to the G5 form): any single entity page accumulates ≥ 200 claims, measured as `len(attributes) + len(relations) + count_of_provenance_lines_in_body`, AND retrieval bench (Phase 8 LoCoMo or any post-Phase-8 workload) shows that queries about that entity miss content that lives in the post-200 portion of the page. **Counterfactual**: if a mega-hub forms but bench retrieval against it stays at parity with smaller entities, sub-paging is decided against and the entity stays as a single page until it crosses the FTS5 / vector budget. **Why current behaviour might already suffice**: archive of consolidated episodic does not count toward claims (those entries move out); the per-entity hard cap of 200 in doc 01 §4.4 is documented (enforcement tracked in B-19) and would log/reject the over-200 case before it ships; mega-hubs in practice cluster claims by topic and the embedding model handles that via the semantic centroid. The R2 risk in doc 18 §10 is "mega-hub will accumulate hundreds-to-thousands of claims over months" — without sub-paging, the entity page truncates at the 1500-char embedding budget; with sub-paging, the 1500-char budget per sub-page lets the full content land in retrievable centroids. **Estimated implementation cost when triggered**: schema extension in `01_data_and_entities.md` §3.5 (each sub-page is a new `EntityPage` with `parent_ref` field, ~30 LOC change); Dream-side partition logic that splits a page along scope boundaries when the trigger fires (~150 LOC + new prompt template); search-side merge for queries that should hit the parent (~50 LOC); rebuild of any LanceDB rows for the partitioned entity. Total ~300-400 LOC + reindex of the partitioned entities only. | `01_data_and_entities.md` §3.5 schema extension; `05_dream_cold_path.md` new section on partition triggers |
| **Alias one-to-many resolution (R6 mitigation)** | **Single concrete trigger** (audit B-15, 2026-05-29, tightening to the G5 form): the alias index builder (`durin.memory.aliases_index.AliasIndex.build`) reports ≥ 2 entities sharing any alias string after normalisation, AND Dream apply records ≥ 1 write-time collision where the LLM-emitted alias was rejected for ambiguity. Both signals are observable: `AliasIndex.size()` vs unique-alias count is a one-line check; Dream apply can emit a `memory.alias_collision` event when it skips a write. **Counterfactual**: if the alias index stays one-to-one across a year of operation (no shared aliases observed), one-to-many is decided against and the absorb-judge path keeps handling the rare collision via merge. **Why current behaviour might already suffice**: numeric suffix slugging (`marcelo_2`) handles file-system collisions per doc 01 §4.5; absorb-judge merges duplicates that should be the same entity; for genuinely distinct entities sharing an alias ("María Garcia" vs "María Lopez"), the LLM-in-the-loop context window can disambiguate at read time from surrounding turn content, so one-to-many is mostly an optimisation. **Estimated implementation cost when triggered**: `AliasIndex` becomes `dict[str, list[str]]` instead of `dict[str, str]` (~30 LOC + tests); `extract_query_entities` returns a candidate list rather than a single hit (~50 LOC + tests); search-side disambiguation in `_entity_aware_rerank` (~50 LOC); Dream write-time tagger learns to ask the LLM "which `María` did you mean?" or to skip-and-log (~80 LOC + prompt template change); telemetry event `memory.alias_disambiguation` (~10 LOC). Total ~250-350 LOC + alias index rebuild. | `01_data_and_entities.md` §4.5 (alias index becomes one-to-many); `03_search_pipeline.md` §3.2 (entity extraction returns candidate list); `05_dream_cold_path.md` §8 (write-time tagger) |
| **Memory export / import (formal)** — structured dump filterable by entity/scope/date, cross-system migration from competing systems (mem0, letta), encrypted format option | **Two concrete triggers** (audit B-16, 2026-05-29, tightening to the G5 form): (1) first external user requests an export (durin opens to non-operator users via any channel — Telegram/Slack/etc. bot, beta release announcement, or limited-release sharing); OR (2) the operator/maintainer plans a breaking schema change to entity pages or the indices that would invalidate existing workspaces on upgrade. **Counterfactual**: as long as durin stays single-operator and no breaking schema change is planned, the feature is decided against — `cp -r ~/.durin/workspace/` between same-version installations is portability enough, and any breaking change ships with a migration script targeting that specific change rather than a generic export format. **Why current behaviour might already suffice**: the workspace IS a normal git repo (the operator can `git push memory/.git/` to any remote and `git clone` it elsewhere); `cp -r` between identical durin versions preserves the full state including LanceDB rows; cross-system import from mem0/letta only matters if durin is the destination of a migration that has not yet been requested. **Estimated implementation cost when triggered**: structured JSON export with `--filter entity=X --since=Y --until=Z` flags (~150 LOC + tests); cross-system import adapters per source system (~200 LOC each — likely just one initially, the requested-by-user source); encryption layer if the trigger came from a beta release (~100 LOC + key management documentation). Total ~450-650 LOC + a dedicated design doc covering format stability commitments. | New design doc when triggered |
| **HRR — Holographic Reduced Representations for compositional entity reasoning** (audit H11, 2026-05-29, originated from cross-framework comparison `hermes-agent/agent/holographic_retrieval.py`) | **Single concrete trigger**: ≥ 5 user queries in any rolling 30-day window of the form "what predicates connect entity A and entity B?", "what facts contradict each other about X?", or "find all relations of type Y across all entities" — i.e. graph-traversal queries that today either return no useful result or require multiple `memory_search` round-trips to assemble. Measured by a one-off LLM-classifier batch over `~/.durin/telemetry/*.jsonl` user messages, scored against a regex-tagged proxy + manual review of 20 hits. **Counterfactual**: if the 30-day classification reports < 5 such queries (i.e. users never ask compositional questions), HRR is decided-against and the existing entity_ranker + RRF fusion handles the predominantly point-lookup workload. **Why current behaviour might already suffice**: the entity_ranker (`durin/memory/entity_ranker.py`) boosts hits whose `entities` field overlaps with the query entities, which handles "facts about Marcelo" well; absorb-judge consolidates contradictions at write time so post-Dream contradiction queries usually have zero candidates by design; cross-corpus relation queries are handled by the agent itself issuing 2-3 `memory_search` calls with different entity refs as `query` — slower than HRR algebra but does not require a new index layer. Hermes' HRR specifically enables `probe(entity)`, `reason(entity_A, entity_B)`, and `contradict()` — operations impossible with bi-encoder embeddings — but hermes ships them because hermes is multi-tenant and operators occasionally need graph-shaped audit queries; single-operator durin's audit needs are typically resolvable via `grep memory/**/*.md`. HRR is also **language-agnostic by design** in durin's case: it operates on entity refs (`person:marcelo`), not on the surface text the entity was extracted from, so unidecode normalisation (`docs/architecture/memory/01_data_and_entities.md` §4.5) makes the algebra work identically across English / Spanish / CJK. **Estimated implementation cost when triggered**: `durin/memory/hrr_index.py` with numpy-based binding (circular convolution) + bundling (superposition) + unbinding (~150 LOC); persistence in `.hrr.npz` alongside `.index.lance` (~30 LOC); Dream-side hook that re-binds an entity page's attributes/relations on every consolidation (~50 LOC + tests); three new tools `memory_probe(entity, predicate)`, `memory_reason(entity_a, entity_b)`, `memory_contradict()` (~150 LOC + descriptions in doc 06); HRR seed determinism via fixed-seed random vectors per symbol (~10 LOC). Total ~400 LOC + new doc section in `03_search_pipeline.md` §X HRR. Multilingual stays free because the algebra never touches surface text. | New section in `03_search_pipeline.md` describing the HRR index alongside vector + FTS |
| **Sentence-level (or paragraph-level) chunking for corpus entries** (audit H12, 2026-05-29, originated from bench-29 retrieval-miss analysis) | **Single concrete trigger**: a bench failure cluster of ≥ 3 questions where (i) the gold answer text appears verbatim in a corpus chunk that ranks outside top-10 of the relevant `memory_search`, AND (ii) re-running the same query against a sentence-chunked variant of the corpus (chunk per `\n\n` paragraph) lands the same gold answer in top-3. The trigger is bench-measurable in one pass — no production telemetry needed. **Counterfactual**: if no bench cluster materialises (i.e. all retrieval misses are either "fact not in corpus at all" or "fact in a chunk that already ranks well"), sentence chunking is decided against. The dominant retrieval-miss mode in bench-29 (2026-05-29) was vocabulary mismatch (`conv-1-q15` agent said "no record" while the fact existed under different wording), NOT chunk-size dilution. **Why current behaviour might already suffice**: cross-encoder rerank (`durin/memory/cross_encoder.py`, ON in bench per audit H7) re-scores the top-50 candidates with the full document text against the query — a chunk whose centroid ranks at position 30 by bi-encoder cosine can still surface at position 3 after the reranker re-reads its body. Reranker latency is 300-1500ms per query but is the cheapest fix for "single-vector-per-doc dilutes the answer". `memory_ingest` already chunks at 1500 chars with 200-char overlap (`text_splitter.split_text`), so a fact straddling a chunk boundary surfaces in both. Mainstream systems (mem0, hermes-fact-store, letta) chunk at ~1500-2000 chars + rely on reranker for fine-grained recovery; nobody ships sentence-level chunking by default because the 10× row blow-up costs disk + embedding compute. ColBERT-style multi-vector late interaction would be the next step beyond sentence chunking but was discarded in §3.7 with the same reasoning. **Estimated implementation cost when triggered**: `text_splitter.split_text` gains a `granularity` param (`chunk`/`paragraph`/`sentence`, ~30 LOC); LanceDB schema unchanged (still one row per chunk, just smaller chunks); chunk metadata gains `parent_chunk_id` so the agent can aggregate back to the full doc context (~50 LOC); search-side merge during sectioning so multiple sibling hits from the same source collapse into one block (~80 LOC); reindex of corpus entries only (no schema migration). Total ~150-200 LOC + reindex. Worth keeping in mind even if not triggered: this is a one-flag operation per-ingest, not a global architectural change, so the operator could opt-in per-document for known-large-doc use cases. | `01_data_and_entities.md` §3.2 corpus section (chunk granularity option); `04_agent_tools.md` §4 memory_ingest (new `granularity` param) |

### 4.1 §2.F eager pre-fetch — detailed rationale

This is one of the deferred items above. Worth detailing because the deferral is data-driven, not arbitrary.

**Mechanism (Hermes + OpenClaw pattern, verified in `hermes-agent/agent/memory_manager.py:227` and `conversation_loop.py:754`):**

```
Before each agent turn:
  1. raw_context = memory_search(query=user_message)
  2. block = "<memory-context>[system note...]\n{raw_context}\n</memory-context>"
  3. user_message_for_api = original_user_message + "\n\n" + block
  4. LLM sees the message with memory already injected; may respond without
     invoking memory_search tool at all
```

**Why deferred (not rejected):**

- **HotLayer already covers 70%.** The always-on canonical+fragment injection (doc 06 §8) handles the most-frequent queries without a per-turn extra search.
- **Multi-query identity.md pattern shipped +3.9pp** in LoCoMo v2 by teaching the agent to invoke memory_search itself when needed. Adding eager pre-fetch on top is uncertain incremental value.
- **Cost per turn**: +50-130ms latency + cache miss in upstream prompt cache (variable payload in user message). Real cost; only worth paying if the value is observable.

**Trigger to revisit (post-§2.11 update, 2026-05-28):**

The original spec was a telemetry event `memory.silent_retrieval_miss`
detected via three heuristics (substring overlap + English negation
tokens + correction patterns). Audit B9 + §2.11 discarded that
detector — its heuristics don't generalise to multi-lingual
workloads (CJK + Spanish in durin's seed bench), and replacing them
with an LLM judge on the hot path breaks the cheap-telemetry
contract the rest of doc 07 describes.

Replacement triggers, in priority order:

1. **Explicit user-feedback signal.** When the operator opens a
   thumbs-down / retry surface (any channel) and the rate of
   "agent forgot" complaints exceeds ~3/week, that's the activation
   signal. Unambiguous, language-agnostic.
2. **Bench failure cluster.** On LoCoMo or EverMemBench traces,
   when an unrecoverable cluster of failures shares the pattern
   "agent didn't invoke `memory_search` AND the gold answer required
   memory recall", that's a controlled-environment signal — same
   meaning as the original detector but with ground-truth labels.
3. **Offline LLM judge over bench traces** (post-hoc, batched, not
   per-turn). Same accuracy as the discarded per-turn detector,
   amortised cost, no hot-path budget impact.

If any of these triggers fire and §2.F gets activated, the spec to
write lives in the paragraph below.

**When activated, the spec to write:** doc 06 new §9 "Eager pre-fetch (§2.F)" with: trigger condition (every turn), search builder (uses user_message as query, scope=all, level=warm), wrapper format (`<memory-context>` block with system note), insertion point (append to user message ephemerally — do NOT persist to session), failure behavior (omit silently on memory_search error), telemetry (`memory.eager_prefetch_invoked` + duration).

---

## 6. Decisions where we explicitly chose against the mainstream

Cases where mem0/graphiti/etc. do X, but we chose NOT X for explicit reasons. Useful when someone says "why doesn't durin do X like everyone else?"

| Topic | Mainstream | Durin choice | Rationale |
|---|---|---|---|
| Default ON for cross-encoder | mostly opt-in OFF (same) | Opt-in OFF | We agree with mainstream here. |
| MMR | rarely implemented | Not in MVP | Same as mainstream. |
| Versioning as a tool | not standard | git history exposed via Dream prompt + CLI | Reuse what exists. |
| LLM in hot path | most avoid; cognee uses LLM in classifier | We strictly avoid | Cost + latency. |
| Multi-vector per facet | rare (some research) | Single vector per doc | Simplicity. |
| Closed catalog | mem0 has implicit catalog via LLM tendencies | Open, with drift control via existing_schema | Generalist use cases. |
| Tool sectioning markers | rare (hermes uses `<memory-context>`) | We use them (CANONICAL/FRAGMENT/SESSION/INGESTED) | Validates +3.9pp in our v2 prompts. |
| Cold-path consolidation | mem0 sync at write; we batch | Async batched Dream | User experience (no write latency) + cost. |

---

## 7. Lessons learned (general)

Distilled from the design process documented in `docs/archive/40_exploracion_datos_y_relaciones.md` and prior iterations:

### Lesson 1 — Tool description is a weak signal

Imperative instructions in tool descriptions ("USE BEFORE answering", "trust this") tend NOT to change LLM behavior reliably. Structural patterns (markers in results, distinct tool names with specific purpose) work better.

**Evidence:** D1 / D3 prompts tested 2026-05 lost 20pp. The v2 prompts (declarative + specific) gained 3.9pp.

**Implication:** prefer structural communication. When you must use text, make it declarative ("issue 2-3 searches for compound questions") not imperative ("ALWAYS use multi-query").

### Lesson 2 — Fix causes, not symptoms

When retrieval fails, the temptation is to add a downstream patch (rewriter, pin, special mode). The right approach is to ask: what UPSTREAM weakness is causing the failure?

**Evidence:** G3.b query rewriter compensated for 5 upstream issues. Fixing those (frontmatter rendering, summary for entity pages, aliases in entries, FTS5 for cross-lingual lexical, BM25 over rendered frontmatter) makes the rewriter unnecessary.

**Implication:** before adding a new component, list the upstream causes the new component would compensate for. Fix one of those instead.

### Lesson 3 — Archive over delete

Recoverability is cheap when designed in; expensive when bolted on.

**Evidence:** archive of consolidated episodic preserves provenance and enables recovery if Dream consolidates wrong. Bench shows this also eliminates the main duplication problem in retrieval.

**Implication:** when removing data from active state, move it (archive) rather than delete. Disk is cheap; bad consolidations are expensive.

### Lesson 4 — Markdown as source of truth

When index and SoT diverge, SoT must win. This requires the index to be a derivative reconstructible from SoT.

**Evidence:** every index in this corpus (LanceDB, FTS5, eventual structural SQLite) is reconstructible from `.md` files. `durin memory reindex` is always available.

**Implication:** never store data in an index that doesn't also exist in markdown. The index is acceleration; markdown is truth.

### Lesson 5 — Single tool with internal routing > multiple specialized tools

Multi-tool agents struggle to choose between similar-purpose tools (`feedback_tool_description_weak_signal.md`).

**Evidence:** mem0, hermes, openclaw, cognee all use single search tool. Cognee tried mode enum and added `FEELING_LUCKY` because the agent picked wrong modes.

**Implication:** if you can route by query pattern (CJK, keyword shape, etc.) internally, do that. Don't make the LLM pick.

### Lesson 6 — Cold-path investment pays compound returns

Building Dream right (consolidation, archive, dedup, drift control) eliminates many downstream problems (duplication, drift, retrieval noise).

**Evidence:** archive + consolidation makes MMR unnecessary, makes pin-by-modality unnecessary, makes drift control structural rather than per-query.

**Implication:** invest in cold path early. Hot-path patches stack up as tech debt.

---

## 8. Cross-references

- Architectural decisions per module: each module's §10/§14/§16 (decisions tables).
- Cross-corpus decisions: `00_overview.md` §10.
- Prior exploration (Spanish, longer-form): `docs/archive/40_exploracion_datos_y_relaciones.md`.
- Mem files documenting past failures: `~/.claude/projects/.../memory/feedback_*.md`, `project_g3b_query_rewriting_plan.md`.
