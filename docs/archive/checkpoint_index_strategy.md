# Checkpoint — index / embedding update strategy (surgical, not rebuild)

> Evaluation requested at the Phase 1-7 checkpoint. Concern: index rebuild has
> high cost (embeddings are per-doc); changes should be surgical
> (upsert / delete-then-insert), full rebuild only for tests / initial build.

## Verdict: the concern is valid, the architecture already enforces it, and the
## one place I'd violated it (Phase 7) is now fixed.

### 1. The architecture is surgical by design
Per-document primitives already exist and are the normal path:
- `indexer.reindex_one_file(workspace, path)` — re-index ONE file (FTS + vector
  upsert of that doc). This is what the daemon's `file_watcher` calls.
- `vector_index.upsert_entity_page / upsert / upsert_skill` — embed ONE doc.
- `fts_index.upsert(...)` — FTS ONE doc.
- `forget._drop_index_rows(...)` — delete ONE doc's index rows.
- `AliasIndex.add / remove / refresh_for` — mutate ONE ref in the shared map.

The `file_watcher` watches `memory/` recursively and, on any `.md`
create/modify, calls `reindex_one_file` for THAT file only. Because
`memory_writer` fast-forwards the working tree, every entity write lands a file
the watcher re-indexes surgically — **1 changed entity → 1 re-embed**, never N.

Full rebuild exists (`rebuild_fts_index`, `vector_index.rebuild_from_workspace`)
but is a rare, TRACKED event (`index_meta.last_full_rebuild`): initial build or
recovery — never per-change.

### 2. The regression I introduced (now fixed)
Phase 7's `delete_entity` / `unmerge` called `invalidate_alias_index`, which
drops the cached alias index so the NEXT access does a full disk walk (rebuild).
Fixed to the surgical path — the same one `absorb()` uses:
- `delete_entity` → `AliasIndex.remove(ref)` (drop one ref from the in-memory map).
- `unmerge` → `AliasIndex.refresh_for(page, slug)` (re-add the restored ref).
No rebuild; `invalidate_alias_index` stays reserved for out-of-band edits/tests.

### 3. "Rebuild" in the demos/tests
Where the Phase 1-7 demos/tests show an index building, it is the alias index's
lazy FIRST build on a fresh `tmp_path` workspace (an empty index has to populate
once). That is test-only. None of the new modules force a rebuild on a change in
a populated workspace.

### 4. Phase 8 index wiring — must stay surgical (and is largely free)
- **Entity write** (memory_writer / extract / refine): the working-tree ff +
  `file_watcher` → `reindex_one_file` already gives a surgical per-entity
  upsert. The integration is mainly "ensure the watcher is running and sees the
  ff'd writes" — not new rebuild code.
- **Reference ingest** (Phase 5): insert = embed its chunks once + FTS the whole
  doc once. An edited reference = drop its old chunk rows + insert new (the doc
  changed) — still surgical. `reindex_one_file` must learn the `type: reference`
  page (chunk for vector, whole for FTS).
- **Deletion** (Phase 7): drop the one doc's rows via `_drop_index_rows`
  (surgical), paired with the archive move. Do NOT rebuild.

### Bottom line
Every memory mutation maps to a surgical upsert / delete of the affected
document — never a full rebuild. The cost the user flagged (re-embedding) is
incurred once per changed doc, which is the floor. Phase 8 wiring reuses the
existing per-doc primitives + the watcher; full rebuild remains a tracked,
rare recovery operation.
