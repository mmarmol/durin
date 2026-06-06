---
title: Prompts and instructions
version: 0.2
status: current — describes the shipped system (post-migration, 2026-06-06)
last_updated: 2026-06-06
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 04_agent_tools.md, 05_dream_cold_path.md
related: 07_telemetry_and_observability.md, ../../qa/post_migration_audit_2026-06.md
---

# Prompts and instructions

This document specifies all the LLM-facing text the memory system produces: the agent's identity.md memory section, the tool descriptions, the Dream pass prompts (extract, absorb-judge, always-on rank, skill-extract), the onboarding wizard text, and the structural marker conventions. **This document is the canonical text** — anything that mentions an LLM-facing string elsewhere must match what's specified here.

**Principle (P6 from overview):** structure communicates better than instructions. Markers and timestamps signal trust and recency. Imperatives like "treat as authoritative" or "USE BEFORE answering" are weak signals (see `feedback_tool_description_weak_signal.md`). Descriptive metadata wins.

---

## 1. Inventory

The system produces text consumed by LLMs in five places:

| # | Surface | Consumed by | Specified in |
|---|---|---|---|
| 1 | `identity.md` Memory section | Agent LLM at every turn (in system prompt) | §2 |
| 2 | Tool descriptions (`memory_search` / `_store` / `_ingest` / `_drill`) | Agent LLM at tool-selection time | §3 |
| 3 | Dream pass prompts (extract, always-on rank, skill-extract sub-agent) | Dream LLM in the cold path | §4 |
| 4 | Absorb-judge prompt | Refine-pass dedup judge LLM | §5 |
| 5 | Onboarding wizard text (CLI + web dashboard) | Human operator at install + configuration | §6 |

Also documented here (consumed structurally, not as instruction text):

| # | Surface | Where it appears |
|---|---|---|
| 6 | Structural markers (CANONICAL/FRAGMENT/SESSION/INGESTED) | In `memory_search` results | §7 |

---

## 2. `identity.md` Memory sections

`durin/templates/agent/identity.md` is the agent's persistent identity prompt. It contains **two** memory sections that the agent reads every turn: `## Memory` (read-time guidance) and `## Memory writing` (write-time guidance, added in audit B11). Canonical text:

```markdown
## Memory

You have access to four memory tools (memory_search, memory_upsert_entity,
memory_ingest, memory_drill). The memory system holds:

- **Entity pages** — consolidated knowledge about a *thing* (a person,
  company, product, topic, project, place, …): its name, aliases, relations
  to other entities, the prose you wrote, and structured attributes the
  system extracts from that prose.
- **References** — coherent documents you ingested, kept whole (a tutorial,
  spec, article). Authoritative source material, not synthesized away.
- **Session summaries** — distilled records of past conversations.
- **Skills** — procedural memory: step-by-step procedures you follow
  for recurring tasks. A `skill` hit is an instruction set to
  **execute**, not a fact to cite.

When you might need a fact, call memory_search rather than answering
from cold recall. State the source of any fact you cite by referencing
the URI or section marker. Do not claim facts that are not in the
results.

For compound or multi-part questions, issue 2-3 searches with different
phrasings rather than one long query. This consistently improves recall.

## Working with search results

When you read the hits a memory tool returns:

- **Read every hit, not just the first.** A relevant fact may appear
  at the bottom — ranking is approximate.
- **Verify the entity.** Confirm each fact you cite is about the
  entity in the question. If a hit attributes something to a
  different person, project or topic, don't transfer it to the
  subject the user asked about.
- **Combine facts across hits.** When several hits describe the same
  topic, synthesise them — a single hit rarely carries the complete
  picture. For listing or counting questions, enumerate every
  distinct item before answering.
- **Don't reframe to fit the question.** If a source describes an
  event factually, present it factually. Don't add emotional,
  interpretive or evaluative language that isn't in the source — if
  memory says "joined a club", don't relabel it as "found his
  calling" or "transformative experience" unless those exact
  concepts appear.
- **Answer multi-part questions partially when needed.** For
  questions with multiple parts (X and Y), answer only the parts
  you have evidence for. Say explicitly when a part has no
  supporting evidence — never bridge unsupported parts by
  stretching the supported ones.
- **Never invent identifiers.** Names, titles, places and dates
  must come verbatim from a hit. When the specific detail asked
  for is missing, answer with what you DO have and name what's
  missing — don't guess the value.
```

