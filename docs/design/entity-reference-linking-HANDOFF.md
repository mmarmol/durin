# HANDOFF — implementing entity → source-document linking (`derived_from`)

Resume point for a fresh session. Read this + the plan, then continue.

## What this is
Implementing the locked design in **`docs/design/entity-reference-linking-plan.md` (v4)**. An entity
gains a `derived_from: list["reference:<slug>"]` field (the documents it was distilled from), shown in
the graph as navigable reference nodes, reachable by drill, maintained by the dream. Read the plan for
the full rationale + verified touch points + the resolved decisions (Q1/Q2/Q3 at the end of the plan).

## Environment / workflow
- Worktree: `/Users/marcelo/git_personal/durin/.claude/worktrees/phantom-graph-policy-a`, branch
  `worktree-phantom-graph-policy-a`. In sync with origin/main. **Nothing pushed.**
- Tests: `python -m pytest tests/memory/<file> -q`.
- Deploy (frontend iter): `cd webui && npm run build`; `SP=/Users/marcelo/.local/pipx/venvs/durin-agent/lib/python3.14/site-packages`;
  `rm -rf "$SP/durin/web/dist" && cp -R durin/web/dist "$SP/durin/web/dist"`;
  `pkill -f "durin gateway"; nohup /Users/marcelo/.local/bin/durin gateway --foreground >> ~/.durin/logs/gateway-deploy.log 2>&1 </dev/null & disown`. Gateway: http://127.0.0.1:8765. For backend changes also `cp` the changed `.py` into `$SP/durin/...`.
- Rules: NO Claude attribution in commits; English code/docs; don't push until asked; clean `*.png`/`.playwright-mcp` before `git add`.

## Locked design decisions (from the plan)
- Field `derived_from: list["reference:<slug>"]` on EntityPage — general across entity types; value is a
  document ref (NOT another entity; entity↔entity is `relations`).
- Per-link provenance in `provenance["derived_from"]` = `{ref: {source_ref, author, extracted_at}}`,
  **keyed by ref** (merge-safe). NOT indexed (no schema bump). `source_ref` to the turn stays correct.
- **Q1**: re-key relation provenance by `(to,type)` (not index) — cleaner, consistent; migration OK
  (fix/delete existing; only user install + dev). Done together with the merge (Phase 3).
- **Q2**: references + entities both first-class visible; fair node weighting (entities get base weight
  ≥1 too) so neither tier is preferentially dropped by the 500-node cap.
- **Q3**: orphan references show in the overview but not in focus/ego mode (accepted).
- Drill goal: done-by-construction (drill returns full frontmatter+body → agent sees `derived_from`).

## Progress
- **P0 DONE + tested** (`durin/memory/entity_page.py`): `derived_from` added to `_KNOWN_FIELDS`,
  dataclass, `from_text` (lenient read), `to_markdown` (emit when populated), `_validate` (each entry a
  valid `reference:` ref). Tests: `tests/memory/test_entity_page_v2.py::TestDerivedFrom` (6). **44
  pass.**
- **P1 IN PROGRESS** (`durin/memory/field_patch.py`): added `"derived_from"` to `PatchKind` Literal and
  a branch in `apply_field_patch` (append+dedup to `page.derived_from`; ref-keyed provenance via
  `make_entry` + `incoming_wins` precedence). Test added:
  `tests/memory/test_field_patch.py::test_derived_from_add_dedup_and_ref_keyed_provenance`.
  **NEXT ACTION: run `python -m pytest tests/memory/test_field_patch.py -q`** (just added, not yet run).
- **Uncommitted working tree**: `entity_page.py`, `test_entity_page_v2.py`, `field_patch.py`,
  `test_field_patch.py`, and the plan/handoff docs. **Commit P0+P1 once field_patch test passes.**

