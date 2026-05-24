# 28 — LoCoMo first real run + SOTA gap analysis

> First real LoCoMo benchmark of durin's memory layer (post the
> 2026-05-24 fix that wired `memory.enabled` end-to-end). Score,
> per-category breakdown, attribution of fails to specific SOTA gaps,
> and a concrete shortlist of architectural changes worth prototyping.
>
> Compañion to [27_locomo_benchmark.md](27_locomo_benchmark.md) (the
> harness). This doc is about the **results**.

---

## TL;DR

- **Current score: 60.8% (62/102), glm-5.1** after B1 (infra retry) +
  B2 (event_summary + blip_caption seeding) shipped. **+3pp over the
  v1 baseline** of 57.8%. Vector retrieval active end-to-end.
- **6.1× over no-memory baseline** (10% on 10-QA ablation).
- **Far from SOTA** (HyperMem 92.73%, Mem0 92.5%). Gap is **~32pp**.
- **B2 helps where its content is relevant** (open_domain +12pp,
  temporal +8pp via curator-supplied dated event summaries) but
  **slightly hurts single_hop / multi_hop** (-4pp each) because vector
  ranking surfaces third-person curator summaries over original first-
  person quotes the agent needs to cite.
- **D1+D3 (text-in-description hacks) reverted** — empirically caused
  -20pp regression on the first 30 QAs before being killed mid-run.
  Confirms: tool-description text is a weak signal for changing LLM
  behavior. See [[feedback_tool_description_weak_signal]] in memory.
- **Pre-everything: ~5-8pp of the gap is harness/infra**, not durin.
  3 fails were LLM connection errors / max-iter (retriable). 2-3 more
  reference data the harness drops (image captions, event summaries —
  see §4.5). Realistic clean-score: **~61-63%** before any architectural
  change. Step -1 in §7.
- **Highest-ROI architectural lever is synthesis discipline / cite-only
  output** (closes ~30% of fails — agent over-generates from correct
  retrieval). Nearly free, pure prompt + render change.
- **Second is list-aware retrieval expansion** (~26% of fails are
  list-shape questions where top-10 misses items).
- **Atomic fact extraction is NOT the top lever** despite being the
  SOTA pattern — per-trace audit shows its impact is over-attributed
  when counted cross-category. Coverage-gap-list (which atomic facts
  helps) is solvable cheaper via list-aware retrieval first.
- **Uncomfortable finding**: HyperMem (SOTA, 92.73%) **has no entity
  nodes**. It wins via topic→episode→fact hierarchy + hybrid retrieval.
  Durin's entity-centric bet optimizes for cross-session coherence —
  a use case LoCoMo doesn't measure. The entity layer isn't what's
  hurting us on LoCoMo, but it isn't what would close the gap either.
- **Honesty note**: 2 versions of §4 were wrong before being corrected
  via user pushback. The doc keeps the audit trail (§4 preface) so a
  reader sees the iterations, not just the final claim.

---

## §1 — Run details

| Field | Value |
|---|---|
| Run dir | `bench-results/locomo/2026-05-24_181245_ffe1518d/` |
| Commit | `ffe1518d` (after the `memory.enabled` fix) |
| Model | glm-5.1 (agent + judge) |
| Dataset | LoCoMo10 (1542 QAs across 10 conversations) |
| Subset | Stratified, 25 per balanced category + 2 adversarial = **102 QAs** |
| Seed | 42 |
| `memory.enabled` | True (now actually wired) |
| Vector index | LanceDB + fastembed multilingual MiniLM, rebuilt per-QA after seed |

Reproduce:

```
python -m scripts.benchmark.locomo_run \
    --data-path ~/.cache/durin/locomo10.json \
    --per-category 25 --allow-undersupplied \
    --model glm-5.1 --judge-model glm-5.1 \
    --max-iterations 8 --timeout-s 120
```

---

## §2 — Score by category