Audit H7 (2026-05-29): the `## Working with search results` block distils three reasoning-quality guidelines extracted from mem0's published LoCoMo agent prompt, restricted to universal "how to work with retrieved hits" guidance — NOT task-disclosed prompting. The LoCoMo-Plus paper (arxiv 2602.10715) criticises task disclosure as "conditioning model behavior on task identity, encouraging task-specific response strategies instead of implicit recall from prior dialogue"; the rules above are about generation quality when consuming search results, applicable to any RAG-style call. The "never invent identifiers" sub-clause reinforces the existing "Do not claim facts that are not in the results" rule with stronger anti-hallucination wording on the high-risk identifiers (names, dates, places). The earlier 5-bullet draft included "prefer specific over generic" — dropped after the bench-29 analysis showed it could push toward an over-specific answer ("Painting") when the question expected the category ("abstract art").

```markdown
## Memory writing

Route by what the information IS:

- **A fact about a thing** (a person, company, product, topic, …) →
  `memory_upsert_entity`. Give the entity `ref` (`<type>:<slug>`), its
  display `name`, any `aliases`, `relations` to other entities, and prose
  `body` describing what you learned. The system extracts structured
  attributes from your prose — you don't write attributes yourself.
- **A document** the user gives you (a tutorial, spec, article) →
  `memory_ingest`. It's kept whole as a reference.
- **An interaction** — nothing to do; the conversation is already recorded
  and the system distils what matters.

Before authoring an entity, call `memory_search` first to see what you
already know, so you extend the existing entity instead of duplicating it.
```

### 2.1 What this prompt does NOT say

- Does NOT say "always call memory_search before answering" (proven weak signal in tool descriptions).
- Does NOT contain imperatives without rationale ("trust X over Y").
- Does NOT enumerate every memory class with definitions — relies on the structural markers (§7) to communicate that information at result-time.

### 2.2 Verified effect

The v2 form of this section (with "don't answer from cold recall" + "state source") shipped 2026-05-25 and produced **+3.9pp net on LoCoMo bench** (60.8% → 64.7%, `project_locomo_v2_prompts_result.md`). Audit E17 (2026-05-28) removed an earlier "+12pp on single_hop" claim — that per-category number was never verifiable from the bench data; only the overall +3.9pp is supported. Declarative + specific worked; imperative + generic did not (D1/D3 prompts tested 2026-05 lost 20pp).

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
- For exact phrase matching, wrap the phrase in double quotes inside `query` —
  e.g. `"shooting percentage" basketball` requires the two words to appear
  adjacent and in order, while `basketball` matches anywhere. Words outside
  quotes stay as loose tokens. An unbalanced quote is treated as a typo and
  discarded.
- Use `level: "cold"` only when you need full body content (verbose; consumes
  many tokens). `warm` (default) returns headline + summary, enough for most
  tasks.
- `limit` defaults to 10. Reduce to 3-5 for chat-style short answers, raise
  to 20-30 for audit / investigative queries that need to see every relevant
  hit. Hard cap 50.

Results come pre-sectioned with structural markers:
- `=== SKILL: <name> ===` — a matching procedure; these are steps to FOLLOW, not facts to cite
- `=== CANONICAL: <uri> ===` — consolidated entity pages (durable knowledge)
- `=== FRAGMENT: <path> ===` — recent observations not yet consolidated
- `=== SESSION: <id> ===` — conversation summaries
- `=== INGESTED: <id> ===` — chunks of documents the user has loaded

