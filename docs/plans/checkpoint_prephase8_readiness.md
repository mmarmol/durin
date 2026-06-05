# Checkpoint — pre-Phase-8 readiness (integration smoke test)

> "Did I test everything I need before Phase 8?" — No. Each phase was verified in
> ISOLATION (fresh tmp workspace, direct calls, ≤1 LLM call) + the legacy suite.
> This smoke test composes Phases 1-7 in ONE workspace and runs the EXISTING
> readers (graph, indexer, hot_layer, search) over the new-model data.

## What the smoke test confirmed WORKS (de-risked)
- The full pipeline composed in one workspace — upsert → extract → refine
  (merge) → mark always_on → ingest reference → delete — runs without state
  corruption (entities consistent, merge archived, delete archived).
- `reindex_one_file` on a new-model ENTITY page: OK (FTS+vector upsert).
- `read_hot_layer`: reads the new entities (2 canonical blocks).
- `search_memory('globex')`: finds the new entity (1 result).
- `build_pinned_context`: OK.

## Gaps the smoke test FOUND (must be in Phase 8 scope)

### G1 — `build_memory_graph` ignores entity-page `relations` (HIGH, user-facing)
An entity with `relations: [{to: person:hank, type: founded_by}]` on disk, both
nodes present, yields **0 edges**. The graph builds edges from memory-ENTRY
entity-tag co-occurrence (the old model), not from the entity PAGE's explicit
`relations`. **The webui Memory graph would show the new entities as
disconnected nodes.** Phase 8 must teach `graph.py` to read `page.relations` as
edges. (Verified: `globex.relations on disk == [{to: person:hank, ...}]`,
`build_memory_graph edges == []`.)

### G2 — `reindex_one_file` rejects reference pages (MEDIUM)
A `memory/references/<slug>.md` (frontmatter `type: reference, title, source,
ingested_at, chunk_count`) fails `MemoryEntry` validation: *"skip incremental …
7 validation errors … id Field required … type Extra inputs not permitted."*
The indexer skips it → **references are not searchable**. Phase 8 must teach
`reindex_one_file` the reference shape (FTS the whole doc, vector the chunks via
the Phase 5 `.chunks.jsonl`), per the Phase 5 deferral — now confirmed with the
exact failure.

## Gaps NOT yet exercised (lower priority, note for Phase 8)
- **G3 — dual-write coexistence:** the smoke test exercised only `memory_writer`
  (plumbing+CAS). The legacy write paths (`store_memory`, `dream_apply` via
  GitStore porcelain) were not run concurrently on the same `memory/.git`. Git
  serialises commits, but verify when both paths are live (Phase 8 removes the
  old ones, so the window is small).
- **G4 — real workspace git repo:** `memory_writer` was tested on fresh tmp
  repos. The real workspace `memory/.git` has history + a `main`/`master` default
  (the `default_ref` HEAD-symref resolution handles both) + the running gateway
  may hold it. Verify against a COPY of the real workspace, never the live one.

## Bottom line
The double-check was worth it: it converted two "deferred index wiring" notes
into concrete, reproduced failures (G1, G2) with exact errors — now known inputs
to the Phase 8 plan instead of mid-integration surprises. Recommend G1 + G2 be
explicit first tasks of Phase 8 (reader compatibility) before wiring triggers.
