---
title: Prompts and instructions
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 04_agent_tools.md, 05_dream_cold_path.md
related: 07_telemetry_and_observability.md
---

# Prompts and instructions

This document specifies all the LLM-facing text the memory system produces: the agent's identity.md memory section, the tool descriptions, the Dream consolidator skill package, the absorb-judge prompt, the onboarding wizard text, and the structural marker conventions. **This document is the canonical text** — anything that mentions an LLM-facing string elsewhere must match what's specified here.

**Principle (P6 from overview):** structure communicates better than instructions. Markers and timestamps signal trust and recency. Imperatives like "treat as authoritative" or "USE BEFORE answering" are weak signals (see `feedback_tool_description_weak_signal.md`). Descriptive metadata wins.

---

## 1. Inventory

The system produces text consumed by LLMs in five places:

| # | Surface | Consumed by | Specified in |
|---|---|---|---|
| 1 | `identity.md` Memory section | Agent LLM at every turn (in system prompt) | §2 |
| 2 | Tool descriptions (`memory_search` / `_store` / `_ingest` / `_drill`) | Agent LLM at tool-selection time | §3 |
| 3 | Dream prompt package (multi-file assembly, NOT a skill) | Dream consolidator LLM at every consolidation call | §4 |
| 4 | Absorb-judge prompt | Dedup judge LLM | §5 |
| 5 | Onboarding wizard text (CLI + web dashboard) | Human operator at install + configuration | §6 |

Also documented here (consumed structurally, not as instruction text):

| # | Surface | Where it appears |
|---|---|---|
| 6 | Structural markers (CANONICAL/FRAGMENT/SESSION/INGESTED) | In `memory_search` results | §7 |

---

## 2. `identity.md` Memory section

`durin/templates/agent/identity.md` is the agent's persistent identity prompt. It contains a `## Memory` section that the agent reads every turn. Canonical text:

```markdown
## Memory

You have access to four memory tools (memory_search, memory_store,
memory_ingest, memory_drill). The memory system holds:

- **Canonical entity pages** — consolidated knowledge about people,
  projects, bugs, deals, files, etc.
- **Recent fragments** — atomic observations that haven't yet been
  consolidated. These may carry the latest state when it differs from
  the canonical page.
- **Session summaries** — distilled records of past conversations.
- **Ingested documents** — chunks of user-provided sources (PDFs,
  notes, articles).

When you might need a fact, call memory_search rather than answering
from cold recall. State the source of any fact you cite by referencing
the URI or section marker. Do not claim facts that are not in the
results.

If the canonical page and a recent fragment disagree, the fragment is
more current — explain the difference instead of choosing silently.

For compound or multi-part questions, issue 2-3 searches with different
phrasings rather than one long query. This consistently improves recall.
```

### 2.1 What this prompt does NOT say

- Does NOT say "always call memory_search before answering" (proven weak signal in tool descriptions).
- Does NOT contain imperatives without rationale ("trust X over Y").
- Does NOT enumerate every memory class with definitions — relies on the structural markers (§7) to communicate that information at result-time.

### 2.2 Verified effect

The v2 form of this section (with "don't answer from cold recall" + "state source") shipped 2026-05-25 and produced **+12pp on single_hop** and **+3.9pp net on LoCoMo bench** (`project_locomo_v2_prompts_result.md`). Declarative + specific worked; imperative + generic did not (D1/D3 prompts tested 2026-05 lost 20pp).

---

## 3. Tool descriptions

These are the **canonical descriptions** the LLM sees when deciding which tool to call. Match exactly the text in `04_agent_tools.md` §2.3, §3.3, §4.3, §5.3 (reproduced here for single-source).

### 3.1 `memory_search`