Each marker also carries a completeness qualifier:
- `(complete)` — the body shown IS the full entry; do NOT call memory_drill on this uri, it returns the same text.
- `(preview N/M)` — N chars shown, M chars exist; call memory_drill on this uri only if you need the remaining body.
Markers without a completeness qualifier are rare (legacy / lexical-only hits) — use judgment.

When sources disagree, more recent fragments may reflect updates that have
not yet been consolidated into the canonical entity page. Use timestamps in
the markers to reason about recency.

State the source of any fact you cite (uri or section marker) in parentheses.
Do not claim facts that are not in the search results.
```

### 3.2 `memory_store`

> **Disabled** (`MemoryStoreTool.enabled()` returns False) — not in the live
> toolset. The entity-centric model writes facts via `memory_upsert_entity`
> (§3.5) and documents via `memory_ingest` (§3.3). The description below is kept
> in sync (the tool still exists) but the LLM never sees it.

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
Add a local document (markdown or plain text) to durin's memory as a
REFERENCE — coherent source material the user wants kept whole:
research notes, transcripts, technical specs, exported pages, markdown
books, etc.

`path` is the absolute or workspace-relative path to the file. The
original is preserved verbatim and the document is indexed for
retrieval. Re-ingesting the same file is idempotent — the id is a hash
of (filename + content).

For web content, use `web_fetch(url=...)` first to get clean markdown,
then `memory_ingest` on the saved file. For a fact about a *thing* (a
person, company, product, topic…), use `memory_upsert_entity` instead —
`memory_ingest` is for whole documents, not individual facts.
```

### 3.4 `memory_drill`

```
Read the full content of one or more memory items by URI.

Pass either ``uri`` (single string) for one item, or ``uris`` (array, up
to 10) for multiple items in one round-trip. With ``uris`` the response
carries one ``{uri, content}`` record per request in the same order,
plus an ``error`` field on entries that failed — individual failures
don't abort the batch.

Use this ONLY when the corresponding memory_search result block is
marked ``preview N/M`` in its section header — N chars were shown, M
chars exist — i.e. more body is available beyond what you already have.
Drill in that case to fetch the rest.

Do NOT drill when the block is marked ``complete``: the search already
showed you the entire body and drill will return the same text, wasting
tokens and an LLM round-trip. Blocks without an explicit completeness
qualifier (rare; legacy / lexical-only hits) are best-guess — drill only
if the visible content seems truncated.

Prefer the ``uris`` form whenever 2+ URIs from one search all need
follow-up. Drill on URIs never expands the candidate set — use
memory_search to find new candidates.
```

Audit H9 (2026-05-29) consolidated the previous ``memory_drill_batch`` tool into ``memory_drill`` so the LLM sees one drill surface instead of two. The list-of-uris payload is identical to the old batch tool; the single-``uri`` legacy shape is preserved unchanged.

### 3.5 `memory_upsert_entity`

```
Author or update an entity (a person, company, product, topic, place, etc.) you have learned a fact about. Provide `ref` as `<type>:<slug>` (e.g. company:mxhero, person:marcelo), the display `name`, any `aliases`, `relations` to other entities ({to: '<type>:<slug>', type: 'partner'}), and prose `body` describing what you know. Merges into the existing entity if it exists, creates it otherwise. Do NOT pass structured attributes — the system extracts those from your prose. Use this for facts about a THING; use memory_ingest for documents.
```

This is the primary write tool in the entity-centric model: the agent authors a THING (person/company/product/topic) as prose; the dream extracts typed attributes from that prose later. Contrast `memory_store` (§3.2, disabled) which wrote raw entries.

### 3.6 `memory_forget`

