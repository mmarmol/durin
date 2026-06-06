# Phase 7 — Deletion + tombstones

> Builds on Phases 1-6. Branch `memory-redesign-phase1`.

**Goal:** User structural negative decisions (delete / un-merge) persist as
permanent tombstones the dreams respect (design §2.13/§2.14, gaps #3 + findings
3B-2/3D-1). Deletion is via archive (git-tracked, never hard-deleted); the user
overrides by explicitly re-authoring.

**Built:** `durin/memory/deletion.py`
- `delete_entity(workspace, ref, *, reason)` — moves the entity to
  `memory/archive/entities/<type>/<slug>.md` (stamped `deleted/deleted_at/
  deleted_reason`) + records a permanent tombstone in `memory/.deleted.json` +
  invalidates the shared alias cache.
- `delete_reference(workspace, ref)` — archives the reference (+ its
  `.chunks.jsonl` sidecar) + tombstone.
- `is_deleted` / `clear_delete_tombstone` — the tombstone check + the user
  override (re-creation clears it).
- `unmerge(workspace, canonical, absorbed)` — restores the absorbed entity from
  the archive (stripping the archive frontmatter stamp), invalidates the alias
  cache, and writes the `do_not_absorb` tombstone (`refine_dream.add_tombstone`)
  so the refine never re-merges the pair.

**Integration:**
- `extract_dream.extract_entity` returns early (no write) when
  `is_deleted(workspace, ref)` — the dream never re-creates a deleted entity
  from stale sessions.
- `memory_upsert_entity` clears the delete tombstone before writing — an
  explicit (re-)authoring is the user override.

**Verified:**
- 5 unit tests: delete archives + tombstones; extract respects the tombstone;
  clear overrides; reference delete (+ chunks); un-merge restores + writes
  do_not_absorb so the refine skips the pair.
- **LIVE (glm-5.1):** authored `company:globex` → deleted it → the REAL extract
  returned committed=False (tombstone gated it, no file); after
  `clear_delete_tombstone` the real glm-5.1 extract ran and re-created globex
  (founding_year/founders/products). Tombstone gates the real pipeline; override
  works.

**Bug fixed in flight:** the alias index is a workspace-shared cache mutated in
place during `absorb()` (the absorbed ref is removed). Restoring/deleting a file
must `invalidate_alias_index` or candidate detection reflects the stale map —
`unmerge` was not re-pairing the restored entity until this was added.

**Deferred (follow-on) — design §2.14/§6.2:**
- **Tool/CLI surface:** a `memory_delete` tool + dashboard delete/un-merge
  buttons that call these (the engine is ready; the UI is Phase 8 / webui work).
- **Git-commit** the archive moves (today they are working-tree file moves; the
  daemon's commit machinery picks them up).
- **Index drop:** remove a deleted reference's chunks from the FTS/vector index
  (paired with the Phase 5 index-wiring follow-on).