```
Search durin's memory for content relevant to your question. Searches across
canonical entity pages, recent observations, session summaries, and ingested
documents in one call.

Usage:
- For most queries, use a single call with a natural-language `query`.
- For multi-part questions, issue 2-3 calls with different phrasings rather
  than one long query.
- For literal-match queries (emails, IDs, URLs), pass the literal string in
  `keywords` in addition to a natural-language `query`. This biases the search
  toward exact matches.
- Use `level: "cold"` only when you need full body content (verbose; consumes
  many tokens). `warm` (default) returns headline + summary, enough for most
  tasks.
- `limit` defaults to 10. Reduce to 3-5 for chat-style short answers, raise
  to 20-30 for audit / investigative queries that need to see every relevant
  hit. Hard cap 50.

Results come pre-sectioned with structural markers:
- `=== CANONICAL: <uri> ===` — consolidated entity pages (durable knowledge)
- `=== FRAGMENT: <path> ===` — recent observations not yet consolidated
- `=== SESSION: <id> ===` — conversation summaries
- `=== INGESTED: <id> ===` — chunks of documents the user has loaded

When sources disagree, more recent fragments may reflect updates that have
not yet been consolidated into the canonical entity page. Use timestamps in
the markers to reason about recency.

State the source of any fact you cite (uri or section marker) in parentheses.
Do not claim facts that are not in the search results.
```

### 3.2 `memory_store`

```
Persist an observation to memory. Use this when you learn a fact the user is
likely to need again — preferences, decisions, facts about people/projects/
tasks, etc.

Storage class (default: episodic):
- `episodic`: working memory; short atomic observation. Most uses.
- `stable`: durable, identity-level. Use sparingly — only when the user has
  explicitly said "remember this" or the fact is clearly identity-level.
- `corpus`: chunks of inline reference text. For files on disk use
  memory_ingest instead — it preserves the original artifact and handles
  chunking.

Always populate `entities` with the URIs this observation mentions (format:
`<type>:<value>`, e.g., `person:marcelo`, `project:durin`). This enables
entity-aware retrieval later.

Keep `headline` short and specific — it can be omitted and the system will
auto-generate one from the first ~10 words of `content`. `content` is the
full body of the observation; don't truncate.

If the user is restating something already known, do NOT call this tool — it
creates duplicates. The Dream consolidation process will eventually fold
duplicates but in the meantime they pollute results. A near-duplicate
(cosine ≥ 0.95 of an existing entry) returns a warning instead of persisting;
pass `force=true` only when you intentionally want to re-affirm an existing
fact.
```

### 3.3 `memory_ingest`