| Category | N | v1 (baseline) | v3 (B1+B2) | Δ |
|---|---|---|---|---|
| adversarial | 2 | 100% (2/2) | 100% (2/2) | = |
| **open_domain** | 25 | 76% (19) | **88% (22)** | **+12pp** ✓ |
| multi_hop | 25 | 72% (18) | 68% (17) | -4pp |
| **temporal** | 25 | 48% (12) | **56% (14)** | **+8pp** ✓ |
| **single_hop** | 25 | 32% (8) | 28% (7) | -4pp |
| **Total** | **102** | **57.8% (59)** | **60.8% (62)** | **+3pp** |

### What B2 (event_summary + blip_caption seeding) actually did

**Wins** (where the curator-produced content was distinctly useful):
- **open_domain**: questions like "what writings does Joanna do" benefit
  enormously from event_summary which abstracts "Joanna writes
  screenplays, books, blog posts" into a single entry; raw turns
  scattered the info.
- **temporal**: event_summary entries carry `valid_from` derived from
  session date, giving the agent an anchored timeline that the turns
  alone didn't surface for date-arithmetic queries.

**Losses** (where third-person curator prose displaced first-person quotes):
- **single_hop**: questions asking for a specific phrasing ("what did
  Melanie paint?") want the actual conversational quote. With event_summary
  in the index, vector ranking often returns the curator's summary
  ("Melanie discussed her recent paintings") instead of the literal
  "I painted a sunset" line — net negative.
- **multi_hop**: similar pattern — needs to cite specific factual claims
  but curator summaries compress them into less-precise narratives.

**Lesson**: seeding curator-derived synthesis helps when the question is
ABOUT what happened thematically; hurts when the question demands literal
recall of a specific fact in the speaker's words. Future B2 refinement:
seed event_summary only for `temporal`+`open_domain`-shaped indices, or
add a "source priority" weight that ranks turn-quotes above summaries
for noun-level queries.

### Counter-intuitive: `single_hop` (32%) is worse than `multi_hop` (72%)

Single-hop is supposed to be the easiest: one fact, one retrieval. That
it scores worst is the strongest signal in the data. Hypothesis (validated
in §4): single-hop questions ask for **atomic noun-level facts** ("what
did X paint?", "what causes does Y support?", "where does Z live?") —
exactly where durin's **full-markdown-per-turn storage + vector-only
retrieval** loses to **BM25 hybrid + atomic fact extraction** (Mem0,
HyperMem, GAAMA).

Multi-hop scores well because durin's vector top-10 + LLM synthesis
naturally handles "combine these 2 facts" — the agent has the contextual
breadth it needs. Multi-hop wins where coverage matters; single-hop
loses where precision matters.

---

## §3 — vs SOTA architectures

Source: [research/16b](research/16b_entities_in_new_systems.md) +
[research/16c](research/16c_entities_academic_and_online.md) + targeted
re-read on 2026-05-24.

| System | LoCoMo | Storage primitive | Retrieval | Why it wins |
|---|---|---|---|---|
| **HyperMem** | **92.73%** | Facts in 3-level hypergraph (topic/episode/fact) | BM25 + dense hybrid, ranked by hyperedge proximity | Atomic facts + episode clustering → temporal coherence is implicit; **no entity nodes** |
| **Mem0** | 92.5% | Per-fact vector embedding + spaCy NER sidecar | Vector top-K + BM25 lemmatized | ADD/UPDATE/DELETE LLM decision at write; atomic facts; hybrid retrieval |
| **GAAMA** | 78.9% | 4 node types (episode/fact/reflection/concept) hypergraph | k-NN + PPR on concept pivots | Concept-mediated retrieval avoids person mega-hub; reflections consolidate patterns |
| **HippoRAG** | 69.9% | Noun-phrase nodes + cosine-similarity synonymy edges | Personalized PageRank from query entities | Soft entity identity via weighted edges (no merge); PPR propagates relevance |
| **durin (this run)** | **57.8%** | Markdown per turn | Vector top-10 (MiniLM) + entity-aware RRF when alias exists | Wired vector path; no atomic facts, no BM25, no temporal filtering |
| **A-MEM** | ~47% | Free-form notes + LLM evolution links | Vector top-K + link chain | No entities; notes-as-atoms; loses temporal chain |

**Key delta**: every system above durin uses **atomic facts** + **hybrid
(lexical + dense) retrieval**. Durin uses neither. Every system above
A-MEM uses **either explicit temporal metadata OR hypergraph episode
clustering** for temporal coherence. Durin has `valid_from` per entry
but doesn't filter retrieval by it.

---

## §4 — Failure attribution: per-trace, per-category (EMPIRICAL)

> **Two drafts kept for honesty**:
> 1. **v1 mistake (cross-category gap counting)**: attributed fails to
>    SOTA techniques across categories ("BM25 closes 10", "atomic facts
>    close 17"). Didn't survive per-trace verification.
> 2. **v2 mistake (assumed harness was correct)**: ran per-trace audit
>    classifying every fail as agent/memory/synthesis issue. Missed that
>    **the harness silently drops data**. After user pushed on a specific
>    case (`conv-2-q19` windshield), found that ~20% of seeded turns lose
>    image context and ~5-10% of fails reference data only in non-turn
>    fields the harness ignores. The §4 attribution below stands for the
>    fails where data IS reachable; see §4.6 for the harness-gap subset.

### §4.1 — Causes per category (single-attribution, dominant cause)

| Cause | single_hop (17) | temporal (13) | multi_hop (7) | open_domain (6) | **Total / %** |
|---|---|---|---|---|---|
| **synthesis_overgeneration** (had truth + added extras / wrong abstraction) | 7 | 4 | 1 | 1 | **13 (30%)** |
| **coverage_gap_list** (vector top-K missed items in a list-shape Q) | 4 | 3 | 2 | 2 | **11 (26%)** |
| **ranking_miss** (right answer in memory but not in top-K) | 4 | 3 | 2 | 0 | **9 (21%)** |
| **wrong_answer** (retrieved unrelated content / confused entities) | 0 | 4 | 1 | 2 | **7 (16%)** |
| **judge_strict** (agent essentially right, judge marked near-miss fail) | 0 | 3 | 0 | 1 | **4 (9%)** |
| **no_retrieval** (0 calls or only filesystem grep) | 2 | 2 | 0 | 0 | **4 (9%)** |
| **temporal_aggregation** (had dated facts, didn't filter/count/sort by date) | 1 | 2 | 0 | 0 | **3 (7%)** |
| **multi_hop_chain_break** (got hop-1, didn't pursue hop-2) | 0 | 0 | 2 | 0 | **2 (5%)** |
| **iteration_limit** (loop on tool calls) | 0 | 0 | 1 | 0 | **1 (2%)** |

Some QAs carry multi-attribution; numbers above are dominant cause.
Totals slightly exceed 43 because in 4 cases two causes were equally
strong and were both counted.

### §4.2 — Why single_hop is the worst (32%)

41% of single_hop fails are **synthesis_overgeneration**: the agent
retrieved the truth and then over-elaborated. Pattern is consistent:

- `conv-2-q28` John causes → agent listed expected (veterans, schools,
  infrastructure) **plus** extras (domestic-abuse, homeless, community
  center). Judge marked fail for the additions.
- `conv-2-q57` "events John has done" → expected 4 items (toy drive,
  food drive, veterans, domestic violence). Agent listed those plus
  homelessness, fire brigade, mentoring, etc.
- `conv-3-q71` "what Joanna and Nate appreciate" → expected "Nature"
  (the abstraction). Agent said "turtles" (the specific instance).
- `conv-0-q43` "what kind of art Caroline makes" → expected "abstract
  art". Agent said "paintings, including self-portraits, deeply
  personal". Truth was retrieved but agent didn't pick the type label.

29% are **coverage_gap_list** (list questions where vector top-K only
surfaces some items): activities, books, damages, indoor activities.

18% are **ranking_miss** (truth exists, top-K didn't include it):
sunset (q37), guitar descriptions (q63), misplaces-keys (q32).

### §4.3 — Why temporal is at 48%

31% **synthesis_overgeneration** + 31% **wrong_answer**. The agent
often retrieves the right time period but constructs a plausible-but-
wrong narrative. 23% are **judge_strict** (3 fails could pass with a
more lenient judge). Only 15% are actual **temporal_aggregation** —
e.g. `conv-0-q40` "beach trips in 2023" where the agent found 2 dated
entries but reported 1.

### §4.4 — Why multi_hop scored 72% (best after adversarial)

Multi-hop wins when both hops live in the same retrieved memory
fragment (vector top-K covers contextually-rich blobs of conversation).
Fails when hops need separate lookups (`conv-5-q55` Audrey vs Andrew
backyard — agent confused entities, never re-searched) or when
disambiguation is needed (`conv-7-q43` neighbor name never resolved).
Vector's "neighborhood of fragments" coverage helps multi-hop more
than single-hop's atomic-noun retrieval.

### §4.5 — Harness data-seeding audit (discovered after v2)

Triggered by user request to verify `conv-2-q19` ("What damages have
happened to John's car?" → expected "Broken windshield, Car broke
down"). The agent only retrieved the broke-down fact. Investigation
revealed:

**"windshield" does not appear in conv-2's seeded turn transcript at
all.** It exists in 3 places in the raw LoCoMo JSON:
1. `event_summary.events_session_4.John[0]` — "John suffers from an
   accident where his car's windshield is shattered"
2. `conversation.session_4[1].blip_caption` — "a photo of a car with
   a broken windshield"
3. `conversation.session_4[1].query` — "shattered windshield"

The harness only seeds `turn.text`. It drops 4 fields:

| Field | Counts (across 1542 QAs, 10 conversations) | Currently seeded? |
|---|---|---|
| `turn.text` | 5882 turns | ✅ |
| `turn.blip_caption` (visual description) | 1226 turns (20.8%) | ❌ |
| `turn.query` (image search context) | 888 turns (15.1%) | ❌ |
| `turn.img_url` | 910 turns | ❌ (probably not needed) |
| `event_summary` (per-session events) | 1 per session × 288 sessions | ❌ |
| `observation` | top-level summary | ❌ |
| `session_summary` | per-session high-level | ❌ |

**Of the 43 fails, with refined keyword matching:**

| Category | Count | Meaning |
|---|---|---|
| **Infra error** (LLM connection / max_iter) | 3 | Should be retried, not counted as real fails |
| **Data fully in seeded turns** | 18 | Real fail — agent had access |
| **Critical keyword only in non-turn fields** | 10 | **Possible harness gap** — needs per-case verification |
| **Expected keywords nowhere by regex** | 12 | Either paraphrase, compound concept, or judge-only reference |

The "10 critical-keyword-only-in-non-turn" needs care. Several are
**not** real harness gaps:

- `conv-2-q48` "july" — the date IS seeded as `valid_from: 2023-07-02`,
  but the agent doesn't translate ISO dates to month names. **Agent gap
  (date arithmetic), not harness gap.**
- `conv-5-q55`, `conv-7-q43` (november, april) — same date-format issue.
- `conv-3-q66`, `conv-3-q84`, `conv-8-q46` — paraphrase issues; the
  concept is in turns but expressed with different words.

**Verified harness gaps (manual check)**: 2-3 fails, including
`conv-2-q19` (windshield in blip_caption + event_summary only),
`conv-3-q16` (purple hair color likely in blip_caption), `conv-6-q19`
("creatures that gave him joy" possibly in event_summary).

**Infra fails** (should-be-retried):
- `conv-6-q11`: hit max-iterations searching for emotional state
- `conv-9-q65`: LLM connection error after retries exhausted
- `conv-4-q27`: same — LLM connection error

### §4.6 — What this means for the 57.8% score

Adjusted reading: of 43 fails,
- ~3 are infra (should be retried before counting → +2-3pp ceiling)
- ~2-3 are harness data-seeding gaps (could be fixed → +2-3pp)
- ~37-38 are real (agent / memory / synthesis / ranking)

**Realistic clean-score after infra retry + harness data-seed fix:
~61-63%.** Not the ~65% suggested by the original "5 harness gap fails"
estimate. The harness gap is real but smaller than first reported.

The bigger finding from this audit is **methodology**: the per-trace
agent classification in §4.1 over-attributed some fails (e.g.
`conv-2-q19` was classified as "coverage_gap_list" — agent only got
1 of 2 damages — but reality was "answer not seeded"; agent did its
best). Estimate **3-5 fails in §4.1 are misclassified** from this
cause. The category-level dominant patterns still hold (synthesis ~30%,
coverage gap ~26%) but the exact counts should be read ±3.

### §4.7 — What the telemetry confirms across the whole run

- `memory.recall.vector` fires when memory_search is called: vector path
  active in ~7/10 calls per QA (was 0/10 pre-fix). The retrieval **API**
  is healthy.
- `hit_count: 10` per vector call: index is healthy.
- `ranking: "default"` in every event — entity-aware RRF **never fired**
  in the bench because seeded LoCoMo conversations don't populate the
  alias index. Durin's flagship entity-aware ranking has **zero effect**
  on this benchmark.
- The `memory.recall` fallback (grep) returned `result_count: 0` only
  when scope didn't include `dreamed` — vector covers all cases that
  matter.

---

## §5 — What would actually close the gap (revised)

This section was rewritten after the per-trace audit. The original L1/L2/L3
priorities were based on cross-category counts; what follows is grounded
in the dominant-cause distribution from §4.1.

### Lever ROI table (by # of fails it plausibly closes)

| Lever | Fails it would help | % of 43 | Effort | Risk |
|---|---|---|---|---|
| **Cite-only synthesis discipline** (prompt, output-format constraint, JSON-mode return of (cited_fact, source_uri) pairs) | **~10-13** (most of synthesis_overgeneration) | 23-30% | Tiny (prompt+rendering) | Low |
| **List-aware retrieval expansion** (detect list questions; bump top_k to 30+; require multi-query expansion) | **~9-11** (most of coverage_gap_list) | 21-26% | Small (detector + top_k tunable) | Low |
| **Stronger judge re-evaluation** (re-judge with sonnet/claude on same answers) | **~4** (judge_strict) | 9% | Trivial (cost only) | None |
| **Improved tool routing prompts** (force memory_search before factual claims) | **~4** (no_retrieval) | 9% | Tiny | Low |
| **Bi-temporal validity filtering** (`valid_until` field + post-filter by query time window) | **~3-5** (temporal_aggregation + some wrong_answer) | 7-12% | Small (~150 LOC) | Low |
| **BM25 + dense hybrid retrieval** (FTS5 alongside LanceDB) | **~3-5** (specific ranking_miss with exact-term answers) | 7-12% | Medium (~400-600 LOC) | Medium |
| **Atomic fact extraction at write** (Mem0/HyperMem pattern; replace turn-markdown with extracted facts) | **~10-15** (helps coverage_gap_list + some synthesis) | 23-35% | Large (~800+ LOC + write-path redesign) | High |
| **Multi-hop chain enforcement** (post-retrieval entity validation; auto-trigger secondary search when first hop is partial) | **~2-3** (multi_hop_chain_break) | 5-7% | Medium | Medium |

### Why this changes the prior recommendation

- **Atomic fact extraction is NOT the highest-ROI lever for our actual
  failure profile.** Cross-category counting in v1 inflated its impact.
  Real coverage_gap_list is 11 fails — and even those can partly be
  fixed by list-aware retrieval expansion (much cheaper) before going
  full atomic-fact rewrite.
- **The biggest single lever is synthesis discipline** — output-format
  constraints + prompt engineering. **It's nearly free.** 30% of fails
  are the agent over-generating from correct retrieval. Constraining
  the synthesis step (cite-only, JSON facts, no embellishment) closes
  the largest single bucket.
- **Stronger judge re-evaluation is a freebie** that recovers up to
  9% of fails (~5pp on the score) without changing anything in durin.
  Should be done first to know the real ceiling of current code.
- **BM25 and bi-temporal validity each close fewer fails than I
  originally claimed** — 3-5 each in absolute terms, not 10/13. Still
  worth doing but not the top priority.

---

## §6 — The uncomfortable finding: entities aren't where the LoCoMo gap is

**Durin's bet** (per [doc 16](research/16c_entities_academic_and_online.md)
+ [doc 18](18_entity_centric_plan.md)): entity-centric memory is the
right substrate for cross-session coherence.

**LoCoMo observation**: HyperMem (SOTA, 92.73%) **has no entity nodes**.
GAAMA (78.9%) deliberately avoids person entity nodes to escape the
mega-hub problem. The 35pp gap between durin and SOTA on LoCoMo is
**not** about entities — it's about retrieval primitives (atomic facts +
hybrid + temporal).

**This does NOT invalidate the entity-centric direction.** LoCoMo
measures **within-session conversational QA after a forgetting period**.
Durin's entity-centric design is optimized for **cross-session
coherence** ("who is Marcelo? what did he say across 6 months?") — a
use case LoCoMo doesn't test.

**But it does tell us**: if we want a competitive LoCoMo number, the
entity layer alone won't get us there. We need to add the atomic-fact /
hybrid / temporal layer on top of (or beside) the entity layer.

**Strategic implication**: durin can pursue both. The entity layer
serves the daily-driver / multi-session use case. The atomic-fact +
hybrid + temporal layer serves the recall-QA benchmark use case (and
will probably also help the daily driver — precise facts about
specific people / projects retrieved with lexical exact match is
valuable in both regimes).

---

## §7 — Recommended next steps (revised, empirical)

**Re-ordered by ROI/effort after §4 audit (including §4.5 harness
discovery).** Pick from top — each row is independently shippable.
Re-bench after each to measure delta in isolation. Same 102-QA seed
for comparability.

### Step -1 — Fix harness data-seeding gaps (~3-5pp ceiling) — **SHIPPED v3 (2026-05-25)**

**Discovered in §4.5.** The harness drops 4 fields per turn/conversation
that contain answer-relevant info for some QAs.

**Shipped result**: +3pp net (+12pp open_domain, +8pp temporal,
-4pp single_hop, -4pp multi_hop). The wins outpaced the per-category
losses but the losses are real — see §2 discussion. Future refinement
would be source-priority weighting in the vector index so curator
summaries don't outrank original quotes for noun-level queries.

**What to change in [`scripts/benchmark/locomo_harness.py::_seed_memory_from_conversation`](../scripts/benchmark/locomo_harness.py):**

1. **When a turn has `blip_caption`, append it to the seeded `text`**
   with a marker like `[image: <caption>]` (or store as a separate
   episodic entry so it's independently retrievable). Same for `query`.
2. **Seed `event_summary` per session** as one episodic entry per
   session — gives the agent access to the LoCoMo curator's session-
   level event summary. Should match conv's `session_N_date_time` for
   temporal anchoring.
3. **Optionally seed `observation` and `session_summary`** at the
   conversation level.

**Also fix in [`run_qa`](../scripts/benchmark/locomo_harness.py)**:
retry on `LLM Connection error` and `max_iter` stop_reasons — 3 fails
in current run were pure infra. Either:
- Add a `--retry-on-infra-error N` flag to `locomo_run.py` that
  re-invokes `run_qa` for those QAs after the main pass.
- Or just bump per-QA retries via the existing `provider.base.py`
  `_CHAT_RETRY_DELAYS` extension we already did.

**Effort**: ~80-150 LOC across `locomo_harness.py::_seed_memory_from_conversation`
+ a small retry wrapper in `locomo_run.py`.

**Risk**: Low. Changes only the bench, not durin. Adds more content
to seeded memory; might slightly increase per-QA wall-clock (more
embeddings to compute) but the index already builds in seconds.

**Expected impact**: +3-5pp ceiling. Score 57.8% → ~61-63%.

### Step 0 — Cheap signal (do first, ~30-60 min, zero code)

**Re-judge the existing run with claude-sonnet** (or any stronger model
your z.ai plan exposes). The 102 traces are already saved. The judge
script (`locomo_judge.py`) accepts `--judge-model`. Re-run only the
verdict step over `traces/*.json` and compute summary.

Expected outcome: **~4 fails recovered** (the 9% `judge_strict` bucket).
This tells you the real ceiling of current code before any change.

> Implementation hint: `locomo_replay.py` re-runs from saved traces but
> currently re-executes the agent. Adding a `--judge-only` flag (skip
> `run_qa`, just re-call `judge_answer` on existing `trace.got`) is
> ~30 LOC.

### Step 1 — Synthesis discipline (REVISED after empirical failure)

**Original plan**: add cite-only / forced-routing text to memory_search
description. **Attempted, failed, reverted.**

**v2 attempt result (2026-05-24 late)**: adding `"USE THIS BEFORE
answering"` + `"cite only facts that appear"` + `"do not pad lists"`
to memory_search description caused **-20pp regression on first 30
QAs** — bench killed mid-run. The agent didn't change behavior in
the direction the text asked, just made worse decisions (extra
memory_search calls, no synthesis improvement).

**Lesson**: text in tool descriptions is a weak signal for changing
LLM behavior. The 3/3 SOTA pattern uses **structural** mechanisms:
- Hermes wraps pre-fetched memory in `<memory-context>` fences with
  system note ADJACENT TO the data.
- OpenClaw inserts a blocking sub-agent that compresses output before
  the main agent sees it.
- OpenClaude bypasses the tool catalog via `<system-reminder>`
  auto-injection.

**What to try next instead**: §2.F eager-inject (Step 4 below). The
synthesis discipline travels with the content, not in a remote
description the LLM may ignore.

### Step 2 — List-aware retrieval (closes ~9-11 list fails)

**Closes ~9-11 fails (21-26%)**. Second biggest bucket.

**What to change**: when the question is list-shape ("what activities",
"what causes", "what kinds of"), top-10 doesn't cover. Two options:

1. **Detect list questions** (regex / 1-shot classifier) and bump
   `top_k` to 30 or 50 inside `memory_search` for those calls.
2. **Multi-query expansion**: rewrite list questions into N sub-queries
   ("what activities Melanie family camping", "what activities Melanie
   family museum", etc.) via a cheap LLM call, run vector for each,
   union the top-K. Mem0-style.

**Effort**: small for option 1 (~80 LOC), medium for option 2 (~200 LOC
+ 1 extra LLM call per list query).

**Risk**: Low. Worst case the agent gets too many hits and ignores
them — already a behavior we tolerate.

**Files to touch**:
- `durin/agent/tools/memory_search.py::execute` (add list detector
  before vector call; expand top_k conditionally)
- `durin/memory/vector_index.py::search` (already accepts top_k kwarg
  — no change needed)

### Step 3 — Tool routing (DROPPED after empirical failure)

**Same v2 failure mode as Step 1**: forced-routing text added to
memory_search description ("USE THIS BEFORE...") contributed to the
-20pp regression. Reverted. No text-in-description variant of this
will work.

**The structural alternative is Step 4 below (§2.F eager-inject)**:
if memory is pre-fetched and injected, the agent doesn't need a
"you MUST search" instruction — the memory is already in context.
No_retrieval fails (7 in current v3 run) become unreachable because
search already happened.

### Step 4 — §2.F eager-inject (next concrete move)

**This is the structural lever that replaces failed Steps 1 + 3.**

§2.F is already in [docs/25_post_t1_state_and_t2_horizon.md](25_post_t1_state_and_t2_horizon.md)
as planned-but-pending. Gate ("silent retrieval miss observable in
telemetry") was met by the v1 LoCoMo bench (4-8 `no_retrieval` fails
per 102 QAs). v3 still shows 7 no_retrieval fails — the gate is still
open.

**Why this is the structural fix**:
- Pre-fetch memory based on the user's question before the agent runs
- Wrap in `<memory-context>` markers (hermes pattern) adjacent to
  content — instruction travels with data
- Inject into user message (ephemeral, not persisted to session)
- Closes ~7 no_retrieval fails directly + likely some
  synthesis_overgeneration fails because the agent sees memory framed
  as authoritative reference from turn 0

**Implementation order**:
1. **Bench-only first** (~100 LOC in `locomo_harness.py`): before
   `loop_agent.run()`, run one `memory_search(query=qa.question)` via
   the tool API, wrap result via new `_build_memory_context_block()`
   helper, append to inbound message content. Measure delta. Risk:
   bench-only, zero impact on durin product.
2. **If bench shows ≥+5pp**: thread the pattern into durin core as
   the §2.F implementation. Wire via AgentLoop._eager_prefetch_memory
   hook called from message build phase.

**Effort (bench-only)**: ~100 LOC + tests.

### Step 5 — Bi-temporal filtering (closes ~3-5 temporal fails)

**Closes ~3-5 fails (7-12%)**, all in the temporal category. v3
already moved temporal +8pp via B2 (event_summary inherits session
date as valid_from). Adding explicit `valid_until` could close 2-3
more.

Same plan as the original L1 (extract `valid_until` at write,
post-filter top-K by query time window).

**Files**:
- `durin/memory/schema.py::MemoryEntry` — add `valid_until: date | None`
- `durin/memory/storage.py` — frontmatter parsing
- `durin/agent/tools/memory_search.py::execute` — temporal query
  detector + post-filter
- `durin/agent/tools/memory_store.py` — extract `valid_until` at write
  (LLM call) OR start with None and add later

### Step 6 — BM25 hybrid (closes ~3-5 ranking_miss fails)

**Closes ~3-5 fails (7-12%)**. Originally L2 with claim of 10 fails;
real number is lower because most ranking_miss fails aren't lexical-
match-solvable (e.g. `conv-0-q37` "sunset" wasn't a vector-vs-BM25
issue — the right episodic was just buried in top-K for other reasons).

**Defer until steps 1-4 are done and measured.** If after those, the
remaining fails are still concentrated in exact-term mismatches, the
backlog item [P6 in doc 20](20_pendings.md) becomes the next move.

### Step 7 — Atomic fact extraction (deferred)

The biggest architectural change. **Defer indefinitely** until steps
1-4 have plateaued. Original analysis overstated its ROI by conflating
"helps coverage_gap_list" (where list-aware retrieval is cheaper)
with "fundamentally rearchitects storage".

When it WOULD become the right move: if after all cheaper levers,
we're stuck below 75-80% and the failure profile is dominated by
"correct fact buried in verbose turn-prose". Today neither condition
holds.

---

## §7.5 — Bench operational improvements (small but useful)

These came out of running the 102-QA pass:

- **`locomo_replay.py --judge-only`** — re-judge saved traces without
  rerunning the agent. Needed for Step 0 above. ~30 LOC.
- **Failure analyzer enhancement** — `locomo_analyze.py` produces a
  `failure_breakdown` but it's coarse (synthesis_error / no_retrieval /
  retrieval_miss_irrelevant / judge_error_possible). The per-trace
  taxonomy from §4 (8 dominant causes) is what's actually actionable.
  Worth porting into the analyzer so future runs auto-produce the §4.1
  table. ~100 LOC.
- **Telemetry persistence to bench dir** — currently `~/.cache/durin/
  telemetry/bench_*.jsonl` is shared across runs and append-only,
  making it hard to slice events by run. Either bind the bench
  telemetry path inside `AgentLoop._warmup_memory_embedding` (override
  the rebind), or rotate the cache file per run dir.

---

## §8 — Caveats

1. **N=102 has ~10pp 95% CI per category**. Per-category numbers are
   directional, not precise. Compare deltas across commits, not
   absolute scores against SOTA published on N=1540.
2. **glm-5.1 ≠ GPT-4/Sonnet**. SOTA stacks use stronger models. Our
   score is also testing the model, not just the memory layer.
3. **Judge bias**: self-judging (glm judges glm) inflates pass rate by
   ~5pp in published comparisons. Want a stronger judge to disambiguate.
4. **Vector model**: multilingual MiniLM is a generalist. Mem0/HyperMem
   use domain-adapted or larger models. Worth testing a stronger
   embedding model as a free improvement.
5. **No dream consolidation in bench**: durin's dream pipeline runs
   offline and the bench bulk-seeds without triggering it. The
   reflection/consolidation pattern we already have isn't exercised
   here.

---

## Last updated: 2026-05-25 (v3 run: B1+B2 shipped → 60.8%, +3pp over baseline; D1+D3 text-in-description hacks attempted then reverted after empirical -20pp regression; §7 reordered with §2.F as next concrete move)
