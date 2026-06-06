# Phase 4 — Refine dream (dedup)

> Builds on Phases 1-3. Branch `memory-redesign-phase1`.

**Goal:** Graph hygiene — where the two creation paths (agent upserts + dream
extraction) converge, detect duplicate entities and merge them (design
§2.7/§2.13, gap #4). Conservative + reversible + respects user decisions.

**Built:** `durin/memory/refine_dream.py`
- `run_refine(workspace, *, llm_invoke, model, confidence_threshold=95)` —
  reuses the existing absorb machinery: `EntityAbsorption.find_candidates()`
  (alias-overlap pairs) → per pair: skip cross-type / tombstoned / user-managed
  → `absorb_judge.judge_pair` → if `verdict == "same"` and
  `confidence >= threshold` → `EntityAbsorption.absorb` (canonical keeps merged
  aliases/body; absorbed moved to `memory/archive/`).
- `is_tombstoned` / `add_tombstone` — a `do_not_absorb` registry
  (`memory/.refine_tombstones.json`, order-independent pair keys). A pair the
  user un-merged is never re-merged (design §2.13/§2.14, finding 3B-2).
- **user-managed protection**: a page the user opted to manage (page-level
  `author == user_authored`) is left alone.

**Phase 1 correction surfaced here:** `memory_writer` now stamps every page it
writes as page-level `author = "agent_created"` (§2.4: default agent-managed).
Previously every agent entity inherited `EntityPage`'s legacy `user_authored`
default, which made the refine treat *all* entities as user-managed. The user
opts to manage a page via a separate path (direct edit / dashboard).

**Verified:**
- 5 stub-judge unit tests: merges same; respects tombstone (not merged);
  keeps different; skips user-managed (judge not even reached); tombstone
  round-trip.
- **LIVE (glm-5.1):** two pages clearly the same company (shared alias +
  overlapping facts) → real judge `verdict=same confidence=99` → merged;
  absorbed page archived to `memory/archive/entities/company/`; 3 commits.
  Conservative threshold (95) + adversarial judge keep false-merges out.

**Deferred (follow-on) — design §2.7/§6.1:**
- **CAS-path absorb:** `EntityAbsorption.absorb` commits via porcelain
  (working-tree) — fine sequentially under the refine pass lock, but converging
  it onto `memory_writer`'s plumbing+CAS multi-file commit is a refinement.
- **Synonym-key unification** (email/e-mail → one) + **contradiction →
  temporal validity** (the other refine jobs beyond dedup).
- **Incremental dirty-set** (4A-2): candidate gen is already bounded by
  alias-overlap; scoping to recently-touched entities is an optimization.
- **Tombstone as user_authored marker** (vs the JSON registry) + the
  un-merge/revert UI that records it.
- **Trigger/cadence:** the periodic (~daily) schedule + pass-exclusion lock.
