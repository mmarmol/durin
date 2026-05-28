---
title: Audit reconciliation doc ↔ code (2026-05-28)
version: 1.0
status: living document — closed item by item
last_updated: 2026-05-28
audience: humans + LLMs closing doc/code debt
depends_on: docs/memory/00..10 (audited)
---

# Audit reconciliation — doc vs code

This doc lists each discrepancy found between `docs/memory/00..10` and the real code in `durin/`. Each item includes:

- **Doc says** — verbatim quote + `file:line` cite
- **Code says** — verbatim quote + `file:line` cite
- **Who is right** — evaluated with justification
- **Proposed action** — fix code, fix doc, or both
- **State** — `pending` / `resolved` / `wontfix`

**Rule**: assume nothing. Only what is verified with direct `grep`/`read` against the current code counts as "code says".

**Order**: critical (1-10), medium (11-22), low (23+). We resolve one by one in order, starting with those that can break agent UX.

---

## CRITICAL — affect agent UX or operation

### A1 — `memory_ingest`: description promises API the schema doesn't implement

**Doc says** (`docs/memory/04_agent_tools.md:200-209`):

```json
{
  "source": "string (required, can be: file path, URL, or 'inline')",
  "content": "string (required if source='inline')",
  "title": "string (optional)",
  "entities": "array of <type>:<value> strings (optional)",
  "chunking": "auto | none (default: auto)"
}
```

**Code says** (`durin/agent/tools/memory_ingest.py:42-47`):

```python
_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to a markdown or "
        "plain-text file the user wants the agent to remember."
    ),
    required=["path"],
    ...
)
```

The **canonical description** synchronized with `docs/memory/06_prompts_and_instructions.md` §3.3 (lines 48-65 of the same file) also publishes `source`/`URL`/`"inline"`/`content` to the LLM. The LLM then invokes `memory_ingest(source="https://...", content=...)` and fails with `unknown parameter`.

**Who is right**: ambiguous. The original intent (doc) is reasonable — an ingest tool should accept URL and inline. The implementation fell short. **The doc is the correct direction**; the code is incomplete.

**Action**: extend the schema and the logic of `memory_ingest.execute` for:
- `source` (req): file path | URL | "inline"
- `content` (opt): text when `source="inline"`
- `title` (opt)
- `entities` (opt)
- `chunking` (opt, default auto)

Keep compatibility with `path` (alias or migration step).

**Risk**: implementing URL fetch raises questions (timeouts, SSRF, content-type sniffing). If reducing scope is preferred: **align the doc to the code** (only `path`) and leave URL/inline as deferred. Human decision.

