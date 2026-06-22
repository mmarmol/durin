---
title: Known bugs in current code (discovered during corpus work)
version: 0.1
status: live
last_updated: 2026-05-27
audience: humans + LLMs working on durin memory subsystem
purpose: Track bugs found during corpus authoring / audits / Phase 0 implementation. Each item has file:line, description, fix-plan, status. Cleared as bugs are fixed.
---

# Known bugs in current code

These were surfaced while authoring the spec corpus or running Phase 0 implementation. They are NOT corpus gaps (those live in `99_gaps_audit.md`); they are code-level defects.

Add new entries as discovered. Remove or mark resolved with commit SHA when fixed.

---

## B1. `absorption.py` — vector index inconsistency post-merge

**Severity:** Medium (data integrity / retrieval correctness)

**Source:** Verified by glm-4.6 bug report on 2026-05-24 (claude-mem observation #898). Confirmed in code review.

**Symptom:**
- When `EntityAbsorption.absorb(canonical, absorbed)` merges two entity pages, it deletes the absorbed entity's row from the LanceDB vector index but **never re-upserts the canonical**. The canonical entity page is rewritten on disk (with merged content), but its vector row in LanceDB still points to the pre-merge embedding (computed before the merge expanded `aliases`, added cross-page body, etc.).
- Result: vector search for terms newly relevant to the canonical (e.g., aliases inherited from the absorbed) does NOT match the canonical's row, because its embedding is stale.

**File:** `durin/memory/absorption.py::EntityAbsorption.absorb()` — the section that touches the vector index after writing the merged canonical to disk.

**Fix plan:** when Phase 0 refactors absorption to use top-level archive paths (see §3 of `09_implementation_roadmap.md` and the related decision in the gap audit), the same commit:

1. Re-embed the canonical from its post-merge `.md` content.
2. Upsert into LanceDB (replaces stale row).
3. Add regression test `test_absorb_re_embeds_canonical_after_merge`.

The fix is naturally grouped with the Phase 0 archive refactor — the consumer of `archive_entity()` will be `absorption.py`, and while we're touching it, we close this bug.

**Status:** Resolved — already fixed in earlier commit (before this bug tracker existed). Verified on 2026-05-27 by reading `absorption.py::absorb()` lines 247-273: the code calls `_vector_index.delete_by_id(absorbed)` followed by `_vector_index.upsert_entity_page(...)` with merged content. The observation #898 captured the bug at the moment it was diagnosed, but the fix shipped in a subsequent commit that the observation doesn't reference. Re-flagging this as a lesson:

> Before opening a bug from a memory observation, READ the current code — observations are point-in-time and may have been resolved without being re-noted.

No further work required. Entry kept here for historical traceability and as the lesson above.

---

## How to use this file

When you find a bug during corpus work, audits, or implementation:

1. Add a new section here with severity, source, symptom, file:line, fix plan, status.
2. Don't try to fix unrelated bugs while doing something else — that creates scope creep and confused commits. Note here and address it in the right commit.
3. When fixing, reference this file's section ID in the commit message (e.g., "closes B1 from docs/internals/memory/98_known_bugs.md").
4. When fixed, change `**Status:** Pending` to `**Status:** Resolved — commit <sha>` and leave the entry in place (don't delete) for historical traceability.

When this file accumulates too many resolved items, periodically prune to a `KNOWN_BUGS_ARCHIVE.md` to keep this file focused on live work.