```
Remove a memory entry you no longer want surfaced. Archives it to memory/archive/<class>/<id>.md (reversible) and removes its search index rows so it stops appearing in memory_search.

This is the ONLY correct way to delete a memory entry — never rm or move files under memory/ via shell, which leaves the search indices pointing at a missing file.

Pass `uri` exactly as memory_search returned it. Refuses entity pages (memory/entities/...): those have their own absorb/revert lifecycle.
```

### 3.7 Synchronization requirement

The text above MUST match the `.description` property on each tool class (`durin/agent/tools/memory_search.py::MemorySearchTool.description`, etc.). That property is the field `Tool.to_schema()` emits as `function.description` in the OpenAI function-calling spec — i.e. what the LLM actually reads when deciding to call the tool.

Each tool's `.description` property delegates to `_PARAMETERS["description"]`, so both fields stay identical with zero redundancy. The `_PARAMETERS["description"]` ends up as `function.parameters.description` (the JSON-Schema-level description of the parameters object), which most LLMs ignore — but keeping the two in lock-step means a future provider that does read it sees the canonical text.

Sync is enforced by `tests/memory/test_tool_description_sync.py`. Audit B1 (2026-05-28) fixed a real bug here: the prior version of this test compared `_PARAMETERS["description"]` (which the LLM ignores) instead of `.description` (which it reads). The two fields had drifted — the long canonical text lived in the ignored field, the short non-canonical text in the read field. Both now resolve to the same string and the test guards the LLM-visible surface.

Divergence is a bug. Updates flow from this document outward; never the other way.

---

## 4. Dream pass prompts

> **Migration note (2026-06-06).** The old `templates/dream/` **multi-file
> JSON-Patch consolidator package** — `consolidator.md` + `json_patch_reference.md`
> + `rules.md` + `commit_format.md` + an `examples/` bundle, where the LLM emitted
> JSON Patch (RFC 6902) ops over the entity page frontmatter — **was deleted**
> with the `DreamConsolidator`/`DreamRunner` cluster. The settled cold path
> (`05_dream_cold_path.md`, audit `../../qa/post_migration_audit_2026-06.md`) has **four passes**
> and **no JSON-Patch envelope, no `===PATCH===`/`===BODY_DELTA===`/`===COMMIT===`
> output, no `Cursor-after` trailer**. The only file left under `templates/dream/`
> is `absorb_judge.md` (§5). The four pass prompts are described below.

The Dream passes (`durin/memory/dream_passes.py` + `extract_dream.py` +
`always_on_dream.py`) build their prompts **in code**, not as a multi-file
template assembly — except the absorb-judge prompt (§5), which is the lone
surviving `templates/dream/` file. None of these is a "skill" in the
invocable-on-demand sense (the agent does not choose to load them; the cold path
builds and passes them).

| Pass | Prompt | Where | Output the LLM produces |
|---|---|---|---|
| **extract** | `build_extract_prompt` | `extract_dream.py` | a JSON object of `attribute_key → scalar/list` |
| **always_on** | `_RANK_PROMPT` | `always_on_dream.py` | feedback refs, one per line, best-first |
| **skill-extract** | `_SKILL_EXTRACT_PROMPT` (sub-agent system prompt) | `dream_passes.py` | `skill_write` tool calls (agentic) |
| **refine** | `absorb_judge.md` | template (§5) | `===VERDICT===`/`===CONFIDENCE===`/`===REASONING===` |

### 4.1 Extract prompt — sessions → entity attributes

`build_extract_prompt(page, turns)` (`durin/memory/extract_dream.py`) is the
write-side prompt. It reads a session's conversation turns and a single target
entity's current page, and asks the LLM to **extract structured attributes** —
NOT to emit JSON Patch ops. The parsed attributes are applied as `FieldPatch`es
(`author="dream"`) through `memory_writer.write_entity`; the JSON-Patch apply
pipeline is gone (`05_dream_cold_path.md` §4). Verbatim:

```
You are durin's memory extractor. From the conversation turns below, extract STRUCTURED ATTRIBUTES about the entity {ref} ({name}).

Rules:
- Only include facts explicitly stated in the turns. Do not invent or infer.
- Reuse an existing attribute key when the meaning matches (see EXISTING).
- Values are scalars or short lists of scalars — NO prose, NO nested objects.
- Output ONLY a JSON object mapping attribute_key -> value. No markdown, no commentary.

EXISTING ATTRIBUTE KEYS: {existing}

ENTITY BODY (prose the agent wrote — extract structure FROM it too):
{body}

CONVERSATION TURNS:
{turns}

JSON:
```

Input slots, all filled by `build_extract_prompt`:

| Slot | Source | Notes |
|---|---|---|
| `{ref}` | `page.entity_ref` | e.g. `person:marcelo` |
| `{name}` | `page.name` | display name |
| `{existing}` | `", ".join(sorted(page.attributes.keys()))` or `(none)` | the **key-reuse** block — drives per-entity schema-drift control (`05_dream_cold_path.md` §10): the LLM reuses `email` instead of minting `e-mail`/`correo` |
| `{body}` | `page.body` (truncated 4000 chars) or `(empty)` | the agent-authored prose, mined for structure too |
| `{turns}` | rendered conversation turns (truncated 12000 chars) | the new turns for this session |

The output is a bare JSON object. `parse_attributes` is tolerant: it strips code
fences, runs `json_repair`, and keeps **only** scalar / list-of-scalar values
(prose blobs and nested dicts are dropped). An empty / unparseable response is a
no-op (`05_dream_cold_path.md` §12). There is no `===PATCH===` marker, no
`provenance` field in the output, and no commit-message section — provenance is
attached per `FieldPatch` (`source_ref` = the session-turn marker
`[[sessions/<stem>.md#turn-<total>]]`) by the writer, and the commit message is
assembled inline by `memory_writer`.

**What this prompt does NOT include (vs the deleted consolidator):**
- No `existing_uris` / dedup block. Extract only enriches entities the agent
  already upserted via `memory_upsert_entity`, so it cannot create a duplicate
  (audit A5); cross-entity identity dedup is the refine pass's job (§5).
- No relation-count / relation-cap slot. The extract prompt writes attributes,
  not relations; the relation cap (soft 50 / hard 200) is enforced at write time
  in `memory_writer` (alert-only — `05_dream_cold_path.md` §4.1), not surfaced in
  this prompt.
- No git-history slot, no "starter types" slot, no per-entity cursor.

### 4.2 always_on rank prompt — curate the pinned guidance

`always_on_dream.py` `_RANK_PROMPT` ranks the **feedback entities**
(`stance` / `practice` / `feedback`) that compete for the always-on pin
(`05_dream_cold_path.md` §2.5). The LLM returns the refs in priority order, one
per line, dropping any item that contradicts a higher-priority one. Verbatim:

```
You are curating durin's ALWAYS-ON guidance: standing behavioural instructions injected into EVERY prompt. Below are candidate items, each as a ref line followed by its text. Return the refs in PRIORITY order (most load-bearing first), ONE PER LINE, and DROP any item that CONTRADICTS a higher-priority item (keep the one that better reflects the user's standing intent). Output ONLY refs, one per line — no prose, no numbering.

{items}
```

`{items}` is the candidate set, each rendered as a `ref` line followed by the
pinned-block render of its page (`_render_pinned_block`). The pass parses the
output line-by-line, keeps only refs it recognises, fits the survivors into
`memory.dream.always_on_token_budget`, and flips the `always_on` flag — it never
deletes anything (`05_dream_cold_path.md` §2.5). When no LLM is configured (or
there is a single candidate) it falls back to precedence (`user_authored` first)
then recency, with no prompt.

### 4.3 Skill-extract sub-agent