```
Add a local document (markdown or plain text) to durin's memory corpus.
Use this when the user wants a file on disk remembered as reference
material — research notes, transcripts, technical specs, exported pages,
markdown books, etc.

`path` is the absolute or workspace-relative path to the file. The file
is copied to `ingested/<id>/` for preservation (so the original is
recoverable verbatim) and the content is chunked into searchable
`memory/corpus/*.md` entries. Re-ingesting the same file is idempotent
— the id is derived from a hash of (filename + content), so renaming
the file before re-ingesting produces a different id.

For web content, use `web_fetch(url=...)` first to get clean markdown,
then `memory_store(content=..., class_name="corpus", source_refs=[url])`.
`web_fetch` already handles URL extraction (Jina/readability),
SSRF protection, redirects, and image detection.

For short inline text (a paragraph or two), call `memory_store` directly
with `class_name="corpus"` — `memory_ingest` is specifically for files
on disk where preserving the original artifact matters.
```

### 3.4 `memory_drill`

```
Read the full content of a memory item by URI. Use this when memory_search
returned a hit and you need to see the full body, including any structured
data in the frontmatter.

This tool is read-only. For related context about an entity (recent
observations, sessions mentioning it), use memory_search with the entity's
name or URI as the query instead.
```

### 3.5 Synchronization requirement

The text above MUST match:
- `durin/agent/tools/memory_*.py::DESCRIPTION` constants.
- The MCP / OpenAI Tool format definitions exposed to the agent.
- Any documentation surfaced to the user explaining the tools.

Divergence is a bug. Updates flow from this document outward; never the other way.

---

## 4. Dream prompt package

Living at `durin/templates/dream/`. This is **the multi-file prompt assembly that the runner concatenates into a single prompt at every Dream consolidator call** — NOT a skill in the invocable-on-demand sense (the agent does not choose to load this; the runner builds and passes it). The package is intentionally larger (~1-2k tokens overhead per call) than a minimal prompt, in exchange for precision. JSON Patch is unforgiving syntax; without good few-shot examples, small models err.

**Terminology note:** the word "skill" elsewhere in durin refers to invocable artifacts the agent chooses to load (`~/.claude/skills/` style). The Dream prompt package is NOT one of those — it's a prompt template assembled by code. Keeping the distinction clear avoids confusion when readers cross-reference with `skill-creator` and similar tooling.

### 4.1 Layout

```
durin/templates/dream/
├── consolidator.md           # Main prompt — the contract, the input slots
├── json_patch_reference.md   # How to write JSON Patch ops correctly
├── rules.md                  # Long-form rules with rationale
├── commit_format.md          # Exact format for the COMMIT section
└── examples/
    ├── 01_new_entity.md      # Few-shot: creating a page from scratch
    ├── 02_update_attribute.md # Few-shot: replacing a value
    ├── 03_add_relation.md    # Few-shot: adding a relation
    ├── 04_handle_conflict.md # Few-shot: contradictory observations
    ├── 05_unify_keys.md      # Few-shot: detecting duplicate keys and merging
    └── 06_no_changes.md      # Few-shot: when entries don't warrant updates
```

### 4.2 `consolidator.md` — the main prompt

The structure (input slots in `{braces}`):

```markdown
You are durin's Dream consolidator. Process N new observations about
entity_id and update its canonical page.

ENTITY: {entity_id}

EXISTING PAGE (current canonical state):
{existing_page_content}

EXISTING SCHEMA for this entity (for coherence; not a constraint):
  attributes: {list_of_attribute_keys}
  relation types: {list_of_relation_types}

  Guidance:
  - PREFER reusing an existing key when the new info has the same semantic meaning.
  - If you notice two existing keys mean the same thing (e.g. 'email' and 'e-mail'),
    unify them in your output: emit ops that consolidate to one canonical key.
  - You MAY introduce new keys if the new information genuinely needs them.
  - The goal is coherent evolution, not rigid preservation.

EXISTING ENTITY URIs in workspace (consider for dedup; create new only if no match):
  {list_of_uris_truncated_to_100_by_recent_mtime}

SUGGESTED STARTER TYPES (for when you must create a new entity URI):
  person, place, project, topic, event, artifact, stance, practice
  (open vocabulary — you may use a different type if none of these fit;
   see `01_data_and_entities.md` §4.1.1 for examples per type)

RECENT GIT HISTORY for this entity (so you can avoid undoing recent updates):
  {recent_commits_with_short_diffs}

PENDING OBSERVATIONS ({n_entries}):
{entries_text}

Now follow the rules in `rules.md` and emit your output using the format
in `commit_format.md`. The output format is strict — see `json_patch_reference.md`
for syntax. Refer to `examples/` for sample outputs in different scenarios.

Begin output:
===PATCH===
```

### 4.3 `json_patch_reference.md`

Compact reference for JSON Patch (RFC 6902) ops the LLM might use:

```markdown
# JSON Patch operations reference

You emit JSON Patch ops over the entity page's frontmatter. Allowed ops:

## `add`
Adds a value at a path. Use for new attributes, new relations, new aliases.
```json
{"op": "add", "path": "/attributes/email", "value": "marcelo@mxhero.com",
 "provenance": "<source_entry_path>"}
```

For appending to a list, use `-` as the path index:
```json
{"op": "add", "path": "/relations/-", "value": {
  "to": "person:susana", "type": "spouse", "since": 2010
}, "provenance": "<source_entry_path>"}
```

## `replace`
Replaces a value at an existing path. Use when an attribute changes.
```json
{"op": "replace", "path": "/attributes/current_residence", "value": "Spain",
 "provenance": "<source_entry_path>"}
```

## `remove`
Removes a value. Use sparingly — only when an observation EXPLICITLY contradicts
existing data. Prefer adding `valid_until` or unifying instead.
```json
{"op": "remove", "path": "/attributes/old_role",
 "provenance": "<source_entry_path>"}
```

## Common pitfalls
- Always include `provenance` pointing to the source observation that justifies
  the op. Without it, the op will be rejected.
- Paths use `/` separators and JSON Pointer syntax. Spaces and special chars
  in keys must be escaped (`~1` for `/`, `~0` for `~`).
- You CANNOT touch paths outside `/attributes/*`, `/relations/*`, `/aliases/*`.
  Internal fields (dream_processed_through, created_at, updated_at) are
  managed by the runner and will be rejected if you target them.
- Order matters within an output: ops are applied sequentially. Don't
  reference a path created by a later op.
```

### 4.4 `rules.md`

Long-form rules with rationale (so the LLM can reason about edge cases):

```markdown
# Dream consolidator rules

## Rule 1 — Coherence over rigidity
Prefer existing attribute keys and relation types when the new information
has the same semantic meaning. Don't invent `e-mail` if `email` is already
present. BUT: if you notice two existing keys mean the same thing, unify
them in this pass. Coherent evolution, not preservation.

## Rule 2 — Single entity per pass
Your task is to update ONE entity (the `entity_id` in the prompt). If a
pending observation mentions a different entity, do NOT include it in this
pass's PATCH. It will be processed in its own pass.

## Rule 3 — Provenance is non-negotiable
Every PATCH op must include a `provenance` field pointing to the source
entry (the `id` or path of an observation that justified this op). Without
provenance, the op will be rejected by the apply pipeline.

## Rule 4 — Preserve by default
Do NOT remove attributes or relations unless an observation EXPLICITLY
contradicts them. When in doubt:
- Append history via `valid_from` / `valid_until` instead of overwriting.
- Add the new fact alongside the existing one, with notes in the body.
- Emit a `remove` op only if the observation says "this is no longer true".

## Rule 5 — Respect recent decisions
The RECENT GIT HISTORY section shows commits in the last 30 days. If a
recent commit updated something, be cautious about reverting that update
based on older observations. Newer evidence wins; older evidence enriches.

## Rule 6 — Body delta is for prose, not data
The `===BODY_DELTA===` section is appended to the entity's narrative body.
Use it for prose context that doesn't fit attributes/relations (relationships
between facts, anecdotes, context, etc.). Leave empty if no body change.

## Rule 7 — Commit message is the audit trail
The `===COMMIT===` section becomes a git commit message. Format per
`commit_format.md`. Subject ≤ 70 chars; trailers required per the format.
The body of the message should explain non-obvious decisions you made
(why you used `replace` vs `add`, why you didn't merge two keys, etc.).
```

### 4.5 `commit_format.md`

```markdown
# Commit message format

Your COMMIT section becomes a git commit message. Format:

```
<subject, max 70 chars>

<optional body, explaining non-obvious decisions>

Sources: <comma-separated entry paths or IDs>
Cursor-after: <ISO timestamp of latest entry processed>
Entities-touched: <entity_id>
```

Note: `Trigger:` and `Run-id:` trailers are added by the runner automatically.
Don't include them in your output.

If you omit one of the LLM-supplied trailers (Sources / Cursor-after /
Entities-touched), the runner will fill them in from its state and log a
warning. Prefer to include them.

## Examples

Good:
```
Update Marcelo's email and add spouse relation

Two observations confirmed the email change from the May 23 conversation
and introduced the spouse relation from a 2010 episodic.

Sources: episodic/2026-05-23T10-12.md, episodic/2026-01-15T19-00.md
Cursor-after: 2026-05-23T10:12:00Z
Entities-touched: person:marcelo
```

Bad (missing trailers):
```
Updated email
```
```

### 4.6 Few-shot examples (`examples/`)

Each example file has the same shape:

```markdown
# Example: <scenario>

## Input
ENTITY: person:marcelo
EXISTING PAGE: ...
EXISTING SCHEMA: ...
PENDING OBSERVATIONS: ...

## Expected output
===PATCH===
[...]
===BODY_DELTA===
...
===COMMIT===
...
===END===

## Why this is the expected output
<reasoning>
```

Examples cover the cases that are likely to confuse small models:
- 01: First touch on a placeholder entity (build from scratch).
- 02: Email change (replace, not add).
- 03: Add a relation to an entity that already exists.
- 04: Two pending observations contradict each other — resolve via temporal validity.
- 05: Observed `email` and `e-mail` both as attributes; unify to `email`.
- 06: Pending observations don't add new facts (already in canonical) — emit empty PATCH + COMMIT explaining why.

---

## 5. Absorb-judge prompt

Lives at `durin/templates/dream/absorb_judge.md`. Active in current code. Specifies how the judge decides whether two entity pages with alias overlap describe the same real entity.

The prompt provides:
- Both entity pages (canonical candidate + absorbed candidate).
- Mtime of each (for staleness reasoning).
- The peer-review framing (judge is critic, not confirmer).

The LLM emits one of three verdicts (identity judgement, **not** action prescription — the runner maps verdict + confidence to the merge action per `05_dream_cold_path.md` §8.4-8.5):

| Verdict | Meaning |
|---|---|
| `same` | The two pages describe the same real entity |
| `different` | The two pages are distinct entities with overlapping aliases (homonymy, shared acronyms, generic placeholders) |
| `unclear` | Evidence is ambiguous; defer the call to a later pass |

Output envelope mirrors the consolidator's: `===VERDICT===` + `===CONFIDENCE===` + `===REASONING===` + `===END===`. The current implementation (`durin/memory/absorb_judge.py::judge`) is solid; this doc captures the contract.

---

## 6. Onboarding wizard text

The durin install wizard (`durin init` CLI command + the web onboarding flow) asks the operator a few questions. The memory-related questions:

### 6.1 Memory subsystem enable

```
durin's memory system lets the agent remember facts across sessions.
Enabling it downloads a small embedding model (~120MB) and starts the
local consolidation process.

Enable memory? [Y/n]:
```

Default: yes. If the operator declines, durin runs without memory (degraded but functional).

### 6.2 Cross-encoder reranker (opt-in)

```
Advanced retrieval option: durin can use a cross-encoder reranker to
improve search quality. This adds 300-1500ms latency per query (depending
on the model) and requires ~1GB additional RAM. The default model is
`jinaai/jina-reranker-v2-base-multilingual` (multilingual, covers 100+
languages including CJK).

Most users do NOT need this — the default search (without the reranker)
works well for typical workloads. Enable it later via the web dashboard
if you find queries returning poor results.

Enable advanced reranker now? [y/N]:
```

Default: no. Wording matches the trade-off discussion in `03_search_pipeline.md` §9.5.

### 6.3 Auto-absorb (entity dedup post-Dream, opt-in)

```
After Dream consolidates a batch of observations, it can optionally run an
LLM judge over entity pairs that share aliases (e.g., "Marcelo Marmol" and
"M. Marmol") and merge them when the judge is highly confident.

This is OFF by default because a bad merge silently combines two distinct
entities — recovery requires `git revert` in the memory repo. Enable only
when you trust the judge model and want to reduce manual cleanup.

Defaults when enabled:
  - Confidence threshold: 95/100 (high — favors precision)
  - Minimum age: 24h (prevents Dream from merging its own hallucinations)
  - Judge model: uses your Dream consolidator model

Enable auto-absorb now? [y/N]:
```

Default: no. Mirrors the conservative defaults in `durin/config/schema.py::AutoAbsorbConfig` and the rationale in `05_dream_cold_path.md` §8.

### 6.4 Aux model for memory tasks

```
durin's Dream process consolidates memory using an LLM. It runs in the
background, consuming ~$0.25-$1.00/day for an active workspace. You can
use the same model as your main agent, or a separate one for memory tasks.

Memory model: [same as agent / specify / skip]
```

Default: same as agent. Detailed model picker comes from `config/schema.py::AuxModelsConfig.memory`.

### 6.5 Web dashboard surfaces

The web dashboard (`webui/src/components/settings/SettingsView.tsx`) mirrors these questions plus exposes config that the CLI wizard skips:
- `memory.search.cross_encoder.model` — dropdown of supported models.
- `memory.search.temporal_decay` — read-only summary of current half-lives.
- `memory.consolidation.threshold_count` — number input.

The dashboard is post-install configuration; the CLI wizard is one-time setup. Both write to the same `~/.durin/config.json`.

---

## 7. Structural markers

Defined in `04_agent_tools.md` §6 and `03_search_pipeline.md` §12. Reproduced here as the canonical convention since the agent LLM parses them.

### 7.1 Four markers

| Marker pattern | Class |
|---|---|
| `=== CANONICAL: <uri> (consolidated <iso_ts>) ===` | entity pages |
| `=== FRAGMENT: <path> (ts <iso_ts>) ===` | post-cursor episodic + stable |
| `=== SESSION: <session_id>/<turn_or_summary> (ts <iso_ts>) ===` | session summaries + raw session hits |
| `=== INGESTED: <ingest_id>/<chunk_or_source> ===` | corpus + raw ingested |

### 7.2 What markers communicate (and what they don't)

| Marker conveys | Marker does NOT convey |
|---|---|
| Class of the content | "Trust this more" — agent infers from class+recency |
| URI for citation | "This is the answer" — agent reasons about content |
| Timestamp for recency | "Ignore older" — recency is signal, not rule |

Imperatives in markers (`(treat as authoritative)`, `(prefer this)`) are weak signals per `feedback_tool_description_weak_signal.md`. The agent reasons structurally; we don't tell it to trust.

### 7.3 Empty sections

Sections with zero hits are **omitted entirely**. No empty headers in output.

---

## 8. Hot layer pre-fetch

The hot layer is **memory eagerly injected into every agent prompt**, without any tool call. It is the always-on equivalent of `memory_search`: identity essentials, canonical entity pages, recent post-cursor fragments, top headlines, and a known-entities list, all assembled at prompt build time and rendered into the agent's stable prompt tier.

Implementation: `durin/memory/hot_layer.py` (Phase 1.9 of the memory subsystem).

### 8.1 Why it exists

Without the hot layer, the agent would have to invoke `memory_search` for every basic question ("what's my email?", "what was the user's last decision?"). That's slow, expensive in tool calls, and prone to "silent retrieval miss" — the agent answers from cold recall instead of querying.

The hot layer makes the most-frequently-needed memory always present. It encodes the §2.H contract from doc 25: canonical entity pages and recent post-cursor fragments **coexist in the prompt**, marked with structural markers so the LLM can reconcile temporal contradictions itself.

### 8.2 Composition and budgets

Five sections, in order, each with a hard char budget. Total ~1900 tokens — cache-friendly within a single day.

| Section | Char budget | Approx. tokens | Cap | Source |
|---|---|---|---|---|
| Identity | 800 | ~200 | n/a (single doc) | `memory/stable/IDENTITY.md` if present |
| Canonical pages | 2400 | ~600 | 12 | `memory/entities/**/*.md` sorted by `updated_at` desc |
| Recent fragments | 1200 | ~300 | 8 | Post-cursor episodic/stable entries, sorted by `valid_from` desc |
| Top headlines | 1200 | ~300 | 12 | Legacy class entries (episodic/stable/corpus) by `valid_from` desc |
| Known entities | 600 | ~150 | 50 | Deduplicated list of entity URIs |

If a section's content exceeds its budget, it is truncated entry-by-entry (full entries dropped, not partial entries). If a section is empty, it is omitted from the rendered output (no empty headers).

### 8.3 Section order and rendering

The hot layer renders in this fixed order with H2 markdown headings:

```
## Memory: Identity

