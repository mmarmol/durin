# Phase 3 — Extract dream (core)

> Builds on Phase 1 (`memory_writer`) + Phase 2 (`memory_upsert_entity`).
> Branch `memory-redesign-phase1`.

**Goal:** The experience → knowledge bridge (design §2.6/§2.7, decision b): read
raw conversation turns about an entity and extract STRUCTURED ATTRIBUTES,
applied as field author `dream` via `memory_writer`. One entity ends up with
the agent's prose (body, author=agent) + the dream's structured attributes
(author=dream), with per-field provenance + precedence.

**Built (this phase):** `durin/memory/extract_dream.py`
- `build_extract_prompt(page, turns)` — focused extraction prompt (existing keys
  for reuse, the entity body, the turns; "JSON object of attributes only").
- `parse_attributes(raw)` — tolerant parse (fences stripped, `json_repair`,
  keeps scalar / list-of-scalar values, drops prose blobs + nested dicts).
- `extract_entity(workspace, ref, turns, *, llm_invoke=default_llm_invoke,
  model, source_ref)` — load page → prompt → LLM → parse → attribute
  `FieldPatch`es (author="dream") → `write_entity(create=True)`.
- Reuses `dream.default_llm_invoke` (litellm + z.ai + secrets) and the Phase 1
  precedence (user > dream > agent → a user-set attribute is never overwritten).

**Verified:**
- 5 stub-LLM unit tests: applies attributes as dream; does NOT overwrite a
  user attribute; idempotent (no duplicate key); fence/nested filtering;
  empty-output no-op.
- **LIVE (glm-5.1):** agent authored `company:mxhero` (prose body + relation,
  author=agent); the extract dream read a real session transcript and extracted
  ~10 structured attributes (founders, founding_year, main_product, awards,
  headquarters_location, carahsoft_partnership_start_year, …) all author=dream,
  coexisting with the agent's prose on one page, per-field provenance, 2 git
  commits. The core redesign idea proven end-to-end.

**Built (orchestration, `extract_runner.py`):** per-session cursor in the
`.meta.json` `derived.extract_cursor`; discovery of entities the agent authored via
`memory_upsert_entity` tool calls in the new turns; `run_extract_for_session`
processes post-cursor turns, extracts each entity, advances the cursor;
idempotent re-run. LIVE: ran over a session, glm-5.1 extracted 7 attributes,
cursor 0->2, re-run no-op.

**Deferred (follow-on, NOT in this phase) — design §2.7/§6.1:**
- **References as input:** extract entities from newly-ingested reference docs
  (same `extract_entity`, fed the reference text) — Phase 5 wires references.
- **Skills extraction:** create/fix skills from recent execution (reuse the
  legacy agentic `SkillWrite` path) — separate engine, separate task.
- **Trigger/cadence:** reactive (session-close + post-compaction) + ~2h
  safety-net + token gate. Core is callable directly; wiring is orchestration.
- **Model via `model_resolve`** (aux_models.memory) instead of the `default_llm_invoke`
  default. Minor.

**Decision flagged:** the extractor applies via the Phase-1 `memory_writer`
(plumbing+CAS, field-level provenance) rather than the legacy
`dream_apply.apply_dream_output` (working-tree + .md.bak + source_ref-only
provenance). This is the intended convergence (§2.4/§2.5) — the legacy apply
path is superseded for entity writes.