The skill-extract pass (`run_skill_extract_pass` in `dream_passes.py`) is **not**
a fixed consolidator prompt — it is an **agentic sub-agent**. It spins up an
`AgentRunner` with `ReadFileTool` / `EditFileTool` / `SkillWriteTool` and the
following **system prompt** (`_SKILL_EXTRACT_PROMPT`), then feeds it the recent
sessions' text as the user turn; the sub-agent decides whether to call
`skill_write` (`05_dream_cold_path.md` §2.3). Verbatim system prompt:

```
You are durin's skill extractor. Review the recent conversation(s) below. If the user established a REUSABLE PROCEDURE for a recurring task — a sequence of steps, a workflow, a how-to to follow again later — create or update a skill for it by calling the `skill_write` tool. A skill is a step-by-step procedure to FOLLOW, not a fact and not a one-off. Reuse/extend an existing skill instead of duplicating it. If the conversation contains no reusable procedure, do nothing — don't call any tool.

EXISTING SKILLS: {existing}
```

`{existing}` is the comma-separated list of skills already present
(`skills/<name>/SKILL.md`), or `(none)`. The behaviour — write a `SKILL.md` only
on a genuinely reusable procedure, reuse/extend rather than duplicate, no-op on a
one-off — is carried by the sub-agent's tool use, not by a strict output
envelope.

---

## 5. Absorb-judge prompt

Lives at `durin/templates/dream/absorb_judge.md` — the **only surviving file** under `templates/dream/`, loaded by `durin/memory/absorb_judge.py` (`_load_template` extracts the largest fenced block). Used by the **refine pass** (`run_refine` → `judge_pair`, `05_dream_cold_path.md` §8) to decide whether two entity pages with alias overlap describe the same real entity.

The prompt provides:
- Both entity pages (canonical candidate + absorbed candidate).
- Mtime of each (for staleness reasoning).
- The peer-review framing (judge is critic, not confirmer).

The LLM emits one of three verdicts (identity judgement, **not** action prescription — the refine pass maps verdict + confidence to the merge action per `05_dream_cold_path.md` §8.4):

| Verdict | Meaning |
|---|---|
| `same` | The two pages describe the same real entity |
| `different` | The two pages are distinct entities with overlapping aliases (homonymy, shared acronyms, generic placeholders) |
| `unclear` | Evidence is ambiguous; defer the call to a later pass |

Output envelope: `===VERDICT===` + `===CONFIDENCE===` + `===REASONING===` + `===END===`, parsed by `_parse_response` (tolerant of surrounding prose and verdict case). The current implementation (`durin/memory/absorb_judge.py::judge_pair`) is solid; this doc captures the contract.

---

## 6. Onboarding wizard text

The durin install wizard (`durin init` CLI command + the web onboarding flow) asks the operator a few questions. The memory-related questions:

### 6.1 Vector memory enable

The wizard surfaces this as an **"Enable vector memory"** toggle in the
memory submenu, **ON by default** — durin is a memory product, so the
semantic layer is the default experience. Accepting installs the `[memory]`
extra (fastembed + lancedb) and pre-downloads the embedding model so it
works out of the box; the model weights also auto-download on first use, so
the runtime self-heals even on a headless install once the extra is present.

```
Vector memory — ON  (embedding: intfloat/multilingual-e5-small)
  > Disable vector memory     ← opt-out here
    Change embedding model
    ...
```

**The toggle gates the VECTOR layer only.** When it's off (or the `[memory]`
extra is missing), the memory tools still work over the markdown files
(grep-level recall) — `memory_store`/`memory_search`/`memory_ingest` are
always available; only the embedding index is skipped. If `memory.enabled`
is true but the extra is absent, the agent loop warns once at startup
(actionable: `durin doctor --install-missing`) and degrades to grep rather
than failing.

### 6.2 Cross-encoder reranker (opt-in)