## Remaining (in order)
1. **Finish P1**: run the field_patch test; commit P0+P1.
2. **P3 — write-time** (`durin/agent/tools/memory_upsert_entity.py`): new param `derived_from` (array of
   `reference:<slug>`) → emit `derived_from` patches in `execute`; add a line to `_DESCRIPTION`. Hint in
   `memory_ingest.py` `_DESCRIPTION`. Per-turn instructions: `templates/agent/identity.md` `## Memory
   writing` + `skills/memory/SKILL.md`. **Reorder the `memory_ingest` result dict so `id`+`reference`
   precede `content`** (C1 — survives 16KB head-truncation). **DOC-SYNC HARD GATE**: update
   `docs/architecture/memory/06_prompts_and_instructions.md §3.3/§3.5` + `04_agent_tools.md` verbatim
   (test `tests/memory/test_tool_description_sync.py`).
3. **P7 — api** (`durin/memory/graph_api.py`): `_serialize_page` add `derived_from` (~1 line);
   `_provenance_events` add a `derived_from` branch (M1) so per-link who/when surfaces. Webui entity
   panel: a "Fuentes" section listing `derived_from` → click → reference panel.
4. **Phase 2 — P5 graph** (`durin/memory/graph.py` + `webui/src/components/MemoryGraphView.tsx`):
   walk `memory/references/` (is_dir guard) → emit a node per reference (`type:"reference"`, label=title,
   weight=1+inbound, ALWAYS shown); emit `derived_from` edges via a **separate** loop (not the relations
   loop) AFTER references are registered; give entities base weight ≥1 (Q2). Webui: `TYPE_PALETTE` +=
   `reference` (amber, ~line 64-76); selection effect (~753-789) add `selected.type === "reference"`
   branch → `getEntryDetail("reference:<slug>")` → render reference content (relax `referenceDetail &&
   !selected` gate ~1643); add reference to the legend.
5. **Phase 3 — Q1 + P2 + P4**:
   - **Q1** (`field_patch.py` relation branch + `graph_api._provenance_events`): re-key relation
     provenance by `(to,type)` composite instead of `{index}`; lenient-read old index format.
   - **P2 merge** (`durin/memory/absorption.py` ~344-390): union+dedup `derived_from` AND pass
     `derived_from=<union>` into the `EntityPage(...)` constructor (~374); fold `provenance["derived_from"]`
     (ref-keyed, setdefault); fold relations provenance now that it's `(to,type)`-keyed (fixes the
     existing bug, item B).
   - **P4 dream** (`durin/memory/extract_dream.py` / `extract_runner.py` / `dream_passes.py`): new pass —
     for entities created/touched in a session lacking a `derived_from` link, read the session
     (conversation + reasoning + `memory_ingest` **call** `path` args) and judge which doc each entity
     was built from; resolve ref via ingest call `path` ↔ reference `source` (`reference.py:113`); emit
     `derived_from` patches. Register in `dream_passes.py`.

## Verified facts (don't re-verify)
- `drill.py:127` returns full file (frontmatter+body) when no anchor → goal-3 free.
- `_entity_text` (indexer.py ~763-773) excludes `derived_from` → not indexed, no `CURRENT_SCHEMA_VERSION`
  bump. `identifying_strings` iterates `extra` only → no pollution.
- 16KB truncation (loop.py ~1928) hits `role:"tool"` RESULTS only, NOT tool-call args. So P4 reads the
  ingest **call** `path` (safe); and reordering the ingest result dict (C1) makes the result ref survive too.
- `absorption.py:374-390` builds the merged page with explicit kwargs → a known field is dropped unless
  passed explicitly.
- `graph.py` §3.6 (~231-253) registers page-less relation targets as phantoms only at ≥2 sources; node
  cap sorts by `(-weight, id)` keep top 500 (~292-297); `build_entity_subgraph` reuses `build_memory_graph`.
- webui selection effect (`MemoryGraphView.tsx` ~753-789) branches `session` vs else→`fetchMemoryEntity`
  (a reference node 404s today); reference panel gated `referenceDetail && !selected` (~1643);
  `getEntryDetail("reference:<slug>")` already works (built earlier this branch).
- Two graph tests (`tests/memory/test_graph_builder.py` ~410/425) assert degree-1 relation targets are
  suppressed — references walked as real nodes is a separate path (don't break these; add new tests).

## The image bug (why we handed off)
The session was accumulating context from earlier removed/oversized images being re-sent each turn.
Fresh session avoids it. Nothing code-related.