**Resolution (2026-05-28)**: Option 2 — align the doc to the code. Key reason discovered during the decision: **`web_fetch` already exists** ([durin/agent/tools/web.py:454](durin/agent/tools/web.py#L454)) and already does URL → markdown with SSRF protection, Jina/readability extractors, image detection. The URL branch in `memory_ingest` wasn't a missing capability but a **pending duplication**. Similar for "inline": `memory_store(class_name="corpus")` covers the case. Changes:

- `_PARAMETERS["description"]` in [memory_ingest.py:48-68](durin/agent/tools/memory_ingest.py#L48-L68) rewritten to reflect only `path` + direct to the correct workflow (`web_fetch` + `memory_store`).
- [docs/memory/04_agent_tools.md](docs/memory/04_agent_tools.md) §4.1, §4.2, §4.3 and §10 (status table) updated.
- [docs/memory/06_prompts_and_instructions.md](docs/memory/06_prompts_and_instructions.md) §3.3 synchronized.
- [docs/memory/08_scope_and_discarded.md](docs/memory/08_scope_and_discarded.md) §2.8 new entry with the genealogy of the error and the lesson on sync tests.

**Sync test lesson**: `test_tool_description_sync.py` validates string equality, not behavior. It passed green with the doc lying to the LLM from commit `572d5cf` (2026-05-28 09:28 +0200) until the fix `bce9092` (~1 hour later). The drift was short by luck — the audit caught it the same morning, but the test would never have detected it. General fix for "sync" tests in the future: exercise the behavior, not just compare strings.

**State**: resolved (commit pending).

---

### A2 — `memory_store` parameters diverge between doc, code, and internal description

**Doc says** (`docs/memory/04_agent_tools.md:134-144`):

```json
{
  "headline": "string (required)",
  "body": "string (required)",
  "class_name": "stable | episodic (default: episodic)",
  "entities": "array of <type>:<value> strings (optional)",
  "summary": "string (optional, default: auto-generated)",
  "source_refs": "array of strings (optional)",
  "valid_from": "ISO date (optional)"
}
```

**Code says** (`durin/agent/tools/memory_store.py:24-68`): parameters = `content` (req), `class_name` (enum includes `corpus`/`pending`), `headline` (opt, auto-gen), `summary` (opt), `source_refs` (opt), `entities` (opt), `force` (opt). **There is no `valid_from`. There is no `body` — it's called `content`.**

**Canonical description in the code itself** (`memory_store.py:83`, synchronized with doc 06 §3.2):
> *"Keep `headline` short and specific. `body` should be the full content; don't truncate."*

→ The tool itself talks about `body` in the description to the LLM, but the real parameter is `content`. **The code is inconsistent with itself.**

**Who is right**: partially each one.
- `content` vs `body`: the code is older, doc 04 proposed `body`. Renaming the parameter to `body` doesn't break anything external (tools are only invoked via schema), but breaks internal tests and code that calls `store_memory(content=...)`. **Better: update doc 04 and the tool description to `content` to minimize change** — the data is there, only the name differs.
- `valid_from`: doc proposes it, code doesn't have it. Not actionable today (it's not used for temporal scoring because decay is not wired, see A9). Defer until decay operates — then it makes sense.
- `force`: exists in code (skip-dedup), doc 04 doesn't mention it. **Doc is right in the sense of "the agent should never see it"** — `force=true` is for humans/tools using the tool programmatically. But since it's exposed to the LLM, it should be documented or removed from the schema and exposed only via internal API.
- `class_name` enum: code includes `corpus`/`pending`; doc says "stable | episodic". The code is correct — the LLM should be able to store `corpus` (although normally `memory_ingest` does it) and `pending` (TODOs). **Doc out of date.**

**Action**:
1. Doc 04 §3.1: rename `body` → `content`, extend `class_name` enum, add `force` (with caveat "rarely relevant"), mark `valid_from` as deferred.
2. Tool description (`memory_store.py:83`): change `"body"` → `"content"`.
3. Re-run the sync test afterwards.

**Resolution (2026-05-28)**: five discrepancies audited individually. Changes shipped:

1. **`pending` removed from agent-facing enum** ([memory_store.py](../../durin/agent/tools/memory_store.py): new `_AGENT_FACING_CLASSES = ("stable", "episodic", "corpus")` replaces `list(MEMORY_CLASSES)`). Verified reason: `paths.py::walk_memory` + `indexer.py` + `file_watcher.py` all exclude `memory/pending/**`. Writing there from the LLM was silent data loss. Internal callers (compaction) keep using the pure `store_memory` function.

2. **`body` → `content` in doc 04 §3.1**. The persisted field of `MemoryEntry` IS called `body` (declared in doc 01 §3.3), but the tool parameter and the pure function were always `content`. Doc 04 v1 conflated the two planes. Doc 04 v2 makes the asymmetry explicit.

3. **`valid_from` is NOT exposed as a tool param**. It's a real field of `MemoryEntry` with legitimate downstream uses (hot_layer cursor compare, entity_ranker pre/post, fragments sort). Automatic default `date.today()`. **The consumer that needs to back-date (LoCoMo bench) uses the pure function directly** ([locomo_harness.py:227-233](../../scripts/benchmark/locomo_harness.py)), not the tool. 99% of LLM stores are "now" — exposing the knob adds noise to the schema with no real use case.

4. **`headline` stays optional**. Auto-gen [`store.py:106-109`](../../durin/memory/store.py) uses the first ~10 words; reasonable for LLM-generated content. Required would add latency with no clear benefit.

5. **`force` documented** in doc 04 §3.1 with caveat ("rarely relevant"). Exists in code since commit `d34b337` for the dedup near-duplicate bypass; doc 04 v1 omitted it by oversight.

Changes to the canonical ([doc 06 §3.2](06_prompts_and_instructions.md)) reflect the 5 points; `_PARAMETERS["description"]` synchronized verbatim. Doc 04 §3.1/§3.2/§3.3/§9 (decision 5b) updated. New entry [doc 08 §2.9](08_scope_and_discarded.md) with full justification + lessons (enum-as-trap, param-vs-field, default-beats-knob).

**New lessons**:
- *Enum values can be traps* — don't blindly mirror a constants tuple to a tool-facing enum without verifying that THE WHOLE system honors each member.
- *Tool param name ≠ persisted field name* — when they differ, document BOTH planes explicitly.
- *Default behavior often beats new tool params* — before exposing a knob, ask who really needs it; if it's an internal pipeline, leave the pure function as its path.

**State**: resolved (commit pending).

---

### A3 — `memory_search` `limit` documented but not exposed

**Doc says** (`docs/memory/04_agent_tools.md:42`):

```json
"limit": "integer (default: 10, max: 50)"
```

**Code says** (`durin/agent/tools/memory_search.py:53-77`): `_PARAMETERS` only has `query`, `scope`, `level`, `keywords`. The limit is hardcoded to 10 at `memory_search.py:348`:

```python
pipeline_result = run_search_pipeline(
    self._workspace,
    query,
    keywords=keywords,
    vector_index=vi,
    limit=10,                # ← hardcoded
    ...
)
```

**Who is right**: **doc is right**. Exposing `limit` to the LLM is useful — some queries want top-3 (short chat), others want top-30 (audit). Today the LLM has no control.

**Action**: add `limit: IntegerSchema(default=10, min=1, max=50)` to the schema, pass it to `run_search_pipeline`.

**Resolution (2026-05-28)**: Option A — expose `limit`. Unlike A1 (URL duplicated `web_fetch`) and A2 (several knobs were traps), here **the pipeline already supports the parameter** ([search_pipeline.py:71](../../durin/memory/search_pipeline.py#L71)), only propagation from the tool was missing. Doc 03 §1 and Doc 04 §2.1 both proposed it — consistent proposal, not an isolated invention.

Changes:
- Schema: `limit: IntegerSchema(10, minimum=1, maximum=50)` added to [memory_search.py](../../durin/agent/tools/memory_search.py).
- `execute()`: defensive clamp `max(1, min(50, int(...)))` with fallback to 10 when coercion fails.
- Pipeline call: `run_search_pipeline(..., limit=limit, ...)` instead of the hardcoded `limit=10`.
- Doc 06 §3.1 canonical + tool description: brief mention with guidance ("3-5 for chat-short, 20-30 for audit/investigative, hard cap 50").
- New test [test_memory_search_limit_param.py](../../tests/memory/test_memory_search_limit_param.py): 7 tests that **exercise the behavior**, fulfilling the lesson in [[feedback-sync-tests-exercise-behavior]]:
  - Schema declared correctly.
  - Default 10 when omitted.
  - `limit=5` trims to 5.
  - `limit=30` allows more (with 25 seeded entries).
  - `limit=999` clamps to 50.
  - `limit=0` clamps to 1.
  - `limit="abc"` fallback to 10 (string-coerce graceful).

**Pre-commit verification**:
- `IntegerSchema(value, description, minimum, maximum)` signature against [schema.py:54-72](../../durin/agent/tools/schema.py#L54-L72).
- `tool_parameters_schema(...)` returns dict (not object), test corrected after first false attempt — application-type error of [[feedback-verify-quantifiers]].
- Default 10 = previous behavior: **no breaking change**.

**State**: resolved (commit pending).

---

### A4 — LanceDB schema in doc 02 §3.1 ≠ actual columns

**Doc says** (`docs/memory/02_indexing.md:65-79`):

| Column | Type |
|---|---|
| `uri` | string PK |
| `path` | string |
| `type` | string (`entity`, `episodic`, `stable`, `corpus`, `session_summary`) |
| `entity_type` | string \| null |
| `entities` | list of strings |
| `vector` | fixed list of floats (**768**) |
| `mtime` | float |
| `headline` | string |
| `summary` | string |
| `valid_from` | string \| null |
| `indexed_at` | string |

And *"**No `body` column.** Storing the body in LanceDB would double the index size for no retrieval benefit."*

**Code says** (`durin/memory/vector_index.py:131-147`):

```python
record: dict[str, Any] = {
    "id": entity_ref,
    "class_name": "entity_page",
    "summary": summary,
    "headline": name,
    "vector": vec,
    "valid_from": "",
    "entities": [],
    "path": str(rel_path),
    # P2.5: full body for cold-tier reads without disk hits.
    "body": body or "",
}
```

Actual columns: `id, class_name, summary, headline, vector, valid_from, entities, path, body`. Vector dim: 384 (default model `paraphrase-multilingual-MiniLM-L12-v2` emits 384, not 768 — `vector_index.py:444` comments migration "from 384-dim to 1024-dim").

**Who is right**: **code is right**. P2.5 (commit `a266344`) added `body` deliberately as an explicit trade-off (double index size vs save N file reads for cold queries). The doc was never updated.

**Action**: update doc 02 §3.1:
- Rename `uri` → `id`, `type` → `class_name`. Remove `entity_type`, `mtime`, `indexed_at` (don't exist).
- Add `body` with the P2.5 justification (doubling size is acceptable because it saves cold-tier disk hits).
- Correct dim 768 → 384.
- Update §3.2 "Dim: 768" → "Dim: 384 (default; changing requires full rebuild)".

**Resolution (2026-05-28)**: during the analysis the user pushed back with the key question — *"the real doc is kept on disk so as not to replicate all the information; the source of truth is the doc on disk, not the database"*. Column-by-column verification showed that `body` was the ONLY field that duplicated substantial `.md` content in LanceDB. P2.5 (commit `a266344`, 2026-05-28 09:10) had violated the original architectural principle for a latency optimization (~5-10 ms saved in cold-tier disk reads) that was NOT a bottleneck — the downstream LLM call takes seconds.

**Decision**: revert P2.5 + align doc to the real schema. Code changes:

- [vector_index.py:131-147](../../durin/memory/vector_index.py#L131-L147) (entity-page record) and [:360-377](../../durin/memory/vector_index.py#L360-L377) (entry record): remove the `body` field from the dict. Explanatory comment points to doc 08 §2.10.
- [search_pipeline.py:294-298](../../durin/memory/search_pipeline.py#L294-L298): the cross-encoder rerank no longer uses `meta.get("body")`. Inline doc in the function explains that if CE quality requires body in the future, the solution is a top-N disk fetch inside the CE step, NOT a column in LanceDB.
- [search_pipeline.py:445-449](../../durin/memory/search_pipeline.py#L445-L449): `_resolve_meta` no longer threads `body` from vector hits.
- [sectioned_output.py:60](../../durin/memory/sectioned_output.py#L60): `SectionedHit.body` kept as field with default `""` (backward-compat; cold-tier callers fall back to `_enrich_body`).
- [index_meta.py:47](../../durin/memory/index_meta.py#L47): `CURRENT_SCHEMA_VERSION` bumped 2 → 3 to force clean rebuild of existing v2 tables (which would have the orphaned `body` column). The `ensure_index_fresh` check (P2.2) triggers it automatically on the next `memory_search.execute`.
- [tests/memory/test_vector_index_no_body_column.py](../../tests/memory/test_vector_index_no_body_column.py): 2 new tests that assert the post-A4 invariant — if anyone re-introduces the column, the test fails with a specific message pointing to doc 08 §2.10.

Doc changes:

- [docs/memory/02_indexing.md §3.1](02_indexing.md): 8 real columns in the schema table. Dedicated block explaining *"no body column — body lives on disk"* with architectural justification + reference to doc 08 §2.10. Clarification of the `id/class_name` (LanceDB) vs `uri/type` (FTS5) asymmetry.
- [docs/memory/02_indexing.md §3.2](02_indexing.md): dim corrected (default 384, not 768); alternatives listed (e5-large 1024-dim, MiniLM-L6 384-dim).
- [docs/memory/02_indexing.md §3.3](02_indexing.md): `entity_page` instead of `entity`; note about session_summary which is NOT emitted today (delegated to A10).
- [docs/memory/02_indexing.md §5.1](02_indexing.md): note about the asymmetry with LanceDB + how FTS5 also honors the principle (indexes the `text` but never returns it).
- [docs/memory/02_indexing.md §11 status](02_indexing.md): vector index row updated with the current schema.
- [docs/memory/08_scope_and_discarded.md §2.10](08_scope_and_discarded.md): permanent entry with genealogy + 5 revert reasons + lesson on optimization vs principle + lesson on symmetry between indices.

**New lessons** (to save in persistent memory):

- *"An optimization that violates an architectural principle must be justified by measurement, not intuition"* — P2.5 saved ~10ms in an operation dominated by LLM latency of seconds.
- *"The fix for a slow consumer is local to that consumer, not a schema change"* — if CE needs more text, optimize CE; don't add columns to LanceDB that 95% of queries don't use.
- *"Symmetry between components is a feature"* — FTS5 and LanceDB both being "metadata + index, content on disk" makes the system easier to reason about.

**Pre-commit verification**: tests/memory/ 903 passed, 1 skipped (894 base + 7 A3 + 2 A4 invariant).

**State**: resolved (commit pending).

---

### A5 — `memory.dream.end` doesn't emit the cost fields doc 08 §3 R3 needs

**Doc says** (`docs/memory/07_telemetry_and_observability.md:194-206`):

```
Already exists, augment with:
| entities_quarantined | int | NEW |
| llm_call_count | int |
| llm_input_tokens_total | int |
| llm_output_tokens_total | int |
| duration_ms | float |
```

**Code says** (`durin/memory/dream_runner.py:337-354`):

```python
emit_tool_event(
    "memory.dream.end",
    {
        "trigger": trigger,
        "entity_filter": entity_filter or "",
        "entities_consolidated": consolidated,
        "entities_failed": failed,
        "duration_s": duration_s,    # ← seconds, not ms
    },
)
```

Doesn't emit `entities_quarantined` / `llm_call_count` / `llm_input_tokens_total` / `llm_output_tokens_total`. `duration_s` in seconds, not `duration_ms`.

Doc 08 §3 R3 (risk register) proposes alarming on `dream_llm_cost_per_day_usd > $5/day`. **Without the token totals, this alarm is infeasible today.**

**Who is right**: doc is right in intent (dream cost is important to measure), but the implementation requires instrumenting the llm_invoke calls inside DreamConsolidator to capture prompt/completion tokens. That's real work (not a doc fix).

**Action**: implement a token accumulator in DreamRunner, pass as kwargs to `_emit_end`. Rename `duration_s` → `duration_ms` (* 1000.0). Add `entities_quarantined` (the concept already exists in `_maybe_auto_absorb`).

**Resolution (2026-05-28)**: The dream's `LLMInvoke` Protocol was `Callable[..., str]` — it discarded the `response.usage` that litellm does provide. Architectural change local to the correct consumer (the dream namespace) — applying the A4 lesson [[feedback-optimization-vs-principle]]: the fix lives where the consumer is, not as global state.

Changes:

- **[durin/memory/dream.py](../../durin/memory/dream.py)**: new `LLMResponse` dataclass (`text + prompt_tokens + completion_tokens`); `LLMInvoke` Protocol updated to return `LLMResponse`; `default_llm_invoke` extracts `response.usage` from litellm; `ConsolidationResult` gains `prompt_tokens`/`completion_tokens`/`llm_call_count`; `consolidate_entity` accumulates tokens even across retries; `DreamError` gains `triggered_quarantine` flag.
- **[durin/memory/dream_quarantine.py](../../durin/memory/dream_quarantine.py)**: `record_failure` now returns `bool` — `True` when that call triggered the 3rd strike → quarantine.
- **[durin/memory/dream.py::DreamConsolidator.apply](../../durin/memory/dream.py)**: captures the flag from `record_failure` and propagates it in `raise DreamError(..., triggered_quarantine=triggered)`.
- **[durin/memory/dream_runner.py](../../durin/memory/dream_runner.py)**: new `_ConsolidateTotals` dataclass (per-pass accumulator); `_consolidate()` returns the totals; `_emit_end()` payload with the 4 new fields + `duration_ms`.
- **[durin/memory/absorb_judge.py](../../durin/memory/absorb_judge.py)**: extracts `.text` from the response. Does NOT accumulate tokens in `dream.end` (the judge runs POST-dream and has its own `memory.absorb.judged` telemetry).
- **[durin/telemetry/schema.py](../../durin/telemetry/schema.py)**: `MemoryDreamEndEvent` TypedDict updated — 4 new fields, `duration_s` removed.
- **[tests/memory/test_dream_end_cost_telemetry.py](../../tests/memory/test_dream_end_cost_telemetry.py)** (new, 4 tests): exercises the real behavior:
  - `LLMResponse` → tokens in the `dream.end` payload.
  - Legacy `str`-returning `llm_invoke` → tokens=0 (under-report safe-failure).
  - Multi-entity → tokens summed correctly.
  - TypedDict schema has the required fields + removed `duration_s`.

**Backward-compat shim**: the call site in `dream.py:341` and `absorb_judge.py:144` accept BOTH `LLMResponse` AND `str` (`isinstance` check). This allows the ~15 existing tests with `lambda p,**kw: "raw"` mocks to keep passing without mechanical churn — they under-report tokens (0) but the dream flow works.

**Doc 07 §6.2 updated**: complete table with the 9 fields, explicit note that the old `duration_s` field was removed (not additive), note on safe-failure direction when the provider doesn't surface `usage`.

**Doc 08 §3 R3 alarm**: now computable. The formula is `dream_llm_cost_per_day_usd = sum(llm_input_tokens_total * input_rate + llm_output_tokens_total * output_rate)` over the day's `memory.dream.end` events.

**Lessons applied**:
- [[feedback-optimization-vs-principle]]: the change is **local to the correct consumer** (dream namespace). `query_rewriter.LLMInvoke` is left intact.
- [[feedback-sync-tests-exercise-behavior]]: the behavior test doesn't just compare doc strings, **it emits real events and verifies the values**.
- [[feedback-verify-quantifiers]]: during development, the test `test_dream_end_aggregates_tokens_across_multiple_entities` failed with the assumption "slug in prompt matches unique entity" — false because prompts include cross-entity aliases. Fixed with a counter-based stub.

**Pre-commit verification**: tests/memory/ 907 passed (903 baseline + 4 new A5), 1 skipped (condition).

**State**: resolved (commit pending).

---

### A6 — `memory.health_check` payload mismatch

**Doc says** (`docs/memory/07_telemetry_and_observability.md:314-327`):

```
| tick_id | UUID |
| triggered_by | scheduled | eager_post_failure |
| components | dict | {name: {"status": ok|degraded|critical, "details": str|null}}
| restorations_attempted | list[str] |
| restorations_succeeded | list[str] |
| duration_ms | float |
```

**Code says** (`durin/memory/health_check.py:114-120`):

```python
payload: dict[str, Any] = {
    "status": status,
    "components": components,       # flat dict[str, str], not nested
    "drift_count": drift_count,
}
if errors:
    payload["errors"] = errors
```

No `tick_id`, `triggered_by`, `restorations_*`, `duration_ms`. `components` is `dict[str, str]` (flat status), not `dict[str, {"status", "details"}]`.

**Who is right**: piece by piece:
- `tick_id`: useful for correlating logs when there are multiple ticks per hour. **Reasonable to add**.
- `triggered_by`: today there's only scheduled (no eager-post-failure). If there will never be eager, this field is spec-only. **Defer until eager exists or remove from doc.**
- `components` nested vs flat: the nested version allows including details (e.g. "lance probe: connection refused"). The code emits the details in a separate `errors` field. **Functionally equivalent, but different shape.** A schema decision.
- `restorations_*`: the code has `_repair_drift` but doesn't emit aggregates. Reasonable to add.
- `duration_ms`: trivial to add (measure t0 on entering `run_tick`).

**Action option A** (smaller change): update doc 07 §9.4 to describe the real payload. Add `duration_ms` (trivial). Leave the rest as "future".

**Action option B** (larger change): add `tick_id` + `restorations_attempted/succeeded` + `duration_ms` to the code and promote `components` to nested.

**Recommendation**: option A. The code's flat structure is simpler and the detail data already goes through `errors`. The doc adjusts to reality; when there's real need for tick_id/eager, re-evaluate.

**Resolution (2026-05-28) — Pragmatic hybrid**: verified analysis showed that **there are no consumers of the event in code today** (zero hits outside the emitter module + tests), so "who is right" is not binary — it's a forward-looking design decision. Result:

- **Added to code**: `tick_id` (uuid hex, 32 chars) + `duration_ms` (via `time.perf_counter()`). They're operational standard: tick_id for log correlation between ticks, duration_ms to differentiate fast vs slow ticks.
- **NOT added**: `triggered_by` (only `scheduled` exists today; would be an enum with a single value), `components` nested (functionally equivalent to flat + errors separate; nested is churn without benefit), `restorations_attempted`/`succeeded` (`drift_count` + `errors` already cover the signal today; add when there's operational alarm requiring it).

Changes:
- [durin/memory/health_check.py](../../durin/memory/health_check.py): `import uuid` + `time` added. `run_tick()` generates `tick_id = uuid.uuid4().hex` and `t0 = time.perf_counter()` on entry; the payload includes both. ~5 LOC delta.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): `MemoryHealthCheckEvent` TypedDict gains `tick_id` and `duration_ms`. Additive — pre-A6 fields still required.
- [docs/memory/07_telemetry_and_observability.md §9.4](07_telemetry_and_observability.md): table rewritten with the 6 current fields + "Shape decisions and what's deliberately NOT emitted" block documenting why `triggered_by`/`nested components`/`restorations_*` were left out. **That block is what prevents this decision from being taken in reverse** (a future reader might see doc 07 §9.4 v1 and "implement what the doc says" without knowing the context).
- [tests/memory/test_health_check_a6_fields.py](../../tests/memory/test_health_check_a6_fields.py): 5 new tests exercising behavior:
  - `tick_id` is exactly 32-char hex (not 36-char dashed — catches `.hex` vs `str()` regression).
  - `duration_ms` is > 0 (catches seconds-instead-of-ms regression — `perf_counter()` delta in seconds is <1, multiplied by 1000 is >0).
  - Consecutive ticks produce distinct tick_ids (catches per-init vs per-tick generation regression).
  - TypedDict has the A6 fields **and** the pre-A6 fields (additive, not replace).
  - Pre-A6 fields still in the payload.

**Lessons applied**:
- [[feedback-verify-quantifiers]]: the test explicitly verifies `len(tick_id) == 32` and that all characters are hex. Doesn't assume "uuid is uuid".
- [[feedback-sync-tests-exercise-behavior]]: behavior tests, not just schema declarations.
- [[feedback-no-wait-and-measure]] inverted: do NOT add fields without demonstrated need (`triggered_by`, `restorations_*`). Document the decision so it doesn't get reversed.

**Pre-commit verification**: tests/memory/ 912 passed (907 baseline + 5 new A6), 1 skipped.

**State**: resolved (commit pending).

---

### A7 — `memory.health.critical` missing `manual_recovery_hint`

**Doc says** (`docs/memory/07_telemetry_and_observability.md:338`):

```
| manual_recovery_hint | string | Suggested CLI: e.g., `durin reindex --target lancedb` |
```

**Code says** (`durin/memory/health_check.py:227-238`):

```python
emit_tool_event(
    "memory.health.critical",
    {
        "component": component,
        "consecutive_failures": count,
        "last_error": error[:200],
    },
)
```

**Who is right**: doc is right on value (if you're going to alert, giving the recovery command helps). Implementation is trivial — mapping component → suggested command.

**Action**: add a recovery hints dict in `health_check.py`:

```python
_RECOVERY_HINTS = {
    "fts5": "durin memory reindex --target fts",
    "lance": "durin memory reindex --target lance",
}
```

And add to the payload.

**Resolution (2026-05-28) — Option A with anti-drift test**: the field is added + a test that protects against drift between the hints and the real CLI. Applying `feedback_verify_quantifiers`, the command suggested by the original doc (`durin reindex --target lancedb`) was **incorrect** — the real command is `durin memory reindex` (it was missing the `memory`). And `--target` accepts `lancedb` (not `lance` which is the probe name). Both spec errors corrected in the implementation.

Changes:

- [durin/memory/health_check.py](../../durin/memory/health_check.py):
  * New `_RECOVERY_HINTS` dict — mapping probe-name → verbatim CLI command.
  * New `_RECOVERY_HINT_FALLBACK = "durin memory reindex --target all"` for new components without a specific hint.
  * `_emit_critical()` payload includes `manual_recovery_hint` (lookup with fallback).
- [durin/cli/memory_cmd.py](../../durin/cli/memory_cmd.py): the `("all", "fts", "lancedb")` constant extracted to an exportable `VALID_REINDEX_TARGETS`. Allows the anti-drift test to compare against a single source of truth instead of hardcoding strings.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): `MemoryHealthCriticalEvent` gains `manual_recovery_hint: str`. Additive.
- [tests/memory/test_health_critical_a7_recovery_hint.py](../../tests/memory/test_health_critical_a7_recovery_hint.py) (new, 6 tests):
  * All known probes (`fts`, `lance`) have a hint.
  * All hints start with `durin memory reindex` (not `durin reindex` — protects against re-introducing the spec typo).
  * **Anti-drift core**: each `--target X` in each hint passes CLI validation (imports `VALID_REINDEX_TARGETS`). If someone renames a target without updating `_RECOVERY_HINTS`, the test fails.
  * Emit path for known component uses the specific hint.
  * Emit path for unknown component uses the fallback.
  * TypedDict declares the field + preserves pre-A7 fields.

- [docs/memory/07_telemetry_and_observability.md §9.5](07_telemetry_and_observability.md): rewritten with the 4 fields. Section explains the probe-name → CLI target translation (legacy drift `lance` vs `lancedb`) and references the anti-drift test. The wrong command from the v1 spec corrected.

**Lessons applied**:
- [[feedback-verify-quantifiers]]: verify that the suggested command **actually exists**. Doc 07 v1 said `durin reindex` — non-existent command (missing `memory`). The audit caught it before implementing.
- [[feedback-sync-tests-exercise-behavior]]: the test doesn't compare strings between doc and code — it verifies that the suggested target **passes CLI validation**, exercising the real contract.
- [[feedback-optimization-vs-principle]]: the fix is local to the consumer (health_check + memory_cmd extract VALID_REINDEX_TARGETS). The human consumer reading logs is legitimate even though there's no software consumer today.

**Pre-commit verification**: tests/memory/ 918 passed (912 baseline + 6 new A7), 1 skipped.

**State**: resolved (commit pending).

---

### A8 — `PushSink` is dead code without wiring

**Doc says** (`docs/memory/07_telemetry_and_observability.md` §12.2 + `09_implementation_roadmap.md` P7.3): HTTPS push opt-in via `telemetry.push_url` + `telemetry.push_token`.

**Code says**:
- `durin/telemetry/push.py:32` `PushSink` exists with passing tests.
- `grep -rn "PushSink" durin/` (outside push.py itself): zero hits.
- `grep -rn "push_url|push_token" durin/config/`: zero hits.
- No sink invokes it; the config doesn't have the fields; the agent never creates it.

**Who is right**: both. The doc describes the feature correctly. The code has half (the class). The wiring is missing: fields in `durin/config/schema.py::TelemetryConfig`, construction in the sink registry, `push.log(...)` call from the emit pipeline.

**Action**:
1. Add to `durin/config/schema.py` (probably under `TelemetryConfig` or create `TelemetryPushConfig`):
   - `push_url: str | None`
   - `push_token: str | None` (better read from the secret store)
   - `push_batch_size: int = 10`
2. In the sink registry (`durin/telemetry/sinks.py` or equivalent): if `push_url` configured, instantiate `PushSink` and add to the fan-out.
3. E2E test: configure fake URL (httpbin), verify that an emit triggers an HTTP request.

**Resolution (2026-05-28) — Option A end-to-end wiring**: the first audit analysis proposed deleting PushSink ("no consumer"). The user corrected: *"measuring behavior is the purpose of telemetry — if there's no consumption, it's because we haven't yet published it to a dashboard/API, not because it's not needed. Measuring is everything."* New lesson saved in persistent memory: [[feedback-telemetry-is-first-class]] — pattern opposite to A4 (P2.5 revert).

Changes:

- [durin/config/schema.py](../../durin/config/schema.py): new `TelemetryPushConfig` + `TelemetryConfig`. `Config` gains `telemetry: TelemetryConfig`. The schema declares `token_secret_name` (reference), NOT the token; an invariant test (`test_config_schema_has_no_plaintext_token_field`) guards against regression.
- [durin/telemetry/logger.py](../../durin/telemetry/logger.py): `TelemetryLogger` gains `_extra_sinks` + `add_sink()`. `log()` writes first to the JSONL (canonical source of truth) and then iterates the additional sinks — each isolated in try/except so a failing sink doesn't affect the rest or the JSONL.
- [durin/telemetry/wiring.py](../../durin/telemetry/wiring.py) (new): `wire_push_sink()` that (a) verifies config validity, (b) resolves the token via `get_secret_store().get(name)`, (c) constructs `PushSink` + attaches to the logger, (d) logs clear warnings if config is incomplete or the secret is missing. All failure modes end in "push disabled, JSONL keeps working".
- [durin/telemetry/__init__.py](../../durin/telemetry/__init__.py): `PushSink` exported in `__all__` (now public package API).
- [durin/agent/loop.py](../../durin/agent/loop.py): integrated — when creating the session_logger, `wire_push_sink` is attempted; in the cleanup `finally`, `push_sink.flush()` is called so events in the partial buffer aren't lost.
- [tests/telemetry/test_push_wiring.py](../../tests/telemetry/test_push_wiring.py) (new, 9 tests):
  * Disabled-path: default → no sink. None config → no sink (no raise).
  * Misconfigured: empty url or secret_name → graceful disable.
  * Secret missing: store doesn't have the name → graceful disable + warning.
  * Happy path: the sink attaches, the RESOLVED token comes from the secret store (assert privacy invariant).
  * Fan-out: 3 events emitted → 3 lines in JSONL + 3 pending in the push buffer.
  * Isolation: broken sink (raises) → JSONL keeps writing correctly.
  * Schema invariant: `TelemetryPushConfig` does NOT have a plaintext `token` field — only `token_secret_name`. Catches a regression that would put the token in config.json.

- [docs/memory/07_telemetry_and_observability.md §12.2](07_telemetry_and_observability.md): retention corrected (90 days, not 1 year). New §12.3 — full description of the push opt-in: TOML config, command for the secret, privacy implications, behavior (failure isolation, drain on shutdown, retry path).

**Lessons applied**:
- [[feedback-telemetry-is-first-class]] (new): measuring behavior is the purpose, doesn't require a downstream consumer to justify.
- [[feedback-verify-quantifiers]]: tests verify the Config schema shape (don't assume; read `model_fields`).
- [[feedback-sync-tests-exercise-behavior]]: the test doesn't compare strings between doc and code; it exercises the happy/unhappy paths of the real wiring.
- Privacy by design: token via secret store (lesson from how `ZHIPU_API_KEY` is handled in A5), default OFF, explicit warning in doc 07 §12.3.

**Pre-commit verification**: tests/memory/ + tests/telemetry/ 962 passed (953 baseline + 9 new A8), 1 skipped.

**State**: resolved (commit pending).

---

### A9 — Temporal decay not applied to ranking

**Doc says** (`docs/memory/00_overview.md:232`, row 3b):
> **In MVP, enabled by default**, but only for observation-type docs. episodic (90d half-life) and session_summary (120d) decay.

**Doc says** also (`docs/memory/03_search_pipeline.md` §10) — "STEP 6 — Temporal decay" between cross-encoder and sectioning, "default enabled".

**Code says** (`durin/memory/decay.py:14-18`, literal header):

```python
"""...
Phase 0 scope: the half-life table + the `half_life_for` resolver. The
ranking-time consumer (apply exponential decay to score) lands in a
later phase.
"""
```

`grep -n "decay|half_life" search_pipeline.py rrf_fusion.py entity_ranker.py` → **zero hits**. Nothing consumes the resolver.

**Who is right**: the code self-documents correctly (header explains that it's pending). **Doc 00 §10 row 3b lies.** Doc 03 §10 promises "enabled by default" — false.

**Action option A** (fulfill the doc): implement the ranking-time consumer. ~50 LOC: in `run_search_pipeline`, after RRF and before entity rerank, multiply `score *= exp(-Δdays/half_life)` for hits with `half_life ≠ None`.

**Action option B** (align doc): mark decay as deferred in doc 00 and doc 03, move to `08_scope_and_discarded.md` as "deferred to post-MVP".

**Recommendation**: option A is ~1h of work and closes an explicit doc promise. Let's do A.

**Resolution (2026-05-28) — Option A, class defaults only**: during the analysis the user pushed back with the key question: *"don't assume the defaults from the doc, enumerate all the classes that get stored and reason about each one"*. The enumeration (verified against `MEMORY_CLASSES` + the real code) arrived at the same table as the original doc — but now with the explicit per-class reasoning recorded:

| Class | Decays | Half-life | Verified reasoning |
|---|---|---|---|
| `entity_page` (alias `entity`) | No | null | `valid_from = ""` always for entity pages; mtime is "last Dream pass", not "fact age" |
| `episodic` | Yes | 90d | Observations with intrinsic timestamp — age IS content information |
| `stable` | No | null | User/agent explicitly marked it as durable; decaying contradicts the decision |
| `corpus` | No | null | `valid_from` is the INGEST date, not the content date — decaying would punish "old books in your pipeline" |
| `session_summary` | Yes | 120d | Same concept as episodic but covers broader topics — but inert until A10 (not emitted today) |
| `pending` | N/A | — | Walker excludes it (A2) |

**Per-entry override is NOT applied in search pipeline**: the user confirmed that per-class is enough. Additional verification showed that **it's spec without real use**: Dream never sets `evergreen` nor `decay_half_life`; the current workspace has no entries with those overrides; the Dream templates don't instruct the LLM to emit them. The field stays in the `MemoryEntry` schema prepared for the future; the `half_life_for` resolver continues to be exported for callers that need it (hot_layer, dream).

Changes:

- [durin/memory/decay.py](../../durin/memory/decay.py): new pure function `apply_class_decay(score, class_name, valid_from_iso, now=None) -> (decayed, factor)`. `CLASS_HALF_LIFE_DEFAULTS` gains `entity_page` as an alias for `entity` (FTS5 / LanceDB use different names; both resolve to null). Module header rewritten with the reasoned table inline.
- [durin/config/schema.py](../../durin/config/schema.py): new `MemoryTemporalDecayConfig(enabled: bool = True)`. `MemorySearchConfig` now has `temporal_decay`.
- [durin/memory/search_pipeline.py](../../durin/memory/search_pipeline.py): new `_temporal_decay_step()` inserted after the cross-encoder and before sectioning. Reorders `fused` by decayed scores. `run_search_pipeline` gains `temporal_decay_enabled: bool = True`. `now` injectable for deterministic tests.
- [durin/agent/tools/memory_search.py](../../durin/agent/tools/memory_search.py): reads `app_config.memory.search.temporal_decay.enabled` and threads it to the pipeline.
- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): new `MemoryRecallDecayEvent` TypedDict + registration in `EVENTS`.
- [tests/memory/test_decay_search_integration.py](../../tests/memory/test_decay_search_integration.py) (new, 19 tests):
  * Unit: `apply_class_decay` for each class (decays / doesn't decay) + edge cases (empty/malformed/future timestamp, unknown class).
  * Quantifier: `exp(-1) ≈ 0.368` for 1 half-life, `exp(-5) ≈ 0.0067` for 5 half-lives.
  * `entity` and `entity_page` both resolve to no-decay (catches the FTS5 vs LanceDB naming).
  * Pipeline: old hits drop to the bottom, recent ones rise; entity_page with old valid_from does NOT move.
  * Telemetry: `memory.recall.decay` event with correct counts.
  * Schema: TypedDict registered, config default enabled=True.

- [docs/memory/03_search_pipeline.md §10.7](03_search_pipeline.md) (new): describes what A9 shipped + the reasoned table + scope (class only).
- [docs/memory/00_overview.md §10 row 3b](00_overview.md): from "promise" to "shipped".

**Lessons applied**:
- [[feedback-verify-quantifiers]] applied twice during development:
  1. Initial test used `_FIXED_NOW = datetime(... 12:00)` but `valid_from="2026-05-28"` parses to 00:00 — 0.5-day delta, factor ≈ 0.9945 (not 1.0). Fix: `_FIXED_NOW = datetime(... 00:00)` so deltas are exact.
  2. Pipeline test passed `now=None` to `_temporal_decay_step` → real wall-clock different from `_FIXED_NOW` the calculation expected. Refactor to inject `now` from tests.
- [[feedback-question-user-input]]: the first plan copied the doc's defaults without reasoning. The user pushed "enumerate and reason about each class" — and the enumeration produced the same result, but with verbatim reasoning saved. The difference: future readers see *why* corpus doesn't decay, not just *that* it doesn't decay.
- [[feedback-sync-tests-exercise-behavior]]: tests don't compare doc strings; they exercise the function with numerical values verified mathematically.

**Pre-commit verification**: tests/memory/ 937 passed (918 baseline + 19 new A9), 1 skipped.

**State**: resolved (commit pending).

---

### A10 — Doc 02 promises indexing of session summaries; nothing indexes them

**Doc says** (`docs/memory/02_indexing.md:104`):

> *"`sessions/<id>/<id>.meta.json::derived._last_summary` (one row per session as `type=session_summary`)"*

And §6.5 (yield rule): *"Also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present"*.

**Code says**:
- `durin/memory/paths.py:78-111` `walk_memory` iterates **only** `*.md` under `memory/`. Never touches `sessions/`.
- `grep -rn "session_summary\|_last_summary" durin/memory/indexer.py durin/memory/vector_index.py` → zero relevant hits (only appears in metadata tables or as return category, not as input).
- `CLASS_HALF_LIFE_DEFAULTS` lists `session_summary: 120` but nothing emits rows with that type to Lance/FTS.

**Who is right**: doc 02 promises a capability that would be useful but doesn't exist. If the dream consolidator wrote summaries as `memory/sessions/<id>.md` (markdown format), the walker would pick them up; today they live in `sessions/<id>/<id>.meta.json` (JSON-derived) and nothing propagates them to the index.

**Action option A** (implement): after closing a session, write the last_summary as `memory/episodic/session-<id>.md` with class `session_summary`. Then the walker sees them.

**Action option B** (remove from doc): delete the §6.5 yield and the `session_summary` row from §3.3. Mark as deferred.

**Recommendation**: option A — session summaries are retrieval-valuable (condensed summary of an entire conversation). ~30 LOC in the session-close handler. But it requires deciding where they live (`memory/<class>/` requires a new class or reusing `episodic`).

**Resolution (2026-05-28) — Option A with single source of truth**: the user pushed back with the key question: *"will the last summary now live in the session's metadata file in addition to its own entity?"* — exactly the A4 (P2.5) pattern we'd already identified as an anti-pattern. The duplication between `<key>.meta.json::_last_summary` and `memory/session_summary/<key>.md` was replication, not fan-out. Solution: the `.md` is the **only** source of truth; the JSON metadata stops carrying `_last_summary` going forward.

Changes:

- [durin/memory/session_summary_store.py](../../durin/memory/session_summary_store.py) (new, ~155 LOC):
  * `SESSION_SUMMARY_CLASS = "session_summary"` constant.
  * `sanitize_session_key(key)` — same pattern as `TelemetryLogger`'s sanitiser: collapses non-word chars + dot runs (path-traversal safe), cap at 80 chars.
  * `session_summary_path(workspace, key)` — resolves to `memory/session_summary/<sanitized>.md`.
  * `write_session_summary(workspace, key, text, last_active=None)` — writes the entry via Pydantic-valid `MemoryEntry`. Empty/sentinel input → no write.
  * `get_session_summary(workspace, key) -> (text, last_active)` — read path. Never raises.
  * `delete_session_summary(workspace, key) -> bool` — explicit deletion.
- [durin/memory/paths.py](../../durin/memory/paths.py): `MEMORY_CLASSES` now includes `"session_summary"` (5 values). Walker picks it up automatically.
- [durin/agent/memory.py::Consolidator._persist_last_summary](../../durin/agent/memory.py): refactored — writes to the `.md` via `write_session_summary` and **pops the legacy `_last_summary` from `session.metadata`** + saves (one-shot migration per compaction). Isolated in try/except so the consolidator flow never breaks on a write failure.
- [durin/agent/memory.py::estimate_session_prompt_tokens](../../durin/agent/memory.py): reads from the `.md` via `get_session_summary`; fallback to legacy `metadata["_last_summary"]` for pre-A10 sessions that haven't yet compacted.
- [durin/agent/loop.py::_format_pending_summary](../../durin/agent/loop.py): changed from `@staticmethod` to instance method to access `self.workspace`. Reads from the `.md` first; falls back to legacy metadata.
- [tests/memory/test_paths.py](../../tests/memory/test_paths.py): canonical set test updated to 5 values.
- 3 legacy tests updated to use `get_session_summary` instead of reading `session.metadata["_last_summary"]`: [test_consolidator.py:167](../../tests/agent/test_consolidator.py), [test_loop_consolidation_tokens.py:191](../../tests/agent/test_loop_consolidation_tokens.py), [test_d1_commands.py:187](../../tests/command/test_d1_commands.py).
- [tests/memory/test_session_summary_indexing.py](../../tests/memory/test_session_summary_indexing.py) (new, 14 tests):
  * `session_summary` in `MEMORY_CLASSES` but NOT in `_AGENT_FACING_CLASSES` (agent never writes summaries directly).
  * `sanitize_session_key`: simple/colon/path-traversal/empty handled.
  * `write_session_summary` round-trip → identical text.
  * Empty/sentinel input → no write.
  * Update overwrites same path (id = sanitized key).
  * `delete_session_summary` removes md; second delete is no-op.
  * Persisted entry is Pydantic-valid (round-trips via `load_entry`).
  * Indexer's `_payload_for` assigns `class_name="session_summary"`.
  * A9 decay for `session_summary` resolves to 120 days.

- [docs/memory/02_indexing.md §3.3](02_indexing.md): new §3.3.1 "Session summaries (audit A10)" explains the flow + single source of truth decision + agent_facing_classes exclusion.

**Pre-A10 sessions**: have `_last_summary` in the `metadata` JSON. The migration is **lazy**: on the next compaction of each session, `_persist_last_summary` writes the new `.md` AND pops the legacy field from metadata. If a session is NEVER recompacted (e.g. user abandons it), the legacy summary stays in its JSON — `_format_pending_summary` reads it as fallback. No data loss; only "no indexing" for those orphan sessions (acceptable; the user will never use them).

**Lessons applied**:
- [[feedback-optimization-vs-principle]] (A4): the user identified the replication before I implemented it. Exactly fits the A4 pattern.
- [[feedback-question-user-input]]: the user pushed "does this live in two places?" and the correct answer was to refactor the plan, not defend the duplication.
- [[feedback-sync-tests-exercise-behavior]]: tests exercise real round-trips, not mocked schemas.

**Pre-commit verification**: tests/memory/ + tests/agent/ + tests/command/ + tests/session/ + tests/telemetry/ 2302 passed (all tests pass after updating the 3 legacy + adding 14 new), 1 skipped.

**State**: resolved (commit pending).

---

### A11 — `MemoryFileWatcher` and `HealthChecker` shipped but not wired to lifecycle

**Doc says** (`docs/memory/10_remaining_work.md` P2.3 + P2.4 DoD):
- P2.3: *"Edit `memory/entities/person/marcelo.md` with vim and, within 5 seconds, the next `memory_search` for 'marcelo' surfaces the words from the edit."*
- P2.4: *"Every 15 minutes (configurable), a background job... probes FTS + Lance."*

**Code says**:
- `durin/memory/file_watcher.py::MemoryFileWatcher` exists + tests pass.
- `durin/memory/health_check.py::HealthChecker` exists + tests pass.
- `grep -rn "MemoryFileWatcher\|HealthChecker" durin/agent durin/cli durin/channels` → **zero hits**.

No call site starts them. `AgentLoop.start`, `durin agent` CLI, the channel adapters — none mention them.

**Who is right**: doc is right about the **intent**; the proposed DoDs require wiring that doesn't exist.

**Action**:
1. `durin/agent/loop.py::AgentLoop.start` — if `cfg.memory.enabled` and `cfg.memory.file_watcher.enabled` (new flag), start `MemoryFileWatcher` as a background thread; stop it in `stop`.
2. Decision: does the health_check cron live in-process (a daemon thread) or as an external cron? Doc 10 P2.4 suggests in-process. Implement `HealthCheckScheduler` that fires `run_tick()` every `cfg.memory.health_check.interval_seconds` (new).
3. Add config keys.
4. Verify live: edit a .md with vim → memory_search sees the change.

**Risk**: file watchers on macOS/Linux/Docker have edge cases. `watchdog` is already a dep.

**Resolution (2026-05-28) — Default ON both services + isolation**: applying `feedback_telemetry_is_first_class` (A8): the health check IS observability infrastructure — the reason "there are no alerts" today is that we didn't wire it. **Default ON**. The file watcher is direct UX (vim edit → next search sees it) — **also default ON**. Both opt-out via config.

Changes:

- [durin/config/schema.py](../../durin/config/schema.py): new `MemoryFileWatcherConfig(enabled=True)` and `MemoryHealthCheckConfig(enabled=True, interval_seconds=900)`. `MemoryConfig` now has `file_watcher` and `health_check`.
- [durin/memory/health_check.py](../../durin/memory/health_check.py): new `HealthCheckScheduler` — daemon thread that calls `run_tick()` every N seconds. `wait(timeout)` instead of `sleep(N)` so `stop()` is responsive (doesn't wait the full interval). Failure isolation: `run_tick()` exception logged but the thread continues (next tick fires).
- [durin/agent/loop.py::AgentLoop](../../durin/agent/loop.py):
  * New attributes `self._memory_file_watcher` and `self._memory_health_scheduler` initialized at the end of `__init__`.
  * New method `_start_memory_background_services()` — constructs + starts each service if its config flag is enabled. Each isolated in try/except: if one fails to start, the other continues and `AgentLoop` starts anyway.
  * New method `_stop_memory_background_services()` — None-safe; calls `stop()` on each and isolates failures.
  * `AgentLoop.stop()` now invokes `_stop_memory_background_services()` before logging.
- [tests/memory/test_a11_lifecycle_wiring.py](../../tests/memory/test_a11_lifecycle_wiring.py) (new, 12 tests):
  * Config defaults: both enabled, interval=900.
  * `HealthCheckScheduler` ticks on start (first tick immediate).
  * `HealthCheckScheduler.stop()` responsive even with interval=3600.
  * `HealthCheckScheduler` isolates `run_tick` failures: the thread continues after exception.
  * `app_config=None` → no services (keeps existing tests simple).
  * Default config → both start.
  * Watcher disabled → only health runs.
  * Health disabled → only watcher runs.
  * `stop()` drains both cleanly.
  * Watcher startup failure isolated — health keeps running.

- [docs/memory/02_indexing.md §6.3](02_indexing.md): new "Lifecycle (audit A11)" block explains that the watcher starts by default, failure isolation, and how to disable it.
- [docs/memory/07_telemetry_and_observability.md §9.4](07_telemetry_and_observability.md): new "Scheduling (audit A11)" block explains `interval_seconds=900` default + "first tick immediate" + responsive shutdown.

**Test impact**: 2302 previous tests keep passing (the 2300+ that build `AgentLoop` do so with `app_config=None`, which skips wiring by design). No breaking change for the existing suite.

**Key architectural decision**: `_start_memory_background_services` is defensive end-to-end. Each service can fail independently:
- Watchdog not installed → watcher fails import → log warning → `_memory_file_watcher` stays None → rest of the loop continues.
- Health check thread can't be created → log warning → `_memory_health_scheduler` stays None → file watcher continues.

Applying `feedback_telemetry_is_first_class`: telemetry (health_check emit) and observability infrastructure (file watcher reindex) do NOT require a downstream consumer to justify — they are the source of the data we will later use.

**Pre-commit verification**: tests/memory/ + tests/agent/ + tests/command/ + tests/session/ + tests/telemetry/ 2314 passed (2302 baseline + 12 new A11), 1 skipped.

**State**: resolved (commit pending).

---

## MEDIUM — drift without breaking direct UX

### B1 — `.description` property of the tools is not synchronized ✅ RESOLVED with the canonical

**Doc says** (`docs/memory/04_agent_tools.md:413-419` §8):

> *"The description in the tool registration MUST match the doc 06 §3.1-§3.4 text verbatim. Sync via `tests/memory/test_tool_description_sync.py`."*

**Code says**:
- `_PARAMETERS["description"]` in each tool **is** synchronized (test passes).
- But each tool also has a different `description` property. Examples:
  - `memory_search.py:165-178`: *"Search the agent's memory. Pass a short topical phrase..."* (compressed text, different from canonical).
  - `memory_store.py:121-127`: *"Persist a memory entry. Idempotent on (class, content)..."* (doesn't mention dedup vs Dream).
  - `memory_ingest.py:94-103`: *"Persist a markdown or plain-text file..."* (doesn't mention URLs, inline, etc.).
  - `memory_drill.py`: similar drift.

**Who is right**: doc is right. **Two different descriptions for the same tool is exactly what the doc says to avoid.**

We need to clarify which one is presented to the LLM at runtime. Check the `Tool` base class to see if it uses `_PARAMETERS["description"]` or `self.description`.

**Action**:
1. Investigate which of the two reaches the LLM. Probably `_PARAMETERS["description"]` (what the sync test validates), but if the property is used in some registry/CLI, it must be aligned.
2. If the property is "human-readable short" and `_PARAMETERS` is "LLM canonical", document the distinction explicitly and add an invariant test (e.g. property contains a summary of the canonical).
3. If the property isn't used anywhere relevant, **delete it** — dead code that confuses.

**Resolution (2026-05-28) — Bug discovery + fix**: the investigation revealed that **the P6.3 sync test was validating the wrong field**. `Tool.to_schema()` ([base.py:258](../../durin/agent/tools/base.py#L258)) emits `self.description` (the short property) as `function.description` in the OpenAI function-calling spec — that's what the LLM actually reads to decide whether to invoke the tool. The `_PARAMETERS["description"]` that P6.3 synchronized with doc 06 ends up as `function.parameters.description` (description of the parameters object schema), which most LLMs ignore.

**Bug**: for weeks the sync test passed green validating a field the LLM ignored while the field the LLM DID read contained short text not synchronized with doc 06. Same pattern as A4 (my own previous commits validated partially).

Changes:

- [durin/agent/tools/memory_search.py:181](../../durin/agent/tools/memory_search.py#L181), [memory_store.py:140](../../durin/agent/tools/memory_store.py#L140), [memory_ingest.py:99](../../durin/agent/tools/memory_ingest.py#L99), [memory_drill.py:47](../../durin/agent/tools/memory_drill.py#L47): each `.description` property now delegates to `_PARAMETERS["description"]` (single source of truth — both fields resolve to the same string). The non-canonical short text is removed. Inline comment explains the flow and references B1.
- [tests/memory/test_tool_description_sync.py](../../tests/memory/test_tool_description_sync.py): the test now instantiates each tool and reads the `.description` property (instead of `_PARAMETERS["description"]`). Additionally, new test `test_description_property_is_what_to_schema_emits` verifies the invariant `to_schema()["function"]["description"] == tool.description` — anti-drift against the case "someone changes `to_schema()` to use another field and the sync ends up looking at the wrong field again".
- [docs/memory/06_prompts_and_instructions.md §3.5](06_prompts_and_instructions.md): rewritten — explains the `.description` → `function.description` contract, why `_PARAMETERS["description"]` (which ends up as `function.parameters.description`) is kept identical by defense-in-depth, and documents the B1 bug this section reflects.

**Lessons applied**:
- [[feedback-sync-tests-exercise-behavior]]: the sync test now exercises the **real contract** (`to_schema()` output) not just string equality. The new invariant test `test_description_property_is_what_to_schema_emits` is specific defense against "someone refactors `to_schema()` and the sync ends up looking at the wrong place".
- [[feedback-verify-quantifiers]]: during the investigation I verified which real consumer reads `self.description` (grep showed `to_schema()` only). Without that check I would have assumed the sync test already covered the right thing.
- [[feedback-optimization-vs-principle]] applied to the A4 pattern: P6.3 was my own commit that synchronized partially — the fix is local to the correct consumer (the property the LLM reads), not changing the global contract.

**Pre-commit verification**: 5/5 sync tests (4 content + 1 invariant); memory suite 964 passing.

**State**: resolved (commit pending).

---

### B2 — Doc 99_phase_progress_review obsolete ✅ RESOLVED

**Doc says** (`docs/memory/99_phase_progress_review.md:5`): "4885 tests passing".

**Doc says** (§2 D4): "Phase 1.9 deferred (v2 pipeline integration in DreamConsolidator)... Next step: Phase 1.9".

**Code says**:
- `git log --oneline`: commit `6aafc3f` shipped Phase 1.9 (DreamConsolidator uses parse_dream_output + apply_dream_output).
- Current test count (latest commit `2e7097a` body): 4968 passing.

**Who is right**: code (commits tell the truth). Doc out of date.

**Action**: update `99_phase_progress_review.md` — mark D4 resolved, update test count, move §4 recommendations to "DONE" state.

**State**: pending

---

### B3 — Doc 10 marks as pending what is done ✅ RESOLVED

**Doc says** (`docs/memory/10_remaining_work.md` lines 24, P2.x, P3.x, P4.x, P5.x, P6.x, P7.x): many items without ✅ DONE.

**Code says** (git log):
- P2.2 ✅ commit `c3eff1e`
- P2.3 ✅ commit `d9a4d8e` (module exists; see A11 about wiring)
- P2.4 ✅ commit `022d4b1` (module exists; see A11)
- P2.5 ✅ commit `a266344`
- P3.3 ✅ commit `bc55686`
- P4.1-P4.3 ✅ commit `b3c50c6`
- P4.4 ✅ this turn
- P5.2-P5.6 ✅ commits `2e7097a`, `572d5cf`
- P6.1-P6.3 ✅ commit `572d5cf`
- P7.2-P7.3 ✅ commit `2e7097a`

Line 24 says "Phase 4 + Phase 8 remain" — Phase 4 closed.

**Who is right**: code. Doc out of date.

**Action**: go through doc 10 and mark each item with ✅ DONE + commit hash. Rewrite line 24.

**State**: pending

---

### B4 — P5.5 implemented differently from spec ✅ RESOLVED

**Doc says** (`docs/memory/10_remaining_work.md` P5.5):
> *"Script `scripts/audit_tool_descriptions.py` extracts descriptions... fails with specific diff if they differ. Wired in CI."*

**Code says**:
- `ls scripts/audit_tool_descriptions.py` → doesn't exist.
- `tests/memory/test_tool_description_sync.py` exists, 4 tests pass, validates `_PARAMETERS["description"]` against doc 06 §3.1-§3.4.
- There's no new CI step in `.github/workflows/`.

**Who is right**: both valid in intent. The pytest test fulfills the same function as the script + CI (pytest already runs in CI), and is more standard (doesn't introduce a custom command).

**Action**: update doc 10 P5.5 to reflect that the implementation is pytest, not a standalone script. State: ✅ DONE with documented deviation.

**State**: pending

---

### B5 — Retention: 1 year in doc vs 90 days in code ✅ RESOLVED

**Doc says** (`docs/memory/07_telemetry_and_observability.md` §12.2):
> *"old events compressed... kept 1 year, then deleted"*

**Code says** (`durin/telemetry/retention.py:34-35`):

```python
COMPRESSION_AGE_DAYS: int = 30
DELETION_AGE_DAYS: int = 90
```

→ 30d to compress, 90d to delete. Total 90 days, not 1 year.

**Who is right**: depends on real use.
- Doc (1 year): conservative, useful for longitudinal analysis.
- Code (90d): minimizes disk usage. Reasonable for single-user durin.

**Action**: make it configurable (`telemetry.retention.{compress_age_days, delete_age_days}` in config schema). Current default (30/90) reasonable; user can raise it to 365 if they want annual analysis. Update doc 07 §12.2 to describe the real defaults + how to extend.

**State**: pending

---

### B6 — Doc 03 §17 status table contradicts §11 about MMR ✅ RESOLVED

**Doc says** §11: "MMR — Removed from MVP".
**Doc says** §17 status table: "MMR | Not implemented | New step, default enabled".

**Code says**: `grep -rn "mmr\|MMR" durin/memory/` → zero hits in production code.

**Who is right**: §11 (removed). §17 was left stale when §11 was updated.

**Action**: correct §17 — MMR row should say "Removed from MVP".

**State**: pending

---

### B7 — Doc 05 §15 + doc 06 §10 status: "v1 page rewrites" ✅ RESOLVED

**Doc says** (`docs/memory/05_dream_cold_path.md:201` and §15 status table): *"current code uses full-page rewrites"*.
**Doc says** (`docs/memory/06_prompts_and_instructions.md` §10): *"templates/dream/consolidator.md: v1 (page + commit)"*.

**Code says**:
- `durin/memory/dream.py` calls `parse_dream_output` + `apply_dream_output` (Phase 1.9 shipped in commit `6aafc3f`).
- `durin/templates/dream/` contains `consolidator.md`, `rules.md`, `commit_format.md`, `json_patch_reference.md`, `examples/01..06_*.md`.
- `dream_prompt_builder.build_dream_prompt` assembles the package.

**Who is right**: code. Doc out of date because it wasn't updated after Phase 1.9.

**Action**: delete the §15 doc 05 line 201 callout; update status table; update doc 06 §10 to "v2 (JSON Patch + body delta)".

**State**: pending

---

### B8 — Doc 03 §15 promises config keys that don't exist ✅ RESOLVED

**Doc says** (`docs/memory/03_search_pipeline.md` §15):

```
memory.search.vector_top_k
memory.search.lexical_top_k
memory.search.rrf_constant
memory.search.rrf_weights
memory.search.sectioning.max_per_source
memory.search.final_top_k
```

**Code says** (`durin/config/schema.py:276-281`):

```python
class MemorySearchConfig(Base):
    cross_encoder: CrossEncoderConfig = Field(
        default_factory=CrossEncoderConfig,
    )
```

Only `cross_encoder`. The rest is hardcoded:
- `vector_top_k=50` (search_pipeline.py:347)
- `limit=10` (memory_search.py:348)
- RRF k=60 + weights (rrf_fusion.py:38-42)
- `DEFAULT_MAX_PER_SOURCE=3` (sectioned_output.py:38)

**Who is right**: depends on the desired level of configurability. **Today, in single-user durin, reasonable hardcoded defaults are OK** — exposing 6 additional knobs adds complexity without clear need.

**Action option A** (minimal): update doc 03 §15 to list **only** the keys that exist (`memory.search.cross_encoder.*`) and add note "the other defaults are hardcoded; changing requires a PR".

**Action option B** (full config surface): expose each knob in the schema.

**Recommendation**: A. Additional configurability is deferred until someone needs to adjust (with data). Mark as "ergonomic deferral".

**State**: pending

---

### B9 — Documented events that are never emitted ✅ RESOLVED (asymmetric)

**Doc says**:
- `memory.silent_retrieval_miss` (doc 07 §4.6)
- `memory.search.failure` (doc 07 §8.1)

**Code says**:
- `grep -rn "memory\.silent_retrieval_miss\|memory\.search\.failure" durin/` → zero hits.
- They're not in the `EVENTS` registry of `durin/telemetry/schema.py`.

**Who is right**: doc proposes, code doesn't implement. **Each event is legitimate** — `silent_retrieval_miss` would let us detect "the user asked X, it should have been in memory, didn't surface" (critical telemetry to validate G3.b query rewriting). `memory.search.failure` would enable degradation alerts.

**Action**:
- `memory.search.failure`: implement in `search_pipeline.py` when a safe wrapper recovers (P5.2 already has `recovered_from`); easy. ~20 LOC.
- `memory.silent_retrieval_miss`: complex — requires LLM judge or user feedback. Defer; remove from doc 07 §4.6 or mark as "research item".

**Resolution (2026-05-28) — Asymmetric: failure implemented, silent_retrieval_miss discarded**: the user pushed back with the key question about `silent_retrieval_miss`: *"how can this be detected effectively across multiple languages, I can't think of a way"*. The honest review confirmed that 2 of the 3 proposed heuristics (negation tokens, correction patterns) are inherently English-shaped, and 1 (substring overlap) generates too many false positives. Without an LLM-based classifier (which breaks the telemetry budget), the event is not viable for the multi-lingual workloads durin serves (LoCoMo seed uses CJK + Spanish). Move from "deferred" to **discarded** with the lesson.

**`memory.search.failure` — IMPLEMENTED**:

- [durin/telemetry/schema.py](../../durin/telemetry/schema.py): new `MemoryRecallFailureEvent` TypedDict with shape trimmed vs the v1 spec (no `kind` enum or `recoverable` bool — the wrappers don't classify exceptions today; inventing those fields would be fabricated data).
- [durin/memory/search_pipeline.py](../../durin/memory/search_pipeline.py): new `_emit_search_failure()` invoked at the end of `run_search_pipeline` when `recovery["sources"]` is not empty. `degraded_to` derived from the counts: `full` (only grep failed and the others covered), `vector_only`, `lexical_only`, `grep_only`, or `none` (recovery_succeeded == False). Wrapped in try/except — an emit failure never breaks the search result.
- [tests/memory/test_search_failure_event.py](../../tests/memory/test_search_failure_event.py) (new, 5 tests):
  * Clean run → no event.
  * Vector fails but lexical produces hits → degraded_to=lexical_only.
  * All sources fail → recovery_succeeded=False, degraded_to=none.
  * TypedDict registered in EVENTS + has the required fields.
  * `emit_tool_event` raises → search result intact (telemetry never breaks search).

**`memory.silent_retrieval_miss` — DISCARDED**:

- [docs/memory/07_telemetry_and_observability.md §4.6](07_telemetry_and_observability.md): rewritten — the event is no longer emitted, the section points to doc 08 §2.11 with the reason.
- [docs/memory/08_scope_and_discarded.md §2.11](08_scope_and_discarded.md) (new): permanent entry with 4 discard reasons + 3 alternatives if the signal is needed in the future + general lesson about "heuristic detectors with language-specific token lists are a red flag for any subsystem that has to serve multi-lingual workloads".

**Doc 07 §8.1** updated with real payload shape + explanation of why `kind` and `recoverable` were trimmed vs the v1 spec.

**Lessons applied**:
- [[feedback-question-user-input]]: the user pushed "how do you do this cross-lingual?" — without that push I would have implemented the heuristics as "deferred" pretending the problem was scheduling. The right question wasn't "when" but "whether it even makes sense".
- [[feedback-telemetry-is-first-class]]: applies for `search.failure` (degradation data the operator would want). Does NOT apply for `silent_retrieval_miss` with the proposed approach — unreliable data is worse than no data (noise > silence).
- [[feedback-optimization-vs-principle]]: applied to the spec. The v1 of `silent_retrieval_miss` violated the principle "must serve multi-lingual workloads"; defending it as "will be deferred" would have repeated the A8 pattern inverted (wiring something speculative whose maintenance cost exceeds the value).
- New future entry in persistent memory: "heuristic detectors with language-specific token lists are a red flag for multi-lingual systems".

**Pre-commit verification**: 5/5 tests for the search failure event; memory suite 969+ passing.

**State**: resolved (commit pending).

---

### B10 — Emitted events not documented ✅ RESOLVED

**Code says**:
- `memory.embedding.load` (`durin/memory/embedding.py:172`)
- `memory.embedding.embed` (`durin/memory/embedding.py:192`)
- `memory.hot_layer.failure` (`durin/memory/hot_layer.py:161`)

All three are in the `EVENTS` registry and are emitted.

**Doc says**: doc 07 §3 category tables don't list them.

**Who is right**: code (emits legitimately useful events). Doc incomplete.

**Action**: add the 3 events to doc 07 with their payload schemas.

**State**: pending

---

### B11 — Doc 06 §2 only mentions `## Memory` (incomplete) ✅ RESOLVED

**Doc says** (`docs/memory/06_prompts_and_instructions.md` §2): reproduces only the `## Memory` block of identity.md.

**Code says** (`durin/templates/agent/identity.md:35-46`): besides `## Memory`, there is `## Memory writing` that gives writing guidance (dedup, when NOT to call memory_store).

**Who is right**: code (has useful content the doc hides).

**Action**: update doc 06 §2 to reproduce BOTH sections verbatim.

**State**: pending

---

### B12 — Cross-encoder model NOT validated against curated list ✅ RESOLVED

**Doc says** (`docs/memory/03_search_pipeline.md` §9.5): *"dropdown for picking the model from the curated list (jina-v2, bge-base, bge-v2-m3, qwen3-reranker-0.6b)"*.

**Code says** (`durin/config/schema.py:266-273`):

```python
class CrossEncoderConfig(Base):
    enabled: bool = False
    model: str = "jinaai/jina-reranker-v2-base-multilingual"  # free string
    batch_size: int = 32
    top_n: int = 10
```

There's no validator, no enum. An invalid value (e.g. `model: "bogus"`) passes config and crashes at load.

**Who is right**: doc is right in intent (curated list). But a **strict** enum breaks extensibility — a user wanting to try a new model shouldn't have to edit the schema.

**Action option A**: soft validator (list of "known good", warn if not in it, don't fail). ~10 LOC.

**Action option B**: leave as free string, align doc to "models known to work" (no curated dropdown).

**Recommendation**: A. Warn-but-allow is the right balance. The webui already filters to the 4 known; the config schema accepts others but logs a warning.

**Resolution (2026-05-28) — Option C: dynamic validation, no fixed list**: the user pushed back with the key observation: *"Models, before being selected and assigned, should pass a test. The ones durin offers at install won't be the only ones allowed; eventually the user should be able to set another, whether via ollama, the API of models we already support, or customs. But I don't see a fixed list outside the one at initial install."*

That discarded both Option A (soft validator with list) and Option B (leave free + doc fix). The correct fix is to **test the model live** before accepting the value — the `check_model_ping` pattern that already exists for LLM models.

Backend changes:

- [durin/memory/cross_encoder.py](../../durin/memory/cross_encoder.py): new `test_model(model_id, *, loader=None) → dict` with shape `{status, message, model_id, duration_ms}`. Attempts `_load_default_scorer(model_id)` + trivial score. Handles four failure modes: empty id, loader returns None (no sentence_transformers or model not found), loader raises (network error, etc.), score raises (model loaded but broken).
- [durin/channels/websocket.py](../../durin/channels/websocket.py): new `GET /api/memory/cross-encoder/test?model=<id>` endpoint (`_handle_cross_encoder_test`). Async + `asyncio.to_thread` so the slow load doesn't block the gateway event loop.

Webui changes:

- [webui/src/lib/api.ts](../../webui/src/lib/api.ts): new `testCrossEncoderModel(token, model)` function + `CrossEncoderTestResult` interface.
- [webui/src/components/settings/MemorySettings.tsx](../../webui/src/components/settings/MemorySettings.tsx): "Reranker model" control refactor. Before: closed dropdown of 4 values. Now: free-form input with HTML `<datalist>` for the 4 suggested + "Test" button + status area. The user can type any id; the Test button invokes the new endpoint; the result (green ok / red fail with message) is shown inline.
- [webui/src/i18n/locales/en/common.json](../../webui/src/i18n/locales/en/common.json): updated strings — `crossEncoderModelPlaceholder` and `crossEncoderTest`; the `crossEncoderModels` namespace (per-model labels) was removed since there's no closed list.

Tests:

- [tests/memory/test_cross_encoder_model_validation.py](../../tests/memory/test_cross_encoder_model_validation.py) (new, 8 tests): cover the 4 failure modes + happy path + critical invariant:
  * `test_no_hardcoded_model_enum_in_config_schema`: asserts that `CrossEncoderConfig.model_fields["model"].annotation is str` (free-form). If someone re-introduces a `Literal[...]` or an `enum`, the test fails loudly — defense against regression to the anti-user-extensibility pattern.

Doc:

- [docs/memory/03_search_pipeline.md §9.5](03_search_pipeline.md): explicit clarification: "The model set is open. The four entries below are bundled in the install as suggestions… but the config field accepts any sentence_transformers compatible id. Validation is dynamic via the Test button."

**Lessons applied**:
- [[feedback-question-user-input]]: without your push-back I would have implemented Option A (soft validator with hardcoded list), which was exactly the user-restrictive anti-pattern you warned me about.
- [[feedback-sync-tests-exercise-behavior]]: the `model_fields["model"].annotation is str` invariant test exercises the schema contract, doesn't compare strings — defense against someone converting the field to a Literal in the future.
- Pattern similar to A8 / [[feedback-telemetry-is-first-class]]: the correct validation is not "allow or not based on a list" but "exercise the real behavior" — load + score, just as `check_model_ping` does for LLM models.

**Pre-commit verification**:
- Backend: 8/8 helper tests + 2328 full suite (no regressions).
- Webui: `npx tsc --noEmit` clean, `npx vitest run` 142/142.

**State**: resolved (commit pending).

---

## LOW — cosmetic / docs

### C1 — Doc 01 §4.3 references `STATEFUL_ATTRIBUTE_PATTERNS` which doesn't exist ✅ RESOLVED

**Doc says** (`docs/memory/01_data_and_entities.md` §4.3): *"The pattern set lives in code as a single source of truth (`STATEFUL_ATTRIBUTE_PATTERNS`)"*.

**Code says**: `grep -rn "STATEFUL_ATTRIBUTE_PATTERNS" durin/` → zero hits.

**Who is right**: doc lies. The constant doesn't exist. The "stateful attribute" logic is probably implicit in `entity_page.py::_validate`.

**Action**: either create the constant (extract from current code), or remove the reference from the doc.

**State**: pending

---

### C2 — Doc 01 §4.4 "soft cap 50 / hard cap 200" entries-per-entity not enforced ✅ RESOLVED

**Doc says** (`docs/memory/01_data_and_entities.md` §4.4): *"Per-entity cap — Soft cap = 50 (warn only), Hard cap = 200"*.

**Code says**: `grep -rn "50\|200" durin/memory/dream.py durin/memory/entity_page.py | grep -iE "cap|limit"` → zero semantically relevant hits.

**Who is right**: doc proposes, code doesn't enforce.

**Action**: implement the cap or remove from doc. Recommendation: implement the soft-cap (log warning when an entity has > 50 entries in its body). The hard cap is defensive — defer until it occurs.

**State**: pending

---

### C3 — Doc 01 §4.5 step 2 describes pinyin-with-tones, code uses direct unidecode ✅ RESOLVED

**Doc says**: *"Transliterate non-Latin scripts to Latin (e.g., 马塞洛 → mǎsàiluò → masailuo)"*.

**Code says** (`durin/memory/entities.py:153`): direct `unidecode(nfc)`. For "马塞洛", `unidecode` produces `"Ma Sai Luo "` → `ma_sai_luo`.

**Who is right**: code (simpler and correct). The pinyin-with-tones intermediate is fiction.

**Action**: update doc 01 §4.5 step 2: *"Transliterate non-Latin scripts to ASCII via unidecode (e.g., 马塞洛 → Ma Sai Luo → ma_sai_luo)"*.

**State**: pending

---

### C4 — Doc 05 §14 says 5 triggers, §2 enumerates 6 ✅ RESOLVED

**Doc says** §14 row 1: "Five trigger types".
**Doc says** §2: 6 triggers (`threshold`, `post_ingest_threshold`, `cron_daily`, `session_close`, `post_compaction`, `manual`).

**Code says** — 6 triggers actually wired (verified via grep in commit `c3eff1e`).

**Who is right**: §2 + code.

**Action**: correct §14 to "Six trigger types".

**State**: pending

---

### C5 — Doc 05 §8.7 mentions verdict `unsure`; code uses `unclear` ✅ RESOLVED

**Doc says** §8.7: *"flag uncertainty as `unsure` rather than confirm"*.
**Code says** (`durin/memory/absorb_judge.py:73`): verdicts = `{"same", "different", "unclear"}`.

§8.4 of the same doc 05 says `unclear` correctly.

**Who is right**: §8.4 + code.

**Action**: correct §8.7 to `unclear`.

**State**: pending

---

### C6 — Doc 07 §15 subtotals out of date ✅ RESOLVED

**Doc says** §15: "12 events in schema.py".
**Code says** `durin/telemetry/schema.py:911-937` — 25 memory.* entries.

**Doc says** §15: "query truncation: Not enforced".
**Code says** (`durin/agent/tools/_telemetry.py:29-33`) — it IS enforced via `_truncate_freetext`.

**Who is right**: code (current count).

**Action**: update §15 with real counts and status.

**State**: pending

---

### C7 — Doc 02 §11 status table fully stale ✅ RESOLVED

**Doc says** §11 (status table): "FTS5 lexical index — Does not exist"; "File watcher — Manual rebuild only"; "Archive folder — Doesn't exist".

**Code says**:
- `durin/memory/fts_index.py` exists + indexer uses it.
- `MemoryFileWatcher` exists (though not wired, see A11).
- `archive/` walker exists (`durin/memory/archive.py`).

**Who is right**: code. Doc 02 §11 is entirely obsolete.

**Action**: redo §11 from scratch reflecting current state.

**State**: pending

---

### C8 — Doc 03 §1 diagram has two "Step 7" (header collision) ✅ RESOLVED

**Doc says**: §11 "Step 7 — Removed (MMR deferred)"; §12 also titled "STEP 7".

**Action**: renumber.

**State**: pending

---

### C9 — Doc 06 §3.5 mentions `memory_*.py::DESCRIPTION` constants that don't exist ✅ RESOLVED

**Doc says** §3.5: *"descriptions must match `memory_*.py::DESCRIPTION` constants"*.
**Code says**: there's no `DESCRIPTION` constant in any tool. The canonical lives in `_PARAMETERS["description"]`.

**Who is right**: code.

**Action**: correct §3.5: *"matches `_PARAMETERS['description']` field"*.

**State**: pending

---

### C10 — Doc 04 §7.1 mentions webui surfaces — verify ✅ RESOLVED

**Doc says** §7.1: there are "informational" webui surfaces.

**Code says**: webui Settings → Memory now exists (P4.4 this turn). The doc doesn't reflect it with the detail of the 3 added controls.

**Action**: update §7.1 with the 3 MemorySettings.tsx controls.

**State**: pending

---

## NON-actionable items (record only)

### D1 — Doc 09 spec, no status claims
OK — reference, doesn't change.

### D2 — Doc 98 known_bugs.md
Only 1 entry (B1 absorption vector index), marked Resolved 2026-05-27. Verified via `absorption.py:244-253`. OK.

### D3 — Doc 99 gaps_audit.md
Round 1-3 marked resolved. Spot-checks confirm. OK.

---

## Executive summary

| Block | Items | Nature | State |
|---|---|---|---|
| Critical (A1-A11) | 11 | Affect agent UX, operation, or measurability | ✅ Closed 2026-05-28 |
| Medium (B1-B12) | 12 | Drift without breaking direct UX | ✅ Closed 2026-05-28 |
| Low (C1-C10) | 10 | Cosmetic / docs | ✅ Closed 2026-05-28 |
| Not actionable (D1-D3) | 3 | OK as-is | ✅ Recorded 2026-05-28 |
| Second pass (E1-E38) | 38 | Drift discovered in re-audit | ✅ Closed 2026-05-28 |

**Total**: 36 items first pass + 38 items second pass = **74 items reconciled as of 2026-05-28**.

**Second-pass commit tally**:
- `42d0986` feat(memory): close E1-E9 second-pass audit (high impact — telemetry + embedding v2.a + EntityPage author + rebuild gap).
- `51b3579` feat(memory): close E10-E15 (doc 03 drift + cursor wiring regression + entities meta bug).
- `935e330` feat(memory): close E16-E20 (doc 04 shapes + EntityPage user-authored protection + walker contract).
- `2c8495b` docs(memory): close E21-E23 (doc 05/06 status drift).
- (TBD) docs(memory): close E24-E38 (cosmetic batch — status rows, numbering, CLI commands).

**Code regressions closed in the second pass**:
- E11: pre/post-cursor wiring lost in the v2 migration (commit c820447) — restored.
- E11 bonus: `_resolve_meta` was not propagating `entities` from vector_meta — fixed.
- E19: user_authored entity page protection was arch-unsupported — `EntityPage.author` + `_maybe_auto_absorb` check shipped.
- E5: documented dashboards (§10.3 perf, §216 capacity) were impossible to implement — `memory.index.write` extended with `duration_ms` + `trigger`.
- E9: v2.a (rendered_frontmatter in entity pages) + fix for the `rebuild_from_workspace` gap that wasn't walking entity pages.

**Lessons recorded in project memory during the second pass**:
- `feedback_telemetry_is_first_class`, `feedback_optimization_vs_principle`, `feedback_sync_tests_exercise_behavior`, `feedback_verify_quantifiers`, `feedback_heuristic_detectors_multilingual` (refreshed from the first pass).

**Resolution order (historical reference)**:
- First pass: A1 → A2 → A3 (the three tools — agent UX) → A11 (watcher+cron wiring) → A9 (decay) → A10 (session summaries) → A8 (push wiring) → A5+A6+A7 (telemetry payload) → A4 (LanceDB schema doc) → B/C/D in order.
- Second pass: E1-E9 high-impact (same flow of evidence → proposal → OK → implement → TDD → commit), then E10-E15 medium (doc 03), E16-E20 (doc 04 + EntityPage author), E21-E23 (doc 05/06 status), E24-E38 cosmetic batch.

**Maintenance**: items marked ✅ RESOLVED are the immutable decision log. Do not delete; when a fix is superseded by a later audit, append a "Superseded YYYY-MM-DD by X" note instead of rewriting the original record.

---

## SECOND PASS (E) — drift discovered in re-audit 2026-05-28

### E1 — `memory.recall` event payload doesn't match doc 07 §4.1 ✅ RESOLVED

**Doc 07 §4.1 (pre-E1)** listed 10 fields: `query`, `keywords`, `scope`, `level`, `result_count`, `total_candidates`, `strategy`, `recovered_from`, `recovery_duration_ms`, `duration_ms`.

**Code `durin/agent/tools/memory_search.py` (pre-E1, lines 454-462)** emitted only 4: `query`, `scope`, `level`, `result_count`. The `MemoryRecallEvent` TypedDict only accepted those 4 + auto-injected `iteration`/`session_key`.

**Verification**: grep `"memory.recall"` across the repo confirms a single emission (`memory_search.py:454`). The 6 "missing" fields are ALREADY computed locally before the emission (`strategy` at line 441-448, `duration_ms` at 390, `pipeline_result.vector_count + lexical_count` for `total_candidates`, `keywords` is a kwarg, `recovered_from` comes from `pipeline_result`).

**Decision**: A8-style (telemetry is first-class infra) — expand the payload, do NOT shrink the doc. Zero new overhead: all values were already computed.

**Resolution**:
- `MemoryRecallEvent` TypedDict extended with required `strategy`/`duration_ms`/`total_candidates` + optional `keywords`/`recovered_from`/`recovery_duration_ms`.
- Callsite builds the dict and adds recovery only on degraded runs (mirrors the tool's response shape).
- TDD tests: 6 cases in `tests/memory/test_recall_event_payload_e1.py` (strategy+duration, total_candidates, keywords with/without, recovery with/without).
- Doc 07 §4.1 rewritten with a `Required` column to distinguish always-on vs degraded-only.

**Commit pending** (E1-E9 batch close).

### E2 — `memory.recall.lexical` field names doc vs code ✅ RESOLVED

**Doc 07 §4.3 (pre-E2)** listed `query`, `tokenizer_used` with values `unicode61|trigram|like_fallback`, `hit_count`, `duration_ms`.

**Code `durin/memory/lexical_search.py:124-133` + TypedDict `MemoryRecallLexicalEvent`**: emits `route` (with values `unicode61|trigram|like_substring`), `query_chars`, `cjk_chars`, `hit_count`, `duration_ms`.

**Verification**: the TypedDict at `schema.py:780-796` is well-structured and the emission matches; the doc was never updated when the field was named `route` instead of the original `tokenizer_used` placeholder. `like_substring` is the `LexicalRoute` enum value (not `like_fallback`).

**Decision**: doc → code. The code is correct and useful (route + query/cjk char counts give "how many queries fell to the CJK fallback" dashboards). Rewriting §4.3.

**Resolution**: doc 07 §4.3 rewritten with the 5 real fields + note on why `query` is not duplicated (already in `memory.recall`, join by `session_key+iteration`).

**Genealogy**: commit `792f1c6` (Phase 3 core) introduced the event with `route` from the first version. Doc 07 §4.3 was an aspirational spec ("NEW event") never reconciled. Zero downstream consumers (verified by grep).

**Commit pending** (E1-E9 batch close).

### E3 — `memory.recall.rrf` field names doc vs code ✅ RESOLVED

**Doc 07 §4.5 pre-E3**: `sources_active` (list), `keyword_boost_applied` (bool), `dedup_count` (int), `duration_ms`.

**Code `durin/memory/rrf_fusion.py:148-158` + TypedDict `MemoryRecallRRFEvent`**: emits `vector_count`, `lexical_count`, `grep_count`, `fused_count`, `boosted`, `duration_ms`.

**Genealogy**: same `792f1c6` commit as E2. Aspirational spec doc, impl diverged and doc never reconciled.

**Consumers**: zero (grep in `durin/` confirms only the emitter and the TypedDict declare these fields; `memory_search.py` reads from `SearchPipelineResult`, not from the event).

**Decision**: doc → code (Option A). Reasons:
- Per-source counts are strictly richer than `sources_active` (derivable as `{s: count>0}`).
- `dedup_count` is derivable as `vector_count + lexical_count + grep_count − fused_count` (count of (URI, source) pairs merged in RRF).
- `boosted` vs `keyword_boost_applied` is a pure rename; the former is more concise.
- Zero code touched.

**Resolution**: doc 07 §4.5 rewritten with the 6 real fields + note on mathematical derivation for `sources_active` and `dedup_count`.

**Commit pending** (E1-E9 batch close).

### E4 — `memory.recall.decay` event emitted without entry in doc 07 ✅ RESOLVED

**Doc 07 §4 (pre-E4)**: recall events table lists 4.1-4.6 with no entry for decay.

**Code**: `durin/memory/search_pipeline.py:594-601` emits `memory.recall.decay` with `hits_total`/`hits_decayed`/`avg_decay_factor`. `MemoryRecallDecayEvent` TypedDict declared at `schema.py:859-880`.

**Genealogy**: A9 (first-pass audit) introduced the event + TypedDict but didn't add a doc entry.

**Decision**: pure additive. `§4.6` (silent_retrieval_miss discarded) is referenced from doc 08 and doc 11 — do NOT renumber; append as `§4.7`.

**Resolution**: doc 07 §4.7 added describing the 3 fields + note about how decay interacts with non-decaying classes (factor=1.0) + pointer to doc 03 §10.3 for config.

**Commit pending** (E1-E9 batch close).

### E5 — `memory.index.write` minimal payload vs documented dashboards ✅ RESOLVED

**Doc 07 §9.1 (pre-E5)**: aspirational spec with 5 fields: `uri`, `trigger`, `targets`, `duration_ms`, `embedding_skipped`.

**Code `durin/memory/indexer.py:212-218` (pre-E5)**: emitted only `uri`, `op`, `index` (always `"fts"` in practice).

**Evidence of documented consumers** (key to deciding direction):
- Doc 07 §10.3 defines alert `index_write_p95_ms < 50ms (per row)` which requires `duration_ms`.
- Doc 09 §216 declares mitigation for trigram table growth: "monitor via `memory.index.write` events" — needs `trigger` to distinguish bursts.

**Genealogy**: commit `be75998` (Phase 2 core) introduced the emitter with minimal shape; the doc was written as an aspirational spec and never reconciled.

**Decision**: B-minimal (code → partial doc). Add `duration_ms` + `trigger` to the emitter; discard `targets`/`embedding_skipped` as aspirational (LanceDB doesn't write this event; no mtime short-circuit).

**Revised trigger taxonomy** (vs original spec):
- Discarded: `tool_write` (no direct call sites from tools), `manual_rebuild` (that path emits `.rebuild`, not `.write`).
- Real: `watcher` (default, file_watcher), `dream_apply` (post-consolidation), `drift_repair` (health check).

**Resolution**:
- `_emit_write` extended with `trigger` (kw) + `duration_ms` (kw).
- `reindex_one_file` accepts default `trigger="watcher"` + measures duration on upsert and delete paths.
- Call sites: dream.py:666 passes `dream_apply`; health_check.py:231 passes `drift_repair`; file_watcher.py uses default.
- `MemoryIndexWriteEvent` TypedDict updated with the 2 fields as required.
- Doc 07 §9.1 rewritten with real shape + trigger taxonomy + note discarding `targets`/`embedding_skipped`.
- TDD tests: 5 cases in `tests/memory/test_index_write_event_e5.py` (duration_ms, default trigger, dream_apply trigger, drift_repair trigger, delete op preserves fields).

**Commit pending** (E1-E9 batch close).

### E6 — Doc 07 §15 "Cost in dream.end" row status stale ✅ RESOLVED

**Doc 07 §15 (pre-E6)**: "Cost in dream.end" row said current state = "Not present", v2 target = "Add `llm_input_tokens_total`, `llm_output_tokens_total`, optional `llm_cost_usd`".

**Post-A5 reality**: A5 (first-pass audit, same doc) already shipped the 3 fields in `memory.dream.end`. The row immediately before ("Memory event registry") even acknowledges "A5 added cost fields to `dream.end`".

**Decision**: flip status row to "shipped" with A5 reference. `llm_cost_usd` remains out-of-scope with reason in §1.

**Resolution**: row rewritten reflecting shipped + pointer to §6.2 and E6.

**Commit pending** (E1-E9 batch close).

### E7 — `silent_retrieval_miss` residue in docs post-discard ✅ RESOLVED

**Context**: §2.11 of doc 08 (audit B9, 2026-05-28) discarded the `memory.silent_retrieval_miss` event and its 3 heuristics (substring overlap + English-shaped negation tokens + correction patterns) for not being multi-lingual viable. Doc 07 §4.6 was rewritten pointing to §2.11. But 4 residual references remained that still cited the discarded event as active.

**Residues found**:
1. `08_scope_and_discarded.md` §5 line 349 — "§2.F eager pre-fetch" row cites `memory.silent_retrieval_miss > 5%` as trigger.
2. `08_scope_and_discarded.md` §4.1 lines 391-397 — "Trigger to revisit" section describes the event + 3 heuristics as an active mechanism.
3. `09_implementation_roadmap.md` §10.1 line 352 — Phase 7 checklist lists `memory.silent_retrieval_miss` as an event to implement.
4. `99_gaps_audit.md` lines 105 and 681 — historical decision records describe the event as an active decision without a supersession note.

**Decision**: doc → doc consistency, respecting the §2.11 discard. Replace the telemetric trigger with the alternatives §2.11 explicitly suggests: explicit user feedback, bench failure cluster on LoCoMo/EverMemBench, offline LLM judge over traces (post-hoc, not per-turn).

**Resolution**:
- doc 08 §5: §2.F row rewritten with 3 language-agnostic triggers.
- doc 08 §4.1: "Trigger to revisit" subsection rewritten; no longer describes the discarded event as an active mechanism.
- doc 09 §10.1: Phase 7 checklist now lists 13 events; `silent_retrieval_miss` removed with a discard note; `recall.decay` added (A9).
- doc 99 historical records (lines 105 + 681): append "Superseded 2026-05-28 (B9 + §2.11 + E7)" note without rewriting the original record.

**Commit pending** (E1-E9 batch close).

### E8 — Doc 03 §14.7 failure event schema stale vs B9 canonical ✅ RESOLVED

**Doc 03 §14.7 (pre-E8)**: JSON shape with 3 fields (`component` single-value enum, `kind` 6-enum, `degraded_to` 4-enum + null) + explicit note "No `recovery_attempted` field".

**Doc 07 §8.1 (post-B9 canonical)** + real code (`search_pipeline.py:240-249`): 5 fields (`component` comma-joined string, `recovery_attempted` bool, `recovery_succeeded` bool, `recovery_duration_ms` float, `degraded_to` 5-enum `full|vector_only|lexical_only|grep_only|none`). No `kind` field (B9 discarded it).

**Specific divergences**:
1. `kind` listed in doc 03; discarded by B9 (wrappers catch generic Exception → emitting `kind` would be invented data).
2. Doc 03 explicitly says "No `recovery_attempted` field"; code does emit it (`recovery_attempted: True` always — forward-compat marker).
3. `component` in doc 03 is a single enum; code is comma-joined string (multiple affected possible).
4. `degraded_to` in doc 03 includes "no_rerank"/null; code uses "full"/"none".
5. Missing in doc 03: `recovery_succeeded`, `recovery_duration_ms`.

**Genealogy**: doc 03 §14.7 is a pre-B9 aspirational spec never reconciled. Doc 07 §8.1 was the B9 output with the definitive schema.

**Decision**: doc 03 §14.7 → collapse to a pointer to the canonical in doc 07 §8.1 (DRY, avoids re-drift). Keep in doc 03 the historical note about discarded `kind` + `recovery_attempted` fields with the B9 reason.

**Resolution**: doc 03 §14.7 rewritten as 2 paragraphs: (1) "event emitted — canonical schema in doc 07 §8.1", (2) note on what v1 spec asked for and why B9 cut it.

**Commit pending** (E1-E9 batch close).

### E9 — Doc 02 contradiction about v1/v2 embedding text (ship v2.a, supersede v2.b) ✅ RESOLVED

**Doc 02 (pre-E9)**: §4.2 + §4.3 presented v2 as an active "target"; §10 rows 4+5 listed v2 as a resolved decision; §11 (post-C7) reported "v2 never shipped, entity-aware ranker covers the case". Triple contradiction.

**Sub-decisions separated by evidence**:
- **v2.a (rendered_frontmatter in entity pages)**: translates `attributes` and `relations` to prose in the embedding text. Closes a real recall gap on attribute-type queries ("X's email", "who is Y's spouse"). The entity-aware ranker does NOT cover this — the ranker re-orders candidates within the top-50, but the page has to enter the top-50 via centroid.
- **v2.b (entities_with_aliases in entries)**: would expand URIs in the embedding text. The entity-aware ranker (A1) covers exactly this case at query-time. v2.b is duplicate work.

**Decision (with user OK 2026-05-28)**: ship v2.a; supersede v2.b by A1.

**Pre-existing gap discovered**: `rebuild_from_workspace` did not walk entity pages — only `memory/<class>/*.md` entries. Post forced-rebuild (schema bump) the entity page rows disappeared from the index until the next Dream/absorb. Fixed as part of E9.

**Resolution**:
- New `VectorIndex._render_frontmatter(attributes, relations)` helper: renders attributes with `_title_key`, skips internal metadata (provenance, dream_processed_through, created_at, updated_at), stateful attributes render only `current`, relations render `Type: target (since date)`.
- `_compose_entity_page_text` extended with `attributes`/`relations` kwargs (defaults None preserve v1 behavior).
- `upsert_entity_page` new plumbing for attributes/relations.
- Call sites: `dream.py:650-657` and `absorption.py:253-260` pass `page.attributes` + `page.relations`.
- `rebuild_from_workspace` now walks `memory/entities/` in addition to `memory/<class>/` and builds records via a new `_entity_page_record` helper.
- `CURRENT_SCHEMA_VERSION` bumped 3 → 4 (E9 — forces rebuild to realign centroids).
- Doc 02 §4.2 marks v2.a shipped + note that summary slot is deferred; §4.3 marks v1 final + v2.b superseded by A1; §10 rows 4+5 updated; §11 adds a "Vector rebuild walks entity pages" row as bug-fix.
- Stub in `tests/memory/test_auto_absorb_dispatcher.py:343-352` extended to accept the new kwargs.
- TDD tests: 7 cases in `tests/memory/test_entity_page_embedding_v2a_e9.py` (rendered_attributes/relations, ordering preserved, empty case, skip internal metadata, stateful current only, rebuild walks entity pages).

**Validation**: 995 tests pass in tests/memory/ (1 pre-existing skipped).

**Commit pending** (E1-E9 batch close).

### E10 — Doc 03 §2.1 scope/level are not inputs to `run_search_pipeline` ✅ RESOLVED

**Doc 03 §2.1 (pre-E10)**: inputs table lists `scope`/`level`/`limit` alongside `query`/`keywords`, presenting all as inputs to the "search pipeline".

**Code**: `run_search_pipeline(workspace, query, *, keywords, vector_index, limit, cross_encoder, cross_encoder_top_n, temporal_decay_enabled)` — does NOT accept `scope` or `level`. These are handled in `MemorySearchTool` (memory_search.py:349 decides `vi=None` when `scope=undreamed`; line 424 filters hits post-pipeline; `level=cold` enriches with body afterwards).

**Decision**: doc → reality. Add a "Tool vs pipeline boundary" note explaining that §2.1 lists the tool surface inputs, and that the pipeline only consumes `query`/`keywords`/`vector_index`/`limit` directly. Zero code touched.

**Resolution**: doc 03 §2.1 extended with a "Tool vs pipeline boundary" block describing how each input is orchestrated (scope/level around the pipeline call; limit clamped to [1,50] in the tool; bodies enriched post-pipeline at cold-tier).

### E11 — Doc 03 §8.4 pre/post-cursor logic lost in v2 migration ✅ RESOLVED

**Doc 03 §8.4**: describes pre/post-cursor partitioning as active behavior of the entity-aware rerank.

**Pre-E11 code**:
- `entity_ranker.rank_with_entities(cursors=...)` implements the logic correctly (tests pass).
- `_load_cursors_from_entities_dir` helper (memory_search.py:31) loaded cursors from entity pages.
- v1 search path wired them with `cursors=cursors`.
- v2 search_pipeline `_entity_aware_rerank` did NOT wire them — it called `rank_with_entities` without `cursors=`.
- Helper became orphan post-migration.

**Genealogy**:
- Commit `b724fa8`: helper introduced and wired in v1.
- Commit `1ea70ac` (Phase 2.5/3.5): new `_entity_aware_rerank` in v2 pipeline WITHOUT cursors from day one.
- Commit `c820447` (Phase 5 d1): v1 → v2 migration eliminates the old function; helper becomes orphan in memory_search.py.

**Use case analysis** (with user, 2026-05-28):
- (a) Narrative texture: user requests event reconstruction → drill by URI works.
- (b) Evidence validation: agent cites source → drill to provenance URI works.
- (c) Temporal evolution: agent sees history → punctual drill works.

Those 3 cases are drill-by-URI, NOT broad searches. The canonical + N pre-cursor fragments duplication in EVERY general query is noise without value.

**Decision (with user OK, option B)**: restore cursor wiring in `_entity_aware_rerank`. Do NOT archive aggressively (option C discarded — would lose recall on raw content).

**Additional bug discovered** during TDD: `_resolve_meta` (search_pipeline.py:507) did NOT propagate `entities` from vector_meta. Result: non-entity_page entries arrived at `rank_with_entities` with `entities=[]` → no overlap → no entry boosted to the entity-match list (only the canonical page). This **masked** the pre-cursor regression: with no entries in the entity-match list, there was no observable difference between pre and post.

**Resolution**:
- `_load_cursors_from_entities_dir` helper moved to `entity_ranker.py::load_cursors_from_entities_dir` (next to its only consumer). Comment in memory_search.py:31 marks the move.
- `_entity_aware_rerank`: loads cursors after `extract_query_entities` and passes them to `rank_with_entities`.
- `_resolve_meta`: propagates `entities` from vector_meta when present.
- Orphan `EntityPage` import removed from memory_search.py.
- TDD tests: 2 cases in `tests/memory/test_pipeline_cursor_wiring_e11.py` (pipeline excludes pre-cursor from the boost; cursor loader returns correct dict).

**Validation**: 997 tests pass in tests/memory/ (995 base + 2 E11; 1 pre-existing skipped).

**Commit pending** (E10-E23 batch close).

### E16 — Doc 04 §2.2/§4.2/§5.2 return shapes vs code ✅ RESOLVED

**Doc 04 §2.2 (pre-E16)** memory_search return: listed `type`, `path`, `score` (don't exist), omitted `source`, `snippet`, `kind`, `class_name`, `entities`, top-level `strategy`, `ranking`. The "`recovered_from: null in normal operation`" claim was false (omitted, not null).

**Reality** (`Result.to_dict()` + `memory_search.py:454-480`):
- Per-result: `source`, `uri`, `headline`, `snippet`, `kind` always + `summary`/`body`/`class_name`/`valid_from`/`entities` conditional + `rendered`.
- Top-level: `results`, `total`, `strategy`, `ranking` always + `recovered_from`/`recovery_duration_ms` only on degraded.

**Doc 04 §4.2** memory_ingest: shape matches but `corpus_entry_id` didn't mark conditional.

**Doc 04 §5.2** memory_drill (E18): listed `path`; code returns only `{uri, content}`.

**Decision**: doc → reality. Rewrite §2.2 with detailed table (yes/conditional/never null), clarify §4.2 `corpus_entry_id` optional, remove `path` from §5.2.

### E17 — Doc 04 +12pp vs +3.9pp ✅ RESOLVED

**Internal contradiction**: §2.4 line 149 says "+3.9pp result"; line 155 says "+12pp on single-hop". `project_locomo_v2_prompts_result.md` memory records **+3.9pp overall (60.8% → 64.7%)**; the "+12pp single-hop" has no verifiable source.

**Decision**: apply `feedback_verify_quantifiers` — don't invent numbers. Align both lines to the verified value.

### E18 — Doc 04 §5.2 memory_drill path ✅ RESOLVED

**Doc**: included `"path": "memory/entities/person/marcelo.md"`. **Code `memory_drill.py:71`**: `return {"uri": uri, "content": text}` — no `path`. Doc → reality.

### E19 — Doc 01 §4.6.1 wrong pointers + arch gap ✅ RESOLVED (B-full)

**Doc 01 §4.6.1 line 480 (pre-E19)**: two false claims:
1. "`dream.py::DreamConsolidator.apply()` filters out user_authored entries" → the only filter is in `cli/memory_cmd.py:150` (`_discover_pending_consolidations`).
2. "`dream_runner.py::_maybe_auto_absorb` skips entity pages where author: user_authored" → no check existed AND `EntityPage` had no `author` field.

**Architectural gap discovered**: the doc promised protection for entity pages, but `EntityPage` didn't support `author`. Auto-absorb would merge pages hand-edited by the user.

**Decision (with user OK, B-full)**: close the full gap, not just the doc:
- `EntityPage` gains `author: str = "user_authored"` field (default safe).
- Frontmatter round-trip (lenient read with fallback, emit only when it differs from the default).
- `dream.py:511` placeholder and `absorption.py:360` merge product set `author="agent_created"`.
- `dream_runner.py::_maybe_auto_absorb` checks both pages and skips with `reason="user_authored"`.
- TDD tests: 3 cases in `tests/memory/test_auto_absorb_user_authored_e19.py` (canonical user-authored, absorbed user-authored, both agent-created proceeds).
- Stub helper `tests/memory/test_auto_absorb_dispatcher.py:_write_page` updated to pass `author="agent_created"` by default (dispatcher tests simulate Dream pages).
- Doc 01 §4.6.1 rewritten with correct pointers + E19 note.

**Validation**: 1000 tests pass in tests/memory/ (997 + 3 new E19; 1 pre-existing skipped).

### E20 — Doc 02 §6.5 walker contract bullet obsolete post-A10 ✅ RESOLVED

**Doc 02 §6.5 line 352 (pre-E20)**: "Also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present".

**Reality** (`walk_memory` in `paths.py:80-113`): only emits `.md` files under `memory/`. A10 (first-pass audit) moved the session summary from JSON sidecar to `memory/session_summary/<sanitized>.md`; the walker treats it like any other class. No peek to `sessions/.../meta.json` anywhere.

**Decision**: doc → reality. Remove the stale bullet and add a note explaining the post-A10 change.

**Commit pending** (E16-E23 batch close).

### E21 — Doc 05 §15 status table 4 rows "Not implemented" shipped ✅ RESOLVED

**Doc 05 §15 (pre-E21)** marked as "Not implemented" / "Not explicit":
- Provenance tracking
- Archive of consumed episodic
- Git commits (Hybrid model)
- Failure quarantine

**Reality** (Phase 1.9, commit `6aafc3f`): all 4 are shipped.
- `dream_patch_parser.py` + `dream_apply.py` collect provenance per op.
- `dream_archive_consumed.py::archive_consumed_episodic` moves to `memory/archive/episodic/`.
- `dream_commit_message.py` + `dream_git_history.py` implement the hybrid model.
- `dream_quarantine.py` + frontmatter fields `dream_failure_count` / `dream_quarantine` + 3-strike logic.

**Decision**: flip to "Shipped (Phase 1.9)" with concrete module pointer in each row + audit E21 reference.

### E22 — Doc 05 §14 row 8 verdict vocab obsolete ✅ RESOLVED

**Doc 05 §14 row 8 (pre-E22)**: "LLM-judged: merge / keep_separate / unsure".

**Code `absorb_judge.py:6,84`**: real vocabulary is `same | different | unclear`. Auto-merge only when `verdict == "same"` AND `confidence ≥ threshold`.

**Genealogy**: possible holdover from the original spec. Never matched the real enum.

**Decision**: doc → reality.

### E23 — Doc 06 §10 status rows identity + onboarding ✅ RESOLVED

**Doc 06 §10 (pre-E23)**:
- "identity.md Memory section | v2 shipped 2026-05-25 (+3.9pp) | Light revision per §2 | Minor wording polish" — the "light revision pending" had no concrete scope; the bench gain was over what's in the template today.
- "Onboarding wizard text | Partial | Add §6 questions" — accurate, `onboard.py` (1169 LOC) has no grep hit for "memory".

**Plus**: doc 06 §2.2 also had "+12pp on single_hop" (same stale claim removed from doc 04 §2.4 in E17). Fix extended.

**Decision**: identity row to fully shipped (drop "light revision pending"); onboarding row keeps "Partial" but with concrete evidence (grep miss); +12pp also removed from doc 06 §2.2.

---

## THIRD PASS (F) — drift discovered in re-audit 2026-05-28

After closing E1-E38 and verifying the full test suite (5088/0 fail), the user asked for a third pass to validate that doc + code stayed consistent. Sub-agents found ~17 new items. Most are drift the second pass didn't reach.

### F1 — Doc 00 §189 `class_half_life_overrides` promise ✅ RESOLVED

**Doc 00 §189 (pre-F1)**: "Configurable via `memory.search.temporal_decay.class_half_life_overrides`."

**Code `durin/config/schema.py:276-291` (pre-F1)**: `MemoryTemporalDecayConfig` only had `enabled: bool`. The promised field **did not exist**. `resolve_class_half_life(class_name)` only consulted `CLASS_HALF_LIFE_DEFAULTS` without overrides.

**"Good system" analysis**:
- Real operators may need to tune: active workspace (90d → 30d), long-running multi-year workspace (90d → 365d), per-class enable/disable.
- The global toggle already exists but is too coarse — it does not allow "decay active but conservative".
- A9 wired all the infra; the override is a small toggle on top.

**Decision**: code → doc (ship the field). Rationale:
1. Reasonable, useful promise — not aspirational.
2. Low cost (~30 LOC + TDD), zero regression risk (default `{}` = no-op).
3. Closes drift by honouring the promise instead of retracting it.

**Resolution**:
- `MemoryTemporalDecayConfig.class_half_life_overrides: dict[str, int | None] = {}` added.
- `resolve_class_half_life(name, *, overrides=None)` extended with semantics: present-int → use, present-None → disable, absent → fall through to default.
- `apply_class_decay` accepts and forwards `overrides`.
- `_temporal_decay_step` accepts and propagates.
- `run_search_pipeline` accepts `temporal_decay_overrides`.
- `memory_search.execute` reads `cfg.memory.search.temporal_decay.class_half_life_overrides` and threads it through.
- TDD tests: 9 cases in `tests/memory/test_class_half_life_overrides_f1.py` (default + override-int + override-null + add decay to no-op class + unknown class + apply_class_decay threads + null disables + config field exists + end-to-end via memory_search).
- Doc 00 §189 updated: marks "audit F1 (2026-05-28)" + clarifies semantics (map class → days, `null` to disable).

**Commit pending** (F1-F11 batch close).

### F3 — Doc 03 §4.2 embedding dim 768 vs 384 ✅ RESOLVED

**Doc 03 §4.2 (pre-F3)** (line 214): `vector = MiniLM.embed(query)  # 768-dim`.

**Reality**: MiniLM-L12-v2 emits 384-dim (doc 02 §3.2 says so correctly; embedding.py confirms).

**Decision**: doc → reality, cosmetic fix.

**Resolution**: line 214 updated to `# 384-dim (audit F3, 2026-05-28)`.

**Commit pending** (F1-F11 batch close).

### F4 — Complete Phase 3 sectioned_output migration ✅ RESOLVED

**Context**: Phase 3 (commit 792f1c6, 2026-05-28) shipped `query_router`, `RRF`, `sectioned_output`, `lexical_executor` as infrastructure — but the callsite wiring in `memory_search` was left intact. Result: two parallel renderers (`Result.render_block` in search.py vs `sectioned_output._render_block`) with different formats. The Phase 3 intent (centralised rendering with section intros + active per-source cap + cross-section grouping) never landed in production.

**Why we hadn't advanced before**: the callsite migration was probably deferred to minimise risk of agent output change; the audit passes (E1-E38) checked field-level drift but did not trace the rendering codepath end-to-end. A system with two renderers for the same concept was technical debt that the second and third pass should have caught.

**Pre-F4 reality**:
- `memory_search.execute` called `r.render_block()` per Result (legacy path).
- `sectioned_output._render_block` emitted a basic marker (snippet only, no END close).
- Section intros never reached the LLM (the legacy path didn't have them).
- The per-source cap WAS applied in the pipeline (search_pipeline.py:182) but rendering was per-row, losing the cross-section grouping Phase 3 wanted.

**Decision**: full migration (user-explicit).

**Resolution**:
- `SectionedHit` extended with `summary` and `entities` (frozen dataclass, new defaults).
- `_render_block` enriched with: END marker (`=== END KIND ===`), body preference `summary > body > snippet`, entities tail (`Entities: ref, ref`) for non-canonical, `(canonical entity page)` hint when no ts.
- `_marker_for` now honours `ts=""` → format without `(ts ...)` suffix.
- `memory_search.execute` main path + archive path: convert Results into enriched SectionedHits, apply `apply_per_source_cap`, call `render_sectioned`. Response shape gains `sectioned_rendered` (string), loses per-row `rendered` (WebUI didn't consume it; the LLM reads the sectioned string).
- `Result.render_block` removed (it had 3 callsites, all in memory_search.py — all migrated).
- TDD tests: 7 new cases in `tests/memory/test_sectioned_migration_f4.py` (END markers, ts/no-ts canonical, summary preference, fallback body/snippet, entities tail, sectioned_rendered field, section intros).
- Tests migrated: `test_fragment_canonical_contract.py::TestRenderBlock` removed (3 cases) and `test_memory_search_tool_includes_rendered_blocks` → `test_memory_search_tool_emits_sectioned_rendered`.
- Doc 03 §12.1/§12.2/§12.4: marker table to the real format, body preference + entities tail documented, section intros mentioned, `max_per_source` config marked "not yet implemented" (gap for a future F).

**Full suite green**: 5107 passed, 16 skipped, 0 failed.

**Out of F4 scope (deferred)**:
- `hot_layer._render_canonical_block` (parallel renderer for eager pre-injection) — different use case (carries the full entity page structure), not in F4 scope. **Updated by G7 (2026-05-28)**: full unification is decided against (doc 08 §2.15); the marker convention is now shared via `durin.memory.section_markers` while the renderers keep their distinct body logic.
- `memory.search.sectioning.max_per_source` config knob — the cap works but is hard-coded; lift to config later if an operator asks. **Updated by G1 (2026-05-28)**: shipped as `MemorySearchSectioningConfig.max_per_source: int = 3`; the original promise in doc 03 §16 row 8 now matches code.

**Commit pending** (F1-F11 batch close).

### F5 — Doc 04 §2.2 return shape example stale ✅ RESOLVED

**Doc 04 §2.2 example (pre-F5)**: `valid_from: "2024-01-15"` for entity_page; `rendered` per-row field.

**Reality**: entity pages always write `valid_from = ""` (doc 03 §10.4 says so; vector_index.py:149,487 confirms). The `rendered` per-row field was removed in F4.

**Decision**: doc → reality, expand example to a 2-result shape to show the entity vs entry distinction; top-level fields table updated with `sectioned_rendered`.

**Resolution**: doc 04 §2.2 rewritten with a 2-result example, `rendered` row replaced by `sectioned_rendered`, `valid_from` row clarifies "Entity pages always `""`".

### F6 — Doc 05 §12 + doc 07 §6.4 kind enum aspirational ✅ RESOLVED

**Doc 05 §12.1-12.4 + doc 07 §6.4 (pre-F6)**: `kind=llm_call_failed | parse_failed | validation_failed | round_trip_failed`.

**Reality**: `DreamApplyFailureKind` enum shipped with values `validation | patch_runtime | round_trip | io`. Quarantine logic in `dream_quarantine.STRUCTURAL_FAILURE_KINDS = {VALIDATION, PATCH_RUNTIME, ROUND_TRIP}`. LLM call failures NEVER emit `memory.dream.entity_failed` (they bubble up upstream from the consolidator). The TypedDict docstring also claimed `parse_failed`/`llm_call_failed` which are never emitted.

**Decision**: doc → code. Document the 4 real values + clarify that LLM failures are ambient/upstream + fix the TypedDict docstring.

**Resolution**:
- Doc 05 §12.1: LLM call failure marked as upstream-of-apply (runner tally, not this event).
- Doc 05 §12.2: parse failure → patch_runtime (broader category covering parse + runtime errors).
- Doc 05 §12.3: `validation_failed` → `validation`.
- Doc 05 §12.4: `round_trip_failed` → `round_trip`.
- Doc 05 §12.4a: new — `io` failure category (disk write).
- Doc 05 §12.5: STRUCTURAL_FAILURE_KINDS set = `{validation, patch_runtime, round_trip}`; ambient = `io` + upstream LLM.
- Doc 05 §14 row 12 updated to the real enum.
- Doc 07 §6.4 rewritten: real fields (`entity_ref`, `trigger`, `kind`, `error_message`, `failure_count_now`, optional `quarantined_until`); structural vs ambient taxonomy explained; note discarding the aspirational kinds.
- `MemoryDreamEntityFailedEvent` TypedDict docstring corrected to reflect that only the 4 enum values are emitted.

### F7 — Dream prompt slots silently empty ✅ RESOLVED

**dream.py:731-734 (pre-F7)**: `existing_attribute_keys=()`, `existing_relation_types=()`, `existing_uris=()`, `recent_history=""` passed as empty. The original comment said "Phase 1 deliverables 9 and 10 will populate" — those never landed.

**Pre-F7 impact**: the Dream LLM ran schema-blind to the existing entity. If the page had `attributes: {e-mail: ...}` and the LLM proposed `attributes.email`, there was no hint to reuse. Schema drift was documented but invisible to the LLM.

**"Good system" analysis**:
- `existing_schema` (attributes + relations): prevents schema drift. Critical for long-term coherence.
- `recent_history`: lets the LLM see its own past decisions; avoids undoing them.
- `existing_uris`: prevents duplicate entity creation (same person registered as `person:marcelo` and `person:marcelo_marmol`).

**Decision**: wire 3 of 4 slots. Defer `existing_uris` (the producer is more complex: walk + sort by mtime + cap).

**Resolution**:
- `dream.py` top-level import of `format_recent_history`.
- `DreamConsolidator._build_prompt`: parses `EntityPage.from_text(current_page)` to extract `attributes.keys()` and `relations.type` set. Calls `format_recent_history(workspace, entity_ref)`. Failures swallowed with a warning log.
- TDD: 4 cases (attribute_keys populated vs `(none)`, relation_types populated vs `(none)`, format_recent_history called once, first-consolidation gracefully empty).
- Doc 05 §5.1 row `existing_schema` updated to "derived via EntityPage.from_text (F7)".
- Doc 05 §5.1 row `existing_uris` marked deferred.
- Doc 05 §5.1 row `recent_history` updates producer.
- Doc 06 §2 inline annotations on each affected slot.

### F8 — Doc 07 §6.5 `memory.dream.patch_applied` field names ✅ RESOLVED

**Pre-F8**: doc listed `entity_uri`, `op_count`, `body_delta_chars`, `commit_sha`, `cursor_advanced_to`. Only `body_delta_chars` matched the code.

**Reality** (`dream_apply._emit_apply_telemetry` + `MemoryDreamPatchAppliedEvent`): `entity_ref`, `trigger`, `ops_applied`, `sources_count`, `body_delta_chars`, `cursor_after`, `duration_ms`.

**Decision**: doc → code. The pre-F8 spec was aspirational with field names that never reached production. `commit_sha` is deliberately dropped (telemetry should not couple to git internals; dashboards join on `entity_ref + cursor_after`).

**Resolution**: doc 07 §6.5 rewritten with the 7 real fields + explicit note about `commit_sha` deferred-by-design.

**G8 correction (2026-05-28)**: F8 framed the `commit_sha` drop as "dashboards join via `entity_ref + cursor_after`" — that argument was fragile (the join requires parsing commit-message trailers and breaks when two entity touches share a cursor in the same Dream pass). The conclusion (don't emit the field) is still correct, but for a different reason: the realistic consumers (operator forensics, audit) run `git log memory/entities/<type>/<slug>.md` directly because the file path is known from `entity_ref` and the commit trailers per doc 05 §6 carry `Trigger`, `Sources`, and `Cursor-after`. The only consumer that would genuinely benefit from `commit_sha` emission is a debug dashboard tracking commit latency at scale — no such consumer exists in code or in any written operational ask. If one materialises, the cheap path is a NEW event `memory.dream.commit_recorded` fired from `dream.py::apply()` after `repo.commit(...)` returns; it joins to `memory.dream.patch_applied` on `(session_key, iteration, entity_ref)`. The implementation cost when triggered is roughly 30-50 LOC (event TypedDict + emission site + telemetry test) — far less than the F8 alternative of restructuring `_emit_apply_telemetry` to fire post-commit. Doc 08 §2.16 carries the full reasoning so the next audit does not silently re-propose the field.

### F10 — Doc 07 §9.2 `memory.index.rebuild` field names ✅ RESOLVED

**Pre-F10**: doc listed `entities_count`, `embedding_batches`, `duration_ms`, `prior_index_existed`.

**Reality** (`indexer._emit_rebuild` + `MemoryIndexRebuildEvent`): `target`, `indexed`, `errors`, `duration_ms`, optional `reason`.

**Decision**: doc → code.

**Resolution**: doc 07 §9.2 rewritten with the real shape + explanation per field. `target` clarifies that today it is always `"fts"` (future: `lancedb`, `all`).

### F11 — Doc 07 §9.3 `memory.index.staleness_detected` field names ✅ RESOLVED

**Pre-F11**: doc listed `uri`, `delta_seconds`, `action`.

**Reality** (`indexer._emit_staleness` + `MemoryIndexStalenessDetectedEvent`): `uri`, `reason` with values `missing_row | mtime_lag | row_for_missing_file`.

**Decision**: doc → code. `delta_seconds` and `action` were aspirational — the cron always re-derives (action single-valued = meaningless), and the time delta is implicit in the join with the corresponding `memory.index.write` event a few seconds later.

**Resolution**: doc 07 §9.3 rewritten + note discarding the 2 aspirational fields with rationale.

**G3 correction (2026-05-28)**: F11 dropped `delta_seconds` claiming "the delta is implicit in the join with `memory.index.write` posterior". That justification was technically wrong: `staleness_detected@T1 → write@T2` gives **recovery latency** (T2 − T1), not **staleness magnitude** (T1 − indexed_mtime) — two different metrics. Without the magnitude, an operator can only count staleness events but not graph p50/p95 of how far behind the watcher fell. G3 ships `delta_seconds` as `NotRequired[float]` set only on `reason='mtime_lag'` events (the other two reasons have no `indexed_mtime` to compare against). `action` stays dropped (single-valued, meaningless). TDD: 4 cases in `tests/memory/test_staleness_delta_seconds_g3.py`.

Pre-existing bug discovered while writing the test: `detect_index_staleness` uses `md.stem` to derive URIs for entries, but the indexer stores `memory/<class>/<id>` with the class prefix. The two don't match, so `mtime_lag` never fires for entries — only `row_for_missing_file` and `missing_row` ghost events from the mismatch. Entity pages (`<type>:<slug>`) match correctly. Out of scope for G3; tracked separately.

### F12 — `compose_embedding_text` single source of truth ✅ RESOLVED

**Doc 02 §4 (pre-F12)**: "**Single source of truth: `vector_index.py::compose_embedding_text(...)`**".

**Pre-F12 reality**: no such function existed. Two specialised composers:
- `_compose_entity_page_text(name, aliases, body, attributes?, relations?)` for EntityPage.
- `_embed_text(entry)` for MemoryEntry.

**Decision**: code → doc. Create the real public dispatcher that delegates to the correct specialist by type. The doc claim stops being aspirational.

**Resolution**:
- `VectorIndex.compose_embedding_text(item, ...)` added as a public `@classmethod`: routes EntityPage → `_compose_entity_page_text`, MemoryEntry → `_embed_text`, raises TypeError on unsupported input.
- The two specialists remain as implementation details (still accessible, not removed).
- Doc 02 §4 updated: the "Single source of truth" claim is now literally true.

### F13 — Doc 02 §11 schema_version 3 vs 4 ✅ RESOLVED

**Doc 02 §11 (pre-F13)**: `CURRENT_SCHEMA_VERSION (3 as of A4)`.

**Reality**: `index_meta.py:55` says `CURRENT_SCHEMA_VERSION = 4`. E9 bumped it when entity page composition gained `rendered_frontmatter`.

**Resolution**: doc 02 §11 row updated to `(4 as of audit E9 / F13 verification, 2026-05-28; bumped from 3 when entity-page composition gained rendered_frontmatter)`.

### F14 — Doc 03 §2.1 scope enum + grep coverage drift ✅ RESOLVED

**Doc 03 §2.1 (pre-F14)**: scope enum `dreamed|undreamed|all`; grep fallback note says "raw session/ingested" only.

**Reality**:
- F2 added `archive` to the enum.
- `_safe_grep_fallback` (search_pipeline.py:472-479) covers `memory/` + `sessions/` + `ingested/` to capture memory entries written outside the tool layer (tests, scripts).

**Resolution**: doc 03 §2.1 scope row updates the enum to `dreamed|undreamed|all|archive` + note that grep also covers `memory/` for entries-written-outside-tool.

### F15 — Doc 04 §5.3 memory_drill description divergence ✅ RESOLVED

**Doc 04 §5.3 (pre-F15)**: three paragraphs; "For related context (recent post-cursor observations mentioning this URI)..." + "This tool is read-only. It does not modify state.".

**Reality** (`memory_drill.py::_PARAMETERS["description"]`): two paragraphs; "This tool is read-only. For related context about an entity (recent observations, sessions mentioning it), use memory_search with the entity's name or URI as the query instead."

**Resolution**: doc 04 §5.3 rewritten verbatim from the shipped string + audit F15 note + clarifies canonical source.

### F16 — Doc 05 §6 step 9 `.md.bak` ordering ✅ RESOLVED

**Doc 05 §6 (pre-F16)**: step 8 = "Write to temp file + atomic rename"; step 9 = "Pre-write: copy the target to .md.bak". Step 9 happened AFTER the write — intra-doc contradiction (cannot be "pre-write" after the write).

**Reality** (`dream_apply.py:165-168`): the copy to `.md.bak` happens BEFORE any mutation. The doc's step 9 had inverted order.

**Resolution**: doc 05 §6 reordered: step 4 = copy `.md.bak` (pre-write); steps 5-9 = apply + render + validate + write; step 10 = round-trip check + restore from bak on failure; step 11 = delete bak + commit. Note references `dream_apply.py:165-168` for verifiability.

### F17 — `existing_uris` slot wired (Dream prompt) ✅ RESOLVED

**Pre-F17**: doc 06 §2 promised `{existing_uris}` recent-mtime ranked + 100-cap to prevent duplicate entity creation. `dream.py:733` passed `existing_uris=()`. F7 deferred wiring. The Dream LLM was creating duplicates (`person:marcelo_marmol` when `person:marcelo` already existed) without any workspace state signal.

**Decision**: implement the real producer.

**Resolution**:
- New module `durin/memory/entity_inventory.py` with `existing_uris_by_recent_mtime(workspace, *, cap=100)`.
- Walks `memory/entities/<type>/<slug>.md` excluding archive (top-level + legacy nested).
- Sorts by file mtime descending; default cap 100.
- `DreamConsolidator._build_prompt` replaces `existing_uris=()` with the producer call (try/except swallows failures → preserves dream resilience).
- TDD tests: 7 cases (empty workspace, collects URIs, recent-mtime sort, caps at 100, custom cap, excludes both archive variants, end-to-end via prompt builder).
- Doc 05 §5.1 + doc 06 §2 updated with producer reference.

**G4 correction (2026-05-28)**: F17 closed with the note "Default cap of 100 lives at `DEFAULT_EXISTING_URIS_CAP` and is hard-coded; lifting it into config is straightforward if operators with very large workspaces ask." Same `feedback_stop_soft_deferrals` filter applies: no telemetry detects "duplicate created because cap was too low", so an operator has no observable trigger to file the ask. Plus, there are TWO caps in series (`entity_inventory.DEFAULT_EXISTING_URIS_CAP` and `dream_prompt_builder._EXISTING_URIS_CAP`) so lifting one silently leaves the other in effect. Classified as **discarded** in doc 08 §2.13; doc 06 §2 note rewritten to say "explicitly decided NOT to lift" rather than "straightforward if asked". The cap stays at 100; the operator who truly needs to tune can patch the constant (one line).

### F18 — Doc 07 §6.1 trigger enum missing `post_ingest_threshold` ✅ RESOLVED

**Doc 07 §6.1 (pre-F18)**: trigger enum `threshold | cron_daily | post_compaction | session_close | manual`. §6.2 (dream.end) already included `post_ingest_threshold`.

**Reality**: `threshold_trigger.py:12-13` emits both in `memory.dream.start`.

**Resolution**: doc 07 §6.1 enum extended to `threshold | post_ingest_threshold | cron_daily | post_compaction | session_close | manual` + cross-ref to §6.2.

### F19 — Doc 07 alarm threshold contradiction ($1.50 vs $5/day) ✅ RESOLVED

**Pre-F19**: §10.2 "healthy range < $1.50 (alerting threshold)" vs §11 "Dream LLM cost > $5/day | error". Apparent contradiction.

**Cross-doc analysis**: doc 09 §11.1 target soak $0.25-$1.50/day; doc 09 §13 alerting $1.50; doc 08 §3 R3 alarm $5/day. The values describe a coherent two-tier alarm (warn $1.50, error $5).

**Decision**: reconcile §10.2 and §11 as explicit two-tier.

**Resolution**:
- §10.2 row updated to "target $0.25-$1.50/day; warn at $1.50 (F19), error at $5".
- §11 alerts table: new warn row `> $1.50/day`; existing error row `> $5/day` preserved.

### F20 — `iteration`/`session_key` auto-injection wired ✅ RESOLVED

**Doc 07 §4.1 (pre-F20)**: "auto-injected by `emit_tool_event`".

**Pre-F20 reality**: aspirational claim; code in `_telemetry.py` did not inject anything. Dashboards joining `memory.recall` to other events on `(session_key, iteration)` had no data to join on.

**Decision**: implement the real auto-injection.

**Resolution**:
- `TelemetryLogger.__init__(path, *, session_key="")` now accepts session_key.
- Properties `session_key`, `iteration` + `set_iteration(int)` method.
- `get_session_logger(session_key, ...)` passes session_key to the constructor.
- `emit_tool_event` reads `logger.session_key` and `logger.iteration` via `getattr` with default (test mocks without the attributes keep working), auto-injects if not already present in the payload. Caller-supplied values win (subagent override).
- New `AgentLoop._on_iteration(iteration)` callback: setattr `_current_iteration` + `current_telemetry().set_iteration(iteration)`. Replaces the previous lambda in the runner setup.
- TDD tests: 6 cases (session_key stamped, iteration starts 0, set_iteration updates, auto-inject, caller-supplied wins, no-logger no-crash).
- Doc 07 §4.1 row updated to flag F20 wired.

### F21 — Doc 03 §15 hardcoded knobs line refs stale ✅ RESOLVED

**Pre-F21**: `vector_top_k @ search_pipeline.py:347`, `lexical_top_k @ :362`, `rrf_constant @ rrf_fusion.py:38`. Real lines after refactors: `:444`, `:459`.

**Resolution**: table updated with verified line numbers OR symbols (`DEFAULT_K`, `DEFAULT_W_*`, `DEFAULT_MAX_PER_SOURCE`) that survive refactors.

### F22 — Doc 02 §4.2 `to_name_resolved` claim vs slug-only ✅ RESOLVED

**Pre-F22**: doc said relations render as `<type.title()>: <to_name_resolved>` with "name of the target entity if known".

**Reality** (`vector_index.py:231`): only strips the type prefix; does not resolve the name.

**Resolution**: row corrected; clarifies that the slug is used; alias-index resolution deferred until bench shows a recall gap on relation queries.

**G5 correction (2026-05-28)**: F22's defer wording "until bench shows a recall gap on relation queries" was vague — same shape as the soft deferrals G2/G4 corrected, but here there IS a real bench (Phase 8 LoCoMo) on a dated roadmap. The defer is legitimate; the wording was loose. G5 tightened both ends:

1. **Trigger written concretely**, so a future audit can check it without re-litigating: Phase 8 LoCoMo bench reports **≥ 2 pp lower recall** on the slice of questions whose gold answer hinges on a relation target's full name, AND the per-failure trace shows the missing token IS the target's `name` field (not some other failure mode). The "AND" matters — if the regression is FTS tokenisation or decay, alias resolution would not fix it.

2. **Counterfactual written**, so we close the question if the trigger does NOT fire: if Phase 8 shows the relation-target slice at or above the bench mean, slug-only is empirically validated and the open question is closed (moved from "deferred" to "decided against, evidence in Phase 8 results").

3. **Doc 02 §4.2** now has a multi-paragraph block "Why slug-only and not the target's resolved name" covering: why current behaviour might already be sufficient (slug == name in most workspaces), why we did not ship it preventively (~5ms extra reads + implementation surface), the trigger + counterfactual verbatim, and the estimated cost when triggered (~80 LOC + reindex).

4. **Doc 09 §11.1** adds the relation-target recall slice as an explicit Phase 8 deliverable so the trigger has a place to land.

Lesson recorded in `feedback_stop_soft_deferrals` already covers this; G5 is the worked example of the tight form (trigger + counterfactual + cost) when a defer IS legitimate.

### G6 — `memory_drill` could not resolve entity-page or archive-scope URIs ✅ RESOLVED

**Discovered while investigating Item 6** (summary slot in entity-page embedding). The user asked whether entity pages were drill candidates and whether the 1500-char embedding budget was a problem. Investigating revealed two unrelated drill bugs that mattered far more than the summary slot.

**Bug 1 — entity pages**: `memory_search` emits URI `memory/entity_page/<type>:<slug>` for every canonical hit (`memory_search.py:720-722`). On-disk file lives at `memory/entities/<type>/<slug>.md`. `drill()` resolved the URI literally and returned `file not found` for every canonical hit. The hot-path flow `agent → memory_search → drill` was broken for entity pages — the primary canonical output of the memory system.

**Bug 2 — archive scope (F2 regression)**: `_run_archive_scope` (`memory_search.py:606`) emitted `uri = front.get('uri', '') or path.stem` — a bare id like `arch1` with no path prefix. `drill()` could not resolve any archive hit. The archive recovery surface F2 shipped was forensically incomplete: agent could see snippets but not pull the full archived body when the snippet was insufficient.

Other URI shapes (`memory/<class>/<id>` for episodic/stable/corpus/pending, `sessions/<key>.md#anchor`, `ingested/<id>/source.md#anchor`) all resolved correctly. Bugs were isolated to these two shapes.

**Resolution**:

- `drill.py` gains `_translate_entity_page_uri(path_part)` — pure URI-shape mapping from `memory/entity_page/<type>:<slug>(.md)?` to `memory/entities/<type>/<slug>.md`. Non-matching paths pass through unchanged. Error message now surfaces the original URI so the agent can debug a canonical lookup that missed.
- `_run_archive_scope` in `memory_search.py` emits the relative path under the workspace (`path.relative_to(self._workspace).as_posix()`) for archive hits, replacing the bare id. Both archived entries (`memory/archive/<class>/<id>.md`) and archived entity pages (`memory/archive/entities/<type>/<slug>.md`) now produce drillable URIs.
- TDD: 6 cases in `tests/memory/test_drill_uri_resolution_g6.py` (entity canonical URI resolves, `.md` suffix variant tolerated, legacy on-disk path still works, missing entity returns clear error with original URI, archive scope entry drillable, archive scope entity-page drillable).

**Resolution of Item 6 (summary slot) after G6**: with drill fixed for entity pages, the original concern "long-body entity pages where match is beyond 1500 chars" splits as follows. The vector path is the only one limited by the 1500-char budget; FTS5 (doc 02 §5.2 "BM25 text truncation: None") indexes the full body, and the grep fallback reads the full file from disk. So a query whose match lives at char 7000 of a 10000-char entity page is found by lexical/grep; the canonical surfaces in the result set; the agent receives the URI; G6 makes that URI drillable; the agent pulls the full body via drill. The summary slot would only help the vector path — and only when the embedding model could not retrieve via headline+aliases+frontmatter alone. With three retrieval paths reaching the page and drill resolving the URI, the summary slot is materially less important than the E9 defer note implied. The slot stays unimplemented; the data-model change (adding `summary` to `EntityPage`) and Dream prompt work that shipping it would require are not justified by the marginal vector-only benefit. The E9 defer is reclassified as "decided against" — same reasoning as G4 (no failure mode that would empirically produce the ask, given the other paths cover retrieval and drill closes the body-recovery loop).

### G7 — Shared marker convention for hot_layer and sectioned_output ✅ RESOLVED

**Context**: F4 (2026-05-28) unified the two search-side renderers (`Result.render_block` → `sectioned_output._render_block`) but left `hot_layer._render_canonical_block` as a third renderer with the F4 wording "deferred — different use case". G7 applies the `feedback_stop_soft_deferrals` filter and splits the question in two.

**A — Full unification of the two renderers**: decided against. The two renderers serve different consumers (eager pre-injection vs lazy search-result rendering), take different inputs (`EntityPage` dataclass vs `SectionedHit` row), and emit different inner content (structured attributes/relations lines vs summary > body > snippet preference). Forcing one into the other's shape produces a regression in both directions. See doc 08 §2.15 for the full reasoning, the table comparing the two renderers, and the estimated cost of doing the work if a future trigger appears (~150-200 LOC + doubled test surface).

**B — Shared marker convention**: shipped. The `=== KIND: <ref> ===` / `=== END KIND ===` strings move to `durin/memory/section_markers.py` (canonical_marker, fragment_marker, session_marker, ingested_marker, end_marker). Both `sectioned_output._marker_for` and `hot_layer._render_canonical_block`/`_render_fragment_block` call the shared helpers. The renderers' body composition stays distinct; only the marker strings have a single source of truth. ~20 LOC of helper + 11 TDD cases in `tests/memory/test_section_markers_g7.py`. Eliminates the drift surface without merging the renderers.

**Combined effect**: the F4 deferred bullet "hot_layer._render_canonical_block — different use case, not in F4 scope" is now correctly classified as "decided against full unification" (doc 08 §2.15) AND "marker convention shared via section_markers" (G7 ship). The deferred item closes.

### F23 — Doc 02 §3.1 summary format ✅ RESOLVED

**Pre-F23**: doc said `name (also: alias1, alias2)`.

**Reality** (`vector_index.py:142, 516`): `name (alias1, alias2)` — no "also:".

**Resolution**: row corrected with the real format.

### F2 — `scope='archive'` + CLI archive commands ✅ RESOLVED (partial)

**Doc 01 §3.6 + doc 04 §11 (pre-F2)**: promised 3 recovery surfaces:
1. `memory_search(scope='archive')` walks `memory/archive/` on demand.
2. `durin archive show <uri>` reads an archived entry.
3. `durin archive list` enumerates the archive folder.

**Pre-F2 reality**:
1. `scope` enum was `["all", "dreamed", "undreamed"]`; `'archive'` rejected at `memory_search.py:315`.
2. CLI had 10 commands (`reindex`, `dream`, `history`, `show`, `diff`, `revert`, `expand`, `absorb`, `stats`, `absorb-suggest`); none archive-prefixed.
3. `durin memory expand <entity>` already covered the archive of a SINGLE entity; file access via `cat memory/archive/...` covered direct lookups.

**"Good system" analysis**:
- `scope='archive'` is the **highest-value** surface for an LLM-in-the-loop assistant: the agent can recover archived content without the operator doing manual grep. Covers the "find what you said 3 months ago about X" case.
- CLI commands are operator-debugging convenience; file access + `memory expand` already cover the minimum viable surface. Without a concrete case, they are speculative construction.

**Decision**: Option C (hybrid). Ship `scope='archive'`, defer both CLI commands.

**Resolution**:
- Enum extended to `["all", "dreamed", "undreamed", "archive"]`.
- `_run_archive_scope(query, limit)` added: walks `memory/archive/**`, parses YAML frontmatter via `split_frontmatter`, substring match over `headline+summary+name+aliases+body`. No decay, no rerank, no cross-encoder (recovery surface, not a hot path).
- Emits `memory.recall` event with `scope='archive'` + `strategy='archive'` so dashboards can tell them apart.
- TDD tests: 6 cases (`scope='archive'` accepted, finds archived episodic, finds archived entity, empty when no archive dir, does NOT include active memory, respects limit).
- Doc 01 §3.6 + §10 row 4 mark F2 shipped.
- Doc 04 §11 listed the CLI commands as deferred with strikethrough (later corrected by G2, see below).
- Doc 08 §5 backlog: entry added with trigger "concrete operator workflow" (later moved to §2.12 discarded by G2).

**Commit pending** (F1-F11 batch close).

### G2 — Correct F2 CLI defer to "decided against" ✅ RESOLVED

**Context**: F2 framed `durin archive show / list` as "deferred until concrete operator workflow surfaces". The user (2026-05-28) pointed out this is a recidivist pattern of soft deferral — wording that effectively kept the items as a phantom to-do because no failure mode would produce that workflow. The items belong in §2 (discarded) not §5 (deferred backlog).

**Reality**: three existing surfaces cover archive recovery:
1. `memory_search(scope='archive')` — agent-visible semantic recovery (audit F2 shipped this).
2. `durin memory expand <entity>` — per-entity rendering of canonical + archived predecessors.
3. `cat memory/archive/<class>/<id>.md` + `find memory/archive -name '*.md'` — direct shell access.

A dedicated `durin archive show / list` would duplicate these without a unique use case.

**Decision**: discarded, not deferred.

**Resolution**:
- Doc 08 entry moved from §5 backlog to §2.12 discarded with explicit "what was proposed", "why we are not implementing", "concrete trigger that would change this", "lesson".
- Doc 04 §11 strikethrough replaced with "Not implemented — covered by …".
- Doc 01 §3.6 + §10 row 4 point at the three existing surfaces with explicit "decided against" note.
- F2 entry in this doc (above) annotated with the G2 correction.
- Personal memory lesson `feedback_stop_soft_deferrals` recorded: "deferred until concrete trigger" without a written failure mode is the same as discarded — except it leaves a phantom to-do that returns each audit pass.

**Commit pending** (E16-E23 batch close).
