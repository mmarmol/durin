---
title: Entity → source-document linking (`derived_from`)
status: plan v4 — code-validated + adversarially reviewed; all decisions locked; READY to implement
---

# End goals (user)

1. An entity carries the **document(s) it was built from**, first-class, by ref — **general across all
   entity types** (the type vocabulary is open; this is not topic-specific).
2. **The graph SHOWS it**: reference documents are **first-class graph nodes** (they have real content,
   so they're visible with or without links), and `derived_from` renders as edges entity→reference,
   navigable (click → the reference content).
3. **Drill reaches the documents** from the entity.
4. The **dream maintains it**: while processing sessions it actively finds entities whose source link
   is missing and corrects them (by reasoning over the conversation), and **unions** sources on merge.

# Locked design decisions

- **Field**: `derived_from: list["reference:<slug>"]` on `EntityPage`. General (any entity type). The
  value is a **document** (reference) — NOT another entity (entity↔entity is `relations`; "built from a
  document" is a distinct, document-only relation). Name avoids the existing `source` (reference file
  path) and `source_ref` (per-field provenance = turn).
- **Per-link provenance**: recorded (who/when/from-which-turn added the link), **consistent** with how
  `relations`/`attributes` record provenance. **Keyed by the ref string** (a dict `{ref: entry}`), NOT
  by positional index — this is the one deliberate improvement over `relations` (whose index-keyed
  provenance is what makes its merge lossy). Ref-keyed → merges cleanly like `attributes`.
- **Merge**: union+dedup the list AND fold the ref-keyed provenance. **Also fix the existing bug** where
  absorption drops `relations` provenance entirely (user-approved, item B).
- **References become a walked graph node type** (first-class, always visible). `derived_from` = edges.
- NOT indexed (navigational metadata; reference body is already indexed separately). No schema bump.

---

# Round-3 review corrections (adversarial)

Phase 1 + Phase 2 verified solid. Two Phase-3 fixes + three notes:
- **C1 (P4 — reliable ref source)**: the ingest **result** dict serializes `content` (whole doc) before
  `reference`, so head-truncation (16 KB) can cut the ref for big docs; the **call `path`** is also
  imperfect (stored `source` is `.resolve()`-d absolute + the slug is the filename *stem*, so relative
  paths / same-stem files don't match cleanly). **Fix**: reorder the `memory_ingest` result dict so
  `id` + `reference` come **first** (survive truncation); the dream then reads `reference:<slug>` from
  the session's ingest result authoritatively. Tiny change, removes the path-normalization guesswork.
- **C2 (P2 — relations-prov) → resolved via Q1 (cleaner refactor)**: re-key relation provenance by
  `(to,type)` (not index). This removes the re-map problem entirely — the merge folds relations
  provenance by key (like attributes/`derived_from`). See Resolved decisions Q1.
- **M1 (P7)**: `graph_api._provenance_events` only enumerates `attributes`+`relations` — add a
  `derived_from` branch or the per-link who/when never surfaces in the Procedencia tab.
- **M2 (note)**: "references always visible" holds for the **overview** only. `build_entity_subgraph`
  (focus/ego) keeps the BFS closure, so an **orphan** reference (no inbound `derived_from`) won't show
  in focus mode; a linked one will. (Acceptable — assumed yes.)
- **M3 (note/tuning)**: reference nodes at weight ≥1 sort **above** weight-0 entities (a freshly
  upserted entity with no edges) under the 500-cap → can demote quiet entities. Tuning decision below.

# Parts (verified touch points)

## P0 — Schema: the `derived_from` field  (`durin/memory/entity_page.py`)
- `_KNOWN_FIELDS` (45-61) += `"derived_from"`; dataclass field `derived_from: list[str] = field(default_factory=list)` (79-108).
- **LOAD-BEARING** (round-2 finding): once it's a known field, the `extra` safety net no longer covers
  it (`from_text:166`). So it MUST be **explicitly read in `from_text`** (lenient, default `[]`, model
  on `relations_raw` 149-156) **and explicitly emitted in `to_markdown`** (conditional, model on
  relations 212-213). Miss either → silently dropped on every read-modify-write.
- `_validate` (323-353): each entry must pass `_is_valid_entity_ref` (67-72) AND `startswith("reference:")`
  (this field holds only document refs).
- Provenance shape: `provenance["derived_from"]` is a dict `{<ref>: {source_ref, author, extracted_at}}`
  — same `make_entry` shape as attributes, keyed by ref.
- No migration: lenient forward (existing pages read `derived_from=[]`, emit nothing).

## P1 — Patch kind `derived_from`  (`durin/memory/field_patch.py`)
- `PatchKind` (18) += `"derived_from"`. (Verified: unknown kinds fall through to `raise ValueError` at
  82; no exhaustive switch elsewhere breaks — adding the kind + branch is sufficient.)
- `apply_field_patch` (34-82): new branch — append `patch.value` (a `reference:<slug>`) to
  `page.derived_from` with dedup; record provenance `prov.setdefault("derived_from", {})[ref] = entry`
  via `make_entry` (consistent with the attribute branch 46-55, but keyed by ref).
- Respect precedence: if the ref already has a provenance entry, apply `incoming_wins` (same as
  attributes) so a higher-authority writer can re-stamp it. Dedup the list by ref.
- `memory_writer.write_entity` (142-225) flows it through unchanged. (Verified: the relation-cap path
  measures `page.relations` only, so a `derived_from` patch raises no spurious cap alert.)

## P2 — Merge: union + fold + fix the relations-provenance bug  (`durin/memory/absorption.py`)
- `derived_from`: union+dedup of `canonical.derived_from` + `absorbed.derived_from`, AND pass
  `derived_from=<union>` into the `EntityPage(...)` constructor at 374-381 (round-2 finding: the
  constructor enumerates kwargs; a known field is NOT carried via `extra` — omitting the kwarg drops it).
- `provenance["derived_from"]`: fold absorbed's ref-keyed entries via `setdefault` (canonical wins),
  same pattern as the existing attributes fold (368-372). Ref-keyed → no index problem.
- **Bug fix (item B)**: today absorption folds only `provenance["attributes"]` and **drops
  `provenance["relations"]`**. Fix: fold absorbed's relation provenance too. Because relation provenance
  is **index-keyed** and the merge **re-indexes** relations (canonical first, then appended absorbed),
  the fix must **re-map** each absorbed relation-provenance `index` to the new position of its relation
  in `merged_relations` before folding. (This is the real bug fix, not a one-liner — flag for review.)

## P3 — Write-time: the agent records it  (`durin/agent/tools/memory_upsert_entity.py`)
- The agent already RECEIVES `reference:<slug>` from `memory_ingest` (verified `memory_ingest.py:157`).
- New param `derived_from` (array of `reference:<slug>`) in `_PARAMETERS` (37-63); `execute` (96-110)
  emits `derived_from` patches. Description line (26-35) telling the agent to pass the ingested ref(s).
- `memory_ingest` description (39-53): hint that the returned `reference:<slug>` should be linked.
- Per-turn instructions (main agent only — subagents are read-only, no memory-write tools):
  `templates/agent/identity.md` `## Memory writing` (67-82) + `skills/memory/SKILL.md` (20-23).
- **Doc sync HARD GATE**: `test_tool_description_sync.py` compares tool `.description` verbatim to
  `docs/architecture/memory/06_prompts_and_instructions.md §3.3/§3.5`; update it + `04_agent_tools.md`
  in the same change or the suite is red.

## P4 — Dream maintains/corrects (goal 4), by reasoning  (`durin/memory/` + `dream_passes.py`)
Net-new code. The dream is an LLM pass that reads the session, so it can **understand** which document
each new entity was derived from (not just temporal adjacency).
- For each entity created/touched in the session whose `derived_from` is missing a link: the dream
  reads the conversation + reasoning + the `memory_ingest` **call** args (`path` — NOT the truncated
  result; round-2 finding: the result is head-truncated at 16 KB so `reference:<slug>` can be cut) and
  the `memory_upsert_entity` refs, and judges which doc(s) the entity was built from.
- Resolve the ref deterministically: ingest call `path` ↔ reference `source` field (`reference.py:113`)
  → the exact `reference:<slug>` (or via `ingested/<id>/meta.json`). The LLM decides the *pairing*; the
  path↔source lookup gives the exact id (no fuzzy slug guessing).
- Emit `derived_from` patches; `write_entity` preserves the field (verified read@HEAD→patch→commit).
- Register the pass in `dream_passes.py`.
- P3 (agent write-time) is the primary; P4 is the catch/repair.

## P5 — Graph: references as first-class nodes + edges (goal 2)  (`durin/memory/graph.py` + webui)
- **graph builder**: add a walk of `memory/references/` → emit one node per reference (`type:"reference"`,
  label = title, navigable, real content). **Always shown** (with or without inbound links), since they
  have content. Give them a real weight (e.g. `1 + inbound derived_from count`) so they sit fairly in
  the 500-node cap (292-297) instead of being weight-0 tail-drops.
- From each entity's `derived_from`, add an edge entity→`reference:<slug>` (typed `derived_from`) via
  its **own emit loop** (do NOT reuse the relations edge loop at ~117/265 — that reads `page.relations`,
  not this field). `is_dir()`-guard the references walk; walk references **before** emitting the edges so
  the target node exists and the both-endpoints guard (260-261) keeps the edge. Node-id `reference:<slug>`
  can't collide with entity ids (separate dirs; guard with a test).
- `build_entity_subgraph` reuses `build_memory_graph` (346) → no separate change.
- **webui** (`MemoryGraphView.tsx`): add `reference` to `TYPE_PALETTE` (64-76, amber); add a
  `selected.type === "reference"` branch to the selection effect (753-789; today the else-branch fetches
  the ENTITY endpoint → a reference node would 404) → `getEntryDetail("reference:<slug>")` → render the
  reference content (reuse the reference panel; relax its `!selected` gate at 1643); legend entry.

## P6 — Drill (goal 3) — done by construction
Verified `drill.py:127`: returns the full entity file (frontmatter+body) when no anchor → the agent
drilling the entity sees `derived_from` and drills the refs. **No drill change.**

## P7 — Panel exposure  (`graph_api.py` + webui)
- `_serialize_page` (142-161): add `derived_from` to the entity-detail response (~1 line).
- `get_entry_detail` (705-737) already resolves `reference:<slug>` → content (built this session).
- webui entity panel: a "Fuentes" section listing `derived_from` → click → reference panel.

## Cross-cutting (verified)
- **Indexing**: `_entity_text` (indexer 763-773) = name+aliases+attributes+relations+body; do NOT add
  `derived_from`. → not indexed, no `CURRENT_SCHEMA_VERSION` bump, no search pollution.
- **identifying_strings** (299): iterates `extra` only; `derived_from` is a known field → never pollutes
  the alias/merge index. ✓
- **Tests**: schema round-trip (read+emit); patch apply+dedup+provenance (ref-keyed); merge
  union+fold incl. the relations-provenance re-index fix; graph (references walked as nodes; weight;
  derived_from edges); drill exposure; doc-sync stays green.

# Phasing
- **Phase 1** = P0 + P1 + P3 + P7: field + agent write-time + panel + drill. New entities get linked,
  reachable by drill and shown in the entity panel.
- **Phase 2** = P5: references as first-class graph nodes + `derived_from` edges (the visual).
- **Phase 3** = P4 (dream maintain/correct) + P2 (merge union + fold + relations-prov bug fix).
  Backfills existing entities (the rabies one) and keeps links correct through consolidation.

# Resolved decisions (user, round 4)
- **Q1 → CLEANER REFACTOR**: re-key relation provenance by `(to,type)` instead of positional index
  (consistent with `derived_from`'s ref-keying; eliminates the fragile index re-map). On-disk format
  change is OK — no real usage yet; **migrate / fix / delete** existing provenance (only the user's
  install + dev). Touch points: `field_patch.py` relation branch (key by `(to,type)` composite, not
  `{index}`); `absorption.py` fold relations provenance by that key (parallel to attributes); update
  any reader that assumed `index` (`graph_api._provenance_events`); lenient-read old index-keyed data.
- **Q2 → FAIR WEIGHTING (a)**: references and entities are **both first-class consultation material**
  (visible so the user knows they exist — same standing as entities, surfaced by memory search).
  Give entities a base weight ≥1 too, so neither tier is preferentially dropped under the 500-cap. The
  cap is only the **overview render bound**; at extreme scale, search + focus + type-filters still
  reach everything. (No separate budget; no orphan-to-tail demotion.)
- **Q3 → ACCEPTED**: an orphan reference (nothing links it yet) shows in the **overview** but not in
  focus/ego mode (it's not a neighbour of the focused node); once linked it appears in focus too. The
  overview covers "know it exists"; focus is inherently neighbourhood-scoped.