```
Recommended: durin can use a cross-encoder reranker to improve search
quality. This adds 300-800ms latency per query and requires ~600MB
additional RAM. The default model is `BAAI/bge-reranker-base` (MIT,
multilingual, ~100M params).

For a personal agent this is worth enabling: the rerank's latency is
dwarfed by the LLM call that follows every search, while the ranking
gain is direct. Decline if you run on edge / RAM-constrained hardware.
You can change this anytime via the web dashboard.

Enable advanced reranker now? [Y/n]:
```

Default presented at onboarding: **Yes (recommended)**. Note this is the
*onboarding* recommendation, not the config-level default: `MemorySearchConfig.cross_encoder.enabled` stays `False` so CI / test / headless
environments that build a default config never trigger the one-time
model download implicitly. The recommendation-ON lives in the wizard
prompt (`prompt_enable_cross_encoder(recommended=True)`), which writes
`true` into the operator's own config. Wording matches the trade-off
discussion in `03_search_pipeline.md` §9.5.

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
  - Judge model: uses your Dream model

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

### 8.4 Fragment selection (two-track model, N3 2026-06-06)

A fragment qualifies for the hot layer if its class is `episodic` or `stable` (not `corpus`, not `pending`) and it tags at least one entity. Qualifying fragments surface newest-first, capped by the budget.

There is no per-entity cursor. The earlier design folded an entity's fragments into its page and used a `dream_processed_through` cursor to graduate consolidated fragments out of the hot layer. The redesign does **not** consolidate fragments into pages — they are a separate raw track (`/remember` facts, session summaries) that coexists with the page — so nothing graduates a fragment out; the recency cap bounds the section and the LLM reconciles a fragment against the canonical page at read time using the timestamps.

### 8.5 Refresh cadence

The hot layer is **re-read from disk on every prompt build** (call: `read_hot_layer(workspace)` in `hot_layer.py`). The disk reads are cheap: a few markdown files + entries directory walk + frontmatter parse, typically < 5ms total.

**Why re-read each build instead of caching in memory:** simplicity + correctness. The alternative (in-process cache invalidated on Dream completion) requires a cache-invalidation contract between Dream and the prompt builder; getting it wrong leaves the agent with stale memory. Re-reading on every build trades 5ms for zero cache-coherence bugs.

**The practical effect of "cheap re-read each build":** between Dream passes, the `.md` files don't change → the assembled hot layer is byte-identical across consecutive turns → the upstream prompt cache (Anthropic / OpenAI) stays warm. A Dream pass invalidates the cache for one turn; the cache rewarms on the next turn. So in practice, the hot layer changes at most once per Dream pass:

- Between Dream passes, the underlying `.md` files don't change → hot layer rendering is identical → upstream prompt cache stays warm.
- A Dream pass that updates entity pages (or a newly-landed fragment) changes the hot layer → cache miss for that one turn → cache warms again on the next turn.

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
| 1 | Source of truth for LLM-facing text | This document. The per-tool `.description` property (e.g. `MemorySearchTool.description` in `durin/agent/tools/memory_search.py`) + `templates/agent/identity.md` + `templates/dream/absorb_judge.md` + the in-code dream prompts (`extract_dream._EXTRACT_PROMPT`, `always_on_dream._RANK_PROMPT`, `dream_passes._SKILL_EXTRACT_PROMPT`) must match this doc verbatim. Divergence = bug. (Audit C9 + B1, 2026-05-28: the v1 text referenced `memory_*.py::DESCRIPTION` constants that never existed; the canonical text lives in `_PARAMETERS["description"]` and is emitted via `Tool.description` → `function.description` in the OpenAI spec — see §3.7.) | §3.7 |
| 2 | Declarative not imperative phrasing | Validated by LoCoMo v2 (+3.9pp). "Don't answer cold" + "state source" + "issue 2-3 searches" worked; "USE BEFORE answering" did not. | §2.2 |
| 3 | Dream prompts are built in code (NOT a multi-file package, NOT a skill) | The four passes build prompts in code (`extract_dream` / `always_on_dream` / `dream_passes`); only `absorb_judge.md` is a template file. The old JSON-Patch consolidator package (`consolidator.md` + `json_patch_reference.md` + `rules.md` + `commit_format.md` + `examples/`) was deleted with the `DreamConsolidator`/`DreamRunner` cluster (audit `../../qa/post_migration_audit_2026-06.md`). | §4 |
| 4 | Extract output is a JSON attribute object, not JSON Patch | `build_extract_prompt` asks for `attribute_key → scalar/list`; applied as `FieldPatch`es via `memory_writer`. No JSON-Patch ops, no few-shot package — the RFC 6902 envelope and its examples are gone. | §4.1 |
| 5 | Extract key-reuse framing | `EXISTING ATTRIBUTE KEYS` block drives per-entity schema-drift control (reuse a key when the meaning matches). Per-entity scope; cross-entity dedup is the refine pass. | §4.1 |
| 6 | No commit-message section in any dream prompt | Commit messages are assembled inline by `memory_writer` / `absorption`; provenance is per-`FieldPatch` (`source_ref`). The old `===COMMIT===` / `Cursor-after` trailer contract is gone (audit B1). | §4.1 |
| 7 | Onboarding default for cross-encoder | OFF (matches §9.5 of doc 03). Wording communicates trade-off. | §6.2 |
| 8 | Structural markers carry only metadata | No valuative language. Class + URI + timestamp only. | §7.2 |

### Open

None at the module level.

---

## 10. Implementation status

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| `identity.md` Memory section | **Shipped (v2, 2026-05-25, +3.9pp on LoCoMo).** Both `## Memory` (read-time) and `## Memory writing` (write-time, B11) blocks present in `durin/templates/agent/identity.md`. Audit E23 (2026-05-28) flipped this row from "Light revision per §2 pending" — no concrete pending polish was specified, and the verified bench gain landed on what's currently in the template. | — | — |
| Tool descriptions | ✅ Sync'd. Each tool's `.description` property delegates to `_PARAMETERS["description"]` which carries the verbatim doc §3 text. `test_tool_description_sync.py` guards both string equality and the `to_schema()` invariant (audit B1, 2026-05-28). | — | — |
| Dream pass prompts | **Shipped (four-pass model).** `build_extract_prompt` (`extract_dream.py`), `_RANK_PROMPT` (`always_on_dream.py`), `_SKILL_EXTRACT_PROMPT` sub-agent (`dream_passes.py`) per §4. The old JSON-Patch `consolidator.md` package was deleted in the migration. | — | — |
| `templates/dream/absorb_judge.md` | Active (refine pass; only surviving `templates/dream/` file) | Same | None |
| Onboarding wizard text | **Shipped.** The default wizard `durin/cli/onboard_wizard.py` has a memory submenu (`_configure_memory`, P10 2026-05-30): vector-memory toggle (ON by default), embedding-model pick (with pre-download warmup), cross-encoder, Dream auto-absorb, and aux-model — wired from `onboard_memory.py`. (`onboard.py` is the legacy `--advanced` field-walker, not the default path.) | Done | — |
| Structural markers | CANONICAL/FRAGMENT in code | + SESSION + INGESTED | Renderer extension |

---

## 11. Cross-references

- Tool specifications (params + return + behavior): `04_agent_tools.md`.
- Dream passes that build and consume these prompts (extract / always_on / skill / refine): `05_dream_cold_path.md` §2, §8.
- Cross-encoder configuration that the wizard exposes: `03_search_pipeline.md` §9.5.
- Telemetry events from these LLM calls: `07_telemetry_and_observability.md` (pending).
- v2 identity.md result that validated the declarative style: `~/.claude/projects/.../memory/project_locomo_v2_prompts_result.md`.