<identity text>

## Memory: Canonical pages

These are the authoritative records — fragments below amend them with newer information.

=== CANONICAL: person:marcelo (consolidated 2026-05-20T...) ===
...

## Memory: Recent fragments (post-cursor)

Episodic entries not yet consolidated into a canonical page. Reconcile with the canonical above using the timestamps.

=== FRAGMENT: memory/episodic/2026-05-26T... (ts ...) ===
...

## Memory: Key Points

- <headline 1>
- <headline 2>
...

## Memory: Known Entities

person:marcelo, person:susana, project:durin, ...
```

The intro sentences ("These are the authoritative records...", "Reconcile with the canonical...") are part of the canonical hot-layer rendering — they cue the LLM to treat canonical as ground truth and fragments as recent amendments to reconcile by timestamp.

### 8.4 Cursor logic for fragments

A fragment qualifies for the hot layer if and only if it satisfies BOTH:

1. The entry's `valid_from` (or file `mtime`, as fallback) is **strictly after** the `dream_processed_through` cursor of the entity it tags.
2. The entry's class is `episodic` or `stable` (not `corpus`, not `pending`).

This means: as soon as Dream consolidates an episodic into an entity page, that episodic stops appearing in the hot layer (its `valid_from <= cursor` post-archive). The fragment slot is freed for newer post-cursor entries.

### 8.5 Refresh cadence

The hot layer is **re-read from disk on every prompt build** (call: `read_hot_layer(workspace)` in `hot_layer.py`). The disk reads are cheap: a few markdown files + entries directory walk + frontmatter parse, typically < 5ms total.

**Why re-read each build instead of caching in memory:** simplicity + correctness. The alternative (in-process cache invalidated on Dream completion) requires a cache-invalidation contract between Dream and the prompt builder; getting it wrong leaves the agent with stale memory. Re-reading on every build trades 5ms for zero cache-coherence bugs.

**The practical effect of "cheap re-read each build":** between Dream passes, the `.md` files don't change → the assembled hot layer is byte-identical across consecutive turns → the upstream prompt cache (Anthropic / OpenAI) stays warm. A Dream pass invalidates the cache for one turn; the cache rewarms on the next turn. So in practice, the hot layer changes at most once per Dream pass:

- Between Dream passes, the underlying `.md` files don't change → hot layer rendering is identical → upstream prompt cache stays warm.
- A Dream pass that consolidates entities → updates `dream_processed_through` cursors → fragment list shrinks → hot layer changes → cache miss for that one turn → cache warms again on the next turn.

This is why the budgets are set conservatively. Larger budgets would invalidate cache more often.

### 8.6 Relationship to tools

The hot layer is **eager**; `memory_search` is **lazy**. Together they cover the spectrum:

| Scenario | Hot layer covers? | Tools needed? |
|---|---|---|
| "What's the user's email?" | Yes — canonical `person:<owner>` page is always present | No |
| "What was the user's last decision?" | Yes — top of fragments | No |
| "Find anything you know about Acme Corp's renewal" | No — Acme may not be top-12 canonical | Yes — call `memory_search` |
| "What did we discuss last Tuesday?" | No — too specific | Yes — call `memory_search` |
| "Read the full content of person:marcelo" | Partial — summary in hot layer; full body needs drill | Yes — call `memory_drill` if cold-tier needed |

The hot layer biases toward **recall** for the most accessed records. Tools cover **precision** queries on demand.

### 8.7 Failure handling

If `read_hot_layer(workspace)` fails (disk error, parser error, missing files), the agent prompt is built without the hot layer section. Telemetry emits `memory.hot_layer.failure` with the error. The agent still works (with degraded recall) — search tools cover everything.

### 8.8 Module decisions

| # | Decision | Resolution |
|---|---|---|
| 1 | Hot layer is part of the stable prompt tier | Yes. Renders once per turn into the system prompt. NOT part of the search pipeline. |
| 2 | Budget = ~1900 tokens total | Cache-friendly between Dream passes. Larger budgets would invalidate prompt cache more often. |
| 3 | Section caps + char budgets | Per §8.2 table. Drop entries beyond cap; don't truncate mid-entry. |
| 4 | Marker convention | CANONICAL + FRAGMENT (per §7), same as `memory_search` result rendering. LLM treats both surfaces identically. |
| 5 | Fragment definition | Post-cursor episodic/stable. Corpus and pending excluded. |
| 6 | Refresh cadence | On every prompt build (cheap disk reads); changes at most once per Dream pass in practice. |
| 7 | Failure mode | Omit hot layer section; emit telemetry; agent continues with tool-only retrieval. |

---

## 9. Module-level decisions

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Source of truth for LLM-facing text | This document. Code constants + `templates/agent/identity.md` + templates/dream/* must match this doc verbatim. Divergence = bug. | §3.5 |
| 2 | Declarative not imperative phrasing | Validated by LoCoMo v2 (+3.9pp). "Don't answer cold" + "state source" + "issue 2-3 searches" worked; "USE BEFORE answering" did not. | §2.2 |
| 3 | Dream prompt package layout (NOT a skill) | Multi-file prompt assembly in `templates/dream/`: main prompt + reference + rules + commit format + 6 few-shot examples. Concatenated by the runner at call time. ~1-2k tokens overhead accepted. This is a prompt template package, not an invocable skill — the agent does not "choose" to load it; the runner builds it. | §4 |
| 4 | Few-shot examples for JSON Patch | Six examples covering common scenarios. Small models need concrete demonstrations of unfamiliar syntax. | §4.6 |
| 5 | `existing_schema` framing | For coherence, not constraint. LLM may add new keys or unify duplicates. Explicit guidance in the prompt. | §4.2 |
| 6 | Commit message format | LLM emits subject + body + 3 trailers (Sources, Cursor-after, Entities-touched). Runner adds Trigger + Run-id. Code verifies and auto-fills missing LLM trailers (warning, no block). | §4.5 |
| 7 | Onboarding default for cross-encoder | OFF (matches §9.5 of doc 03). Wording communicates trade-off. | §6.2 |
| 8 | Structural markers carry only metadata | No valuative language. Class + URI + timestamp only. | §7.2 |

### Open

None at the module level.

---

## 10. Implementation status

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| `identity.md` Memory section | v2 shipped 2026-05-25 (+3.9pp) | Light revision per §2 | Minor wording polish |
| Tool descriptions | Active in tools' `DESCRIPTION` constants | Sync to §3 canonical text | Reconcile any divergence |
| `templates/dream/consolidator.md` | v1 (page + commit) | v2 (skill package multi-file per §4) | Rewrite; create supporting files; update prompt builder |
| `templates/dream/absorb_judge.md` | Active | Same | None |
| Onboarding wizard text | Partial | Add §6 questions | Wizard CLI + webui changes |
| Structural markers | CANONICAL/FRAGMENT in code | + SESSION + INGESTED | Renderer extension |

---

## 11. Cross-references

- Tool specifications (params + return + behavior): `04_agent_tools.md`.
- Dream pipeline that consumes the skill package: `05_dream_cold_path.md` §5.
- Cross-encoder configuration that the wizard exposes: `03_search_pipeline.md` §9.5.
- Telemetry events from these LLM calls: `07_telemetry_and_observability.md` (pending).
- v2 identity.md result that validated the declarative style: `~/.claude/projects/.../memory/project_locomo_v2_prompts_result.md`.
