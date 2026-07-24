# Skills — architecture overview

> Single as-built reference for durin's skills subsystem: what it does today,
> how the pieces fit, and where each lives in code. For the SKILL.md format
> contract, see [`01_format_and_interop.md`](01_format_and_interop.md); for the
> cold-path lifecycle (skill-extract, observations, curation, suggestions), see
> [`02_lifecycle_and_curation.md`](02_lifecycle_and_curation.md); for the
> `skill.*` telemetry and how usage/effectiveness reach the webui, see
> [`03_telemetry_and_effectiveness.md`](03_telemetry_and_effectiveness.md).
> Citations are **file + symbol** (grep-stable).

---

## 1. Purpose

A skill is a markdown plugin that teaches durin how to perform a class of
tasks — installed once, refined over time, surfaced to the agent precisely
when it is needed. The subsystem manages the full lifecycle: authoring new
skills from session experience, importing verified skills from external
registries, curating and evolving skills based on live usage feedback, and
retiring skills that are no longer needed. Three properties define it:

- **Git-backed versioning.** Every skill lives in a git-tracked directory
  (`workspace/skills/<name>/`). Every mutation — create, edit, import, fuse —
  is a committed diff. History is an audit trail; rollback is `git revert`.
- **Single mutation chokepoint.** All writes go through
  `durin/agent/skills_store.py`. External sources (import, discovery,
  dream-create) pass through an explicit validate + scan + gate pipeline before
  any commit is made.
- **Three-tier retrieval.** Skills reach the agent via always-injection (full
  body, every turn), a usage-ranked hot-tier (names and descriptions), or
  on-demand FTS and vector search — matched to context window cost versus
  coverage needs.

---

## 2. Mental model

**Skills are versioned markdown plugins with two homes.** Builtins ship with
the durin package (`durin/skills/`; managed as `BUILTIN_SKILLS_DIR`). The
workspace git subtree (`workspace/skills/`) is the mutable layer: user-created,
imported, and dream-evolved skills live here. A workspace skill of the same name
shadows its builtin counterpart. Removing the workspace copy restores the
builtin.

**One chokepoint enforces the gate.** `durin/agent/skills_store.py` is the
single write path for every origin — the in-loop agent tool, the daily dream
pass, import, and curation all converge on the same commit machinery.
The import gate (`decide_action` in `durin/agent/skills_import.py`) is enforced
in code, never in prompt: dangerous sources are blocked, code-carrying or
cautioned sources require human confirmation, and only safe allowlisted sources
auto-proceed. The gate runs again at install, even if an earlier scan said safe.
The webui import finishes the job for an `allow` verdict (auto-install rather
than parking in quarantine) and short-circuits an already-installed skill before
the costly fetch. First-party **builtins** (`source == "builtin"`) are exempt
from the inventory scan — they ship vetted, so carrying scripts never flags them
caution/insecure; a *forked* builtin lives in the workspace and is scanned like
any other.

**Two ramps into the same gate.** `skill_write` is the quick, one-shot ramp:
the caller already has a full body (and any bundled files) in hand, in a
single call. `skill-drafts/<name>/` + `skill_publish` is the iterative ramp:
the agent builds and tests a skill with ordinary file and exec tools — a
script, a venv, a real input — over as many turns as it needs, then a single
`skill_publish` call promotes the finished draft. Both ramps converge on
`_finalize_skill`, the shared activation core: composition gate, security scan
of any bundled files, provenance stamp, attribution, `skill.authored`
telemetry, and the versioned commit that makes the skill visible. `skill_edit`
(a bounded update to an already-active skill) is a separate, narrower path
that does not re-enter this core.

**The registry is not a generic filesystem.** `write_file`, `edit_file`, and
`notebook_edit` refuse any path under `skills/`, redirecting the caller to the
draft flow instead — reads are unaffected, so an active skill's SKILL.md stays
directly readable. The only doors into `skills/` are durin's own store
operations: `_finalize_skill`, `apply_skill_edit`/`save_skill_content` (both
fork a builtin in first), import, and dream's curation and restructure passes.

**Three retrieval tiers match context cost to need.** Always-tier (`always:
true`) injects the full SKILL.md body into the stable system prompt every turn —
high cost, maximum incentive. The hot-tier lists names and descriptions for the
usage-ranked working set, letting the agent pull full bodies on demand with the
`skill_view` tool. The
searchable tier indexes every skill as a memory class (FTS and vector) reachable
via `memory_search(kind=skill)` — zero marginal cost until queried.

---

## 3. Diagram

```mermaid
flowchart TD
    subgraph Source["Source paths"]
        U["In-loop agent: create (quick)\nskill_write"]
        UE["In-loop agent: modify\nskill_edit"]
        SD["skill-drafts/name/\nwrite_file / edit_file / exec\nbuild + test, iterative"]
        D["Dream skill-extract pass\nrun_skill_extract_pass\n(calls skill_write)"]
        I["Registry discovery\nskill_search → skill_import"]
        M["Manual import\nskills_import.py"]
    end

    subgraph Gate["Validation and gating"]
        RC["resolve_candidates\nfetch into quarantine"]
        VS["validate_skill\nscan_skill → ScanReport"]
        DA["decide_action\ndangerous→block\ncode/caution/unknown→confirm\nsafe+allowlisted→allow"]
        II["install_imported_skill\nre-scan + enforce gate in code\nstamp provenance"]
        FIN["_finalize_skill\ncomposition gate → security scan\nprovenance + attribution\nskill.authored → commit"]
    end

    subgraph Storage["Workspace git subtree"]
        SK["workspace/skills/name/\nSKILL.md + assets\nGitStore auto_commit\nAttribution trailers"]
        QU["import-quarantine/\n.durin/import-quarantine/"]
        OB[".observations.jsonl\ngap / correction / improvement"]
    end

    subgraph Evolve["Mutation and curation"]
        CC["curate_catalog\ndaily delta: evolve / restructure / fuse / retire / principle"]
        FW["fork_on_write\ncopy builtin into workspace before edit"]
        DR["check_upstream_drift\nre-fetch + re-scan → evolve via judge"]
    end

    subgraph Retrieval["Retrieval tiers"]
        AT["Always-tier\nfull body in stable prompt\nget_always_skills"]
        HT["Hot-tier\nnames + desc working-set\ncompute_working_set"]
        SR["Searchable\nFTS + vector via SkillPage\nmemory_search kind=skill"]
    end

    subgraph Remove["Removal"]
        RM["remove_skill\nworkspace dir deleted + commit"]
        RV["revert-to-builtin\nworkspace fork deleted → builtin reappears"]
    end

    U --> FIN
    SD -->|skill_publish| FIN
    D --> FIN
    FIN --> SK
    UE --> SK
    I --> RC
    M --> RC
    RC --> QU
    QU --> VS
    VS --> DA
    DA -->|allow| II
    DA -->|confirm| II
    II --> SK
    SK -->|auto/workspace| CC
    SK --> FW
    FW --> SK
    CC --> DR
    DR --> SK
    SK --> AT
    SK --> HT
    SK --> SR
    OB -->|evidence| CC
    SK -->|mode=auto| RM
    SK -->|builtin fork| RV
```

---

## 4. How it works

### Create

Two ramps produce new skills, and both converge on the same activation core.

**The shared activation core.** `_finalize_skill` (`durin/agent/skills_store.py`)
is the chokepoint every create-time and publish-time write passes through,
whichever ramp produced the body. It first calls `_ensure_surface_frontmatter`
to backfill a missing `name`/`description` in the frontmatter, derived from the
body (`02_lifecycle_and_curation.md` §4) — so a body with no explicit
`description:` field still lands with a searchable one, regardless of which
ramp produced it. When the skill carries bundled files it then runs
`scan_skill` — the same deterministic scanner imports pass through — and a
`caution`/`dangerous` verdict quarantines the whole directory instead of
activating it (see Sweep and quarantine below for the quarantine shape). A
`safe` verdict (or no bundled files at all) stamps `metadata.durin.provenance`
(`source`, `created_at`, and `scan_verdict` when a scan ran) and
`metadata.durin.mode: auto`, commits through `GitStore.auto_commit` with
Attribution trailers (Actor, Session, Agent), calls `_sync_index` to update FTS
and vector, and emits a `skill.authored` telemetry event — `ramp` names which
door produced it (see `03_telemetry_and_effectiveness.md`). Only a skill that
completes this core is visible to the loader, search, and curation; a
quarantined skill emits no `skill.authored` event.

**Quick ramp — `skill_write`.** For a body the agent already has in hand. The
core tool calls `skills_store.dream_create_skill`, which runs the composition
gate (`02_lifecycle_and_curation.md`), refuses outright if no description can
be derived from the body either (frontmatter or prose), writes `SKILL.md` (+
any bundled `files`), then hands off to `_finalize_skill` with `ramp="write"` —
which backfills the frontmatter as described above. One call, one commit.

**Dream skill-extract pass.** `durin/memory/dream_passes.py::run_skill_extract_pass`
runs a sub-agent (`AgentRunner`, `max_iterations=8`) over recent sessions plus
any logged `new:*` gap observations. When the sub-agent finds a recurring
multi-step procedure it calls `skill_write` — the same tool and the same quick
ramp the in-loop agent uses. The pass returns early without an LLM call when
there are no sessions and no gap observations.

**`skill_edit`** (bounded update to an already-active skill) is a separate,
narrower path — see Evolve below and `02_lifecycle_and_curation.md`. It forks a
builtin into the workspace via `fork_on_write` before applying the diff, so the
builtin package is never touched, and it does not re-enter `_finalize_skill`:
no composition re-gate, no new `skill.authored` event, because the skill is
already active.

### Draft and publish

The iterative ramp exists for a skill that needs to be built and proven before
it is trustworthy — a bundled script with a virtualenv, a dry run against real
inputs, more than one edit-and-check cycle — where a single `skill_write` call
would mean handing over an untested body.

**`skill-drafts/<name>/`** is a scratch directory at the workspace root,
alongside `skills/` but outside it. Generic tools (`write_file`, `edit_file`,
`exec`) read and write there freely, so the agent can scaffold, edit, run, and
re-run a skill exactly the way it would any other workspace file — a script, a
venv, a config, a fixture. Nothing under it is committed to the skills git
subtree, indexed, or curated: because it sits outside `skills/`, the loader,
FTS/vector search, `curate_catalog`, usage tracking, and the unverified-origin
sweep (below) never see it. A draft is inert until published, and it persists
across sessions until it is either published or discarded — there is no
cleanup sweep for an abandoned draft.

**`skill_publish`** promotes a finished draft. It runs the same body/description
integrity check `skill_write` enforces (`_skill_md_integrity`) and the
composition gate against the draft's `SKILL.md` *before* moving anything, so a
malformed or rejected draft is left exactly as it was, for revision. It then
refuses if a skill of the same name is already active — nothing is clobbered —
moves the draft directory into `skills/<name>/`, and hands off to
`_finalize_skill` with `ramp="publish"`. Because both ramps converge on that
same core, a draft with a derivable body but no explicit `description:` field
gets the identical frontmatter backfill the quick ramp gets — a published
skill is never left with an empty indexed description. `skill_publish` carries
the same `override_composition` escape hatch `skill_write`'s in-session door
does (the user's explicit word, after seeing the gate's reason); there is no
dream-side "hard" variant for it, because dream never builds drafts.

**`skill_discard`** deletes `skill-drafts/<name>/` outright. It never touches
the active registry — there is nothing to gate, since nothing was ever
published.

**The registry is write-guarded.** The generic `write_file`, `edit_file`, and
`notebook_edit` tools refuse any path under `skills/`, redirecting the caller
to the draft flow instead; `read_file` (and other read-only tools: `list_dir`,
grep/search, the repo overview) are unaffected, so an active skill's SKILL.md
stays directly readable. The same guard covers the other registries that own a
validated, versioned write door — `workflows/` (use `workflow_write` /
`workflow_edit`, and `workflow_script_write` for the scripts a script node runs)
and `loops/` (use the loop tools) — because otherwise the rule is only an
instruction in a skill, and a generic write lands unvalidated and unversioned.
The guard lives in the agent-facing tools themselves
(`durin/agent/tools/filesystem.py`, `.../notebook.py`); durin's own store
operations write to `skills/` directly through `skills_store.py` and never go
through those tools, so they are unaffected by it. The one deliberate opt-out
is dream's agentic `restructure` action (`02_lifecycle_and_curation.md`): its
sub-agent edits an isolated tempdir copy that mirrors the `skills/<name>/`
layout purely so path references line up, not the live registry — that copy
is validated through the same gated commit path before anything reaches
`skills/`, so the guard would only be blocking a write to a directory nothing
ever reads from.

**skill-creator** (the builtin skill for authoring skills) scaffolds new
skills into `skill-drafts/` and finalizes with `skill_publish`, matching this
ramp — it no longer writes into `skills/` directly.

There is no web or CLI surface for drafts; the scratch area is reachable only
through the same in-loop tools that read and write the rest of the workspace.

### Import

All external sources (registry hits, GitHub refs, HTTPS URLs, local paths) share
one pipeline in `durin/agent/skills_import.py`:

1. `resolve_candidates(source)` resolves the ref to one or more candidates.
2. `fetch_candidate` downloads into `.durin/import-quarantine/<name>/` (zip-slip
   safe, SSRF-safe, file size and count capped by `skills.security` limits).
3. `validate_skill` checks the agentskills.io format (name, description, code
   detection via `iter_code_files`).
4. `scan_skill` (`durin/security/skill_scan.py`) runs a deterministic static
   scan: body regex rules (prompt injection, hidden instructions, sensitive
   paths, secrets, unicode bidi) plus an AST behavioral pass on bundled Python
   scripts (shell exec, dynamic eval, reverse shell patterns). Returns a
   `ScanReport` with `findings` and a `verdict` of `safe`, `caution`, or
   `dangerous`.
5. `decide_action(source, verdict=..., carries_code=..., allowlist=...)` applies
   the trust-times-verdict gate: `dangerous` → block; `carries_code` OR
   `caution` OR source not in allowlist → confirm; safe + allowlisted → allow.
6. `install_imported_skill` re-runs the scan on the quarantined copy (fresh, in
   case of tampering since the initial scan), enforces the gate a second time in
   code, stamps `metadata.durin.provenance`, and calls `GitStore.auto_commit`.
   `_sync_index` updates the search indices.

The optional LLM judge (`durin/security/skill_judge.py`,
`skills.security.llm_judge.trigger`, default `off`) adds a semantic layer after
the static scan for paraphrased or non-English injection patterns. It is
opt-in; the static scan is always the primary gate.

### Discover

`durin/agent/skill_registry.py` provides a `SkillRegistry` protocol and two
adapters: `SkillsShRegistry` (queries `skills.sh/api/search`) and
`ClawHubRegistry` (queries clawhub's ranked `/api/v1/search?q=` endpoint — not
its recency list). `search_registries` queries both adapters in parallel
(SSRF-safe), deduplicates by ref, and round-robin interleaves results using a
stable `crc32` tiebreak so no registry permanently owns the top slot.

A preview call (`describe` endpoint / `web_skill_describe`) reads only the
SKILL.md body from the remote source — it never installs or executes anything
and degrades gracefully on network errors. This lets users inspect a skill
before routing it through the import gate.

### Evolve

`durin/agent/skill_curation.py::curate_catalog` runs as the final step of the
daily dream cron. Before anything is judged, it deterministically backfills any
`auto` skill missing a frontmatter description (derived from the body — see
Create above) and folds those repairs into the delta. It then reviews the
change-gated delta: `mode="auto"` AND `source="workspace"` skills that
`needs_curation` (body changed since last pass, **or** the stored
`curation_rules` stamp is older than the current `CURATION_RULES_VERSION`,
which forces a one-time recheck of the whole set after a rules change), plus
any auto workspace skill with an OPEN observation even if its body is
unchanged. An LLM judge proposes `evolve` (surgical edit), `restructure`
(agentic doctrine repair via `restructure_skill_agentic`), `fuse` (merge
near-duplicates via `dream_fuse_skills`), `retire` (delete via `remove_skill`,
git-recoverable), `principle` (add a cross-cutting rule), or `retire_principle`
actions. The judge receives OPEN observations as evidence and DECLINED history
to prevent re-proposing rejected changes. Imported skills are `mode=manual` and
are not auto-curated — instead, they are handled by the suggestion path
described below. See `02_lifecycle_and_curation.md` for the full delta-build
sequence, the observation queue and skill-signal hindsight detection that feed
it, and `03_telemetry_and_effectiveness.md` for the `skill.*` events each step
emits.

The judge is also told which reviewed skills carry a **recent user hand-edit**,
read straight from the skill git editorial: `user_edits_since_curation` returns
each `Actor: user` commit since the last `curated @` stamp **with its unified
diff**, so the judge sees exactly what the user changed by hand, not merely that
a change happened. `auto` means dream *may* improve a skill, not that
the user is locked out of it: a user may edit an auto skill directly (see the
web editor note under Format & interop), and the edit stays `auto`. The judge
is instructed to treat such edits as intentional — it may still evolve them for
a concrete, stated reason, but must not revert or undo a user edit silently.

#### Skill suggestions (manual skills)

When `memory.dream.skill_suggestions_enabled` is on (the default), the daily
curation pass also evaluates `mode=manual` workspace skills and, where it
would propose `evolve` or `retire`, enqueues the proposal as a
**suggestion** in the dream bandeja rather than applying it directly. The user
can accept or reject each suggestion from the webui; nothing is applied
without explicit acceptance. Fuse suggestions (merging two manual skills into
one) are out of scope for the suggestion path for now; they remain an action
type available only in the auto-curation pass.

Each suggestion carries:
- the proposed action and the judge's reasoning
- for content changes (`evolve`), a unified-diff patch rendered by the
  `DiffViewer` component in the webui — the reusable display seam for future
  history views as well

Rejecting a suggestion writes an **expiring tombstone** (approximately 30 days)
so the same conclusion is not re-proposed within that window. This is a
time-limited signal, distinct from the `do_not_absorb` tombstone used by the
refine pass, which is permanent. Once the tombstone expires the curation judge
re-evaluates the skill normally on the next applicable run.

Auto skills and the in-loop `skill_edit` path are unaffected: they are curated
and applied as before.

**Observation queue.** `durin/agent/skill_observations.py` persists live
feedback from the `skill_observe` core tool (`correction`, `gap`,
`improvement`, `simplify` kinds) to `skills/.observations.jsonl` inside the
git subtree. Edits via `skill_edit` on auto skills auto-log an improvement
observation. A hindsight pass during the dream extract
(`discover_skill_signals` in `durin/agent/skill_signals.py`) scans the
**tail** of post-cursor session turns (tail-windowed, not head, so corrections
at the end of long sessions are not missed). Observations accumulate with
dedup-by-count; `count >= 2` is the recurrence signal that licenses curation
action. See `02_lifecycle_and_curation.md` for the full observation lifecycle
(OPEN/APPLIED/DECLINED, paraphrase-tolerant dedup, archival) and
`03_telemetry_and_effectiveness.md` for how usage and observation counts reach
the webui Skills panel.

**Cross-cutting principles** are promoted by the curation judge into
`skills/.principles.jsonl` (capped at 12 entries). They are injected into both
the curation prompt and the skill-extract prompt so new and evolved skills are
born compliant.

**Upstream drift.** `durin/agent/skill_drift.py::check_upstream_drift` is
wired into `curate_catalog`. For a skill whose `provenance.source` is a real
repo, it re-fetches + re-scans; if the content changed and the gate says allow,
the upstream body is fed to the curation judge to merge via `evolve` while
preserving local edits. Confirm or block sources are left for human review.

### Remove

`skills_store.removable_action` classifies the target:

| Case | Condition | Action | Effect |
|---|---|---|---|
| Created / imported / dream | workspace dir, no builtin of same name | `remove` | dir deleted, git commit, `_unsync_index` |
| Forked builtin | workspace dir exists and builtin of same name exists | `revert` | workspace copy deleted, git commit, `_unsync_index` |
| Pure builtin | no workspace dir | `null` | refused — package is not touched |

On revert, `_unsync_index` is correct: workspace skills are the only ones
indexed (builtins are never indexed), so dropping the fork's FTS and vector
rows restores the builtin's pre-fork un-indexed state. No re-index of the
builtin is needed.

Removal is not exposed as an LLM-callable tool — it is an admin action
reachable from the web panel, the CLI (`durin skill remove <name>`), and the
chat command (`/skills remove <name>`).

### Retrieve

`durin/agent/skills.py::SkillsLoader` loads skills for context assembly:

1. **Always-tier.** `get_always_skills()` returns skills with `always: true`
   in frontmatter. `load_skills_for_context(names)` injects full SKILL.md
   bodies (without frontmatter) into the stable system prompt every turn.
   `disable_model_invocation` skills are excluded from the visible catalog but
   the always-tier is not filtered by the visible catalog — it is a stable
   injection that bypasses hot-tier and searchable logic.

2. **Hot-tier.** `durin/agent/skill_usage.py::compute_working_set` aggregates
   `skill_calls` from session sidecars — view events (`skill_view`), read
   events (`read_file` on a `SKILL.md`), and edit events (`skill_edit`) — to produce a usage-ranked set
   of names. The set is split into a `frequent` slice (top N by call count over
   a 7-day window) and a `recent` slice (top N over 24 hours), deduped and
   filled with remaining candidates. Sizes are controlled by
   `agents.defaults.skills_hot_tier`. The hot tier injects names + descriptions
   into `templates/agent/skills_section.md`; the agent loads full bodies on
   demand with the `skill_view` tool (`durin/agent/tools/skill_view.py`), which
   also returns the skill's bundled-file map and a readiness check, or a raw
   `read_file`. The computed set is memoized in `ContextBuilder` keyed on the
   candidate name-set: usage re-ranking never churns the cache-stable prefix
   turn to turn, but installing or removing a skill changes the key, so the
   catalog reflects it on the next turn without a process restart.

3. **Searchable.** `durin/memory/skill_page.py::SkillPage` is the memory class
   for skills: it wraps the SKILL.md frontmatter and body for FTS and vector
   indexing. When `memory.index_skills` is on (default), every workspace skill
   is reachable via `memory_search(kind=skill)`. `disable_model_invocation`
   skills are indexed but excluded from searchable results the model sees.

### Sweep and quarantine

`durin/agent/skill_lifecycle.py::sweep_unverified_skills` runs at
`ContextBuilder.__init__` and on surface calls. It relocates any workspace skill
that arrived without `metadata.durin.provenance` (a registry CLI or manual file
drop) to `.durin/import-quarantine/`, prepends an `unverified_origin` finding
to the scan report, and makes it inert for the agent. Approve re-gates through
`install_imported_skill`; reject deletes.

**Broken frontmatter is not "no provenance".** A YAML typo in the hand-written
fields (an unquoted `:` in a plain multi-line description is the classic) makes
the whole frontmatter unparseable, which used to read as "no provenance" and
expel a legitimately-gated skill — destroying its original provenance on
re-import. `_durin_blob` now falls back to a metadata-only parse
(`skills_frontmatter.recover_metadata`): the `metadata.durin` blob is
machine-written and parses on its own even when the prose above it is broken,
so mode and provenance survive the typo. The sweep additionally logs one OPEN
`correction` observation naming the unparseable frontmatter (deduplicated —
never re-logged or bumped per sweep), so the breakage surfaces for repair
instead of failing silently. A broken-frontmatter skill with **no** recoverable
provenance is still quarantined: deliberately corrupt YAML must not become a
sweep bypass, and presence-of-provenance is all the sweep ever checked.

**Attributed backstop.** Before quarantining, the sweep tries to identify who
produced the skill: it reads the skill's own path-scoped git log
(`GitStore.log(path=name)` — the same path-scoping `skill_history` and
`user_edits_since_curation` use, so a generically-named skill is never
attributed off some unrelated commit whose *message* happens to mention its
name) for the introducing commit's `Session:` trailer. A trailer found there
becomes `source: "agent:session:<id>"` in the quarantine's `.scan.json`, and
the sweep emits the same `skill.authored` event the two authoring ramps do,
with `ramp="backstop"` — a skill that slipped past the write-guard is still
credited to the session that produced it, even though it lands in quarantine
rather than active. When no commit carrying a `Session:` trailer can be found
for that path (or the skills store was never initialized), attribution falls
back to `source: "unverified:workspace"` and no `skill.authored` event fires —
there is no session left to credit.

---

## 5. Key types and entry points

| Symbol | File | Role |
|---|---|---|
| `SkillsStore` functions (`_store`) | `durin/agent/skills_store.py` | Single write chokepoint for all skill mutations. Provides `dream_create_skill`, `install_imported_skill`, `remove_skill`, `fork_on_write`, `mark_curated`, `needs_curation`. All writes call `GitStore.auto_commit` with `Attribution` trailers. |
| `_finalize_skill` | `durin/agent/skills_store.py` | Shared activation core for both authoring ramps: backfills a missing frontmatter `name`/`description`, scans bundled files, stamps provenance + `mode=auto`, commits with attribution, syncs the index, emits `skill.authored`. Quarantines instead of activating on a caution/dangerous scan (no event emitted). |
| `publish_draft_skill` / `discard_draft_skill` | `durin/agent/skills_store.py` | Promote `skill-drafts/<name>/` into the registry via `_finalize_skill` (`ramp="publish"`) — gated on the same integrity check and composition gate as create, refusing a name collision with an already-active skill — or delete the draft outright without touching the registry. |
| `Attribution` | `durin/agent/skills_store.py` | Actor, Session, Agent trailers stamped on every skill git commit. |
| `SkillsLoader` | `durin/agent/skills.py` | Loads skills from workspace (shadows builtins). `list_skills`, `load_skill`, `get_always_skills`, `build_skills_summary`, `load_skills_for_context`. |
| `decide_action` | `durin/agent/skills_import.py` | Trust-times-verdict gate: dangerous → block; carries_code / caution / not-allowlisted → confirm; else allow. Enforced in code at install. |
| `scan_skill` / `ScanReport` | `durin/security/skill_scan.py` | Deterministic static scan: body regex rules + AST behavioral pass. Returns `findings` and `verdict` (safe / caution / dangerous). |
| `SkillRegistry` protocol + adapters | `durin/agent/skill_registry.py` | Protocol: `search(query, limit)` → list of `SkillSearchHit`. Two adapters: `SkillsShRegistry` (skills.sh) and `ClawHubRegistry` (clawhub). `search_registries` queries both in parallel with round-robin interleave. |
| `SkillCandidate` / `resolve_candidates` / `fetch_candidate` | `durin/agent/skill_resolve.py` + `durin/agent/skills_import.py` | Resolve a source ref to candidates, fetch into quarantine (zip-slip safe, SSRF-safe, size-capped), validate SKILL.md format. |
| `validate_skill` | `durin/agent/skills_import.py` | Checks agentskills.io format (name, description, code detection). Returns `ValidationReport` with `carries_code`. |
| `skill_observe` / observation queue | `durin/agent/skill_observations.py` | Task-observer pattern. In-session feedback: `correction`, `gap`, `improvement`, `simplify`. Store: `skills/.observations.jsonl`. States: OPEN → APPLIED / DECLINED. Principles: `skills/.principles.jsonl` (cap 12). |
| `curate_catalog` | `durin/agent/skill_curation.py` | Daily delta-curation over `mode="auto"` workspace skills. LLM judge proposes evolve / restructure / fuse / retire / principle. Change-gated: never scales with full catalog size. |
| `SkillPage` | `durin/memory/skill_page.py` | Memory class for skills. Wraps SKILL.md frontmatter and body for FTS and vector indexing. Enables `memory_search(kind=skill)`. |
| `run_skill_extract_pass` | `durin/memory/dream_passes.py` | Daily dream pass: sub-agent mines recent sessions and gap observations, calls `skill_write` for recurring procedures. Agentic (uses `AgentRunner`). |
| `sweep_unverified_skills` | `durin/agent/skill_lifecycle.py` | Relocates no-provenance workspace skills to quarantine, prepends `unverified_origin` finding. Attributes to `agent:session:<id>` via a path-scoped git-log read when the introducing commit carries a `Session:` trailer (emits `skill.authored`, `ramp="backstop"`), else falls back to `unverified:workspace`. Runs at ContextBuilder init and on surfaces. |
| `compute_working_set` | `durin/agent/skill_usage.py` | Hot-tier sizing: frequent (7-day window) + recent (24-hour window) skill calls from session sidecars. Returns list of skill names. |
| `get_always_skills` / `load_skills_for_context` | `durin/agent/skills.py` | Always-tier retrieval: filter for `always: true`, inject full bodies into stable prompt. |
| `skills_inventory` / web routes | `durin/agent/skills_surface.py` | Read model for CLI and web. Augments skill list with verdict and findings (pinning a stricter import-time provenance verdict via a synthetic `import_verdict` finding), removable action, review overrides, requirements resolution. No mutations. |
| `check_upstream_drift` | `durin/agent/skill_drift.py` | Re-fetches and re-scans a skill's source repo. If changed and gate allows, feeds upstream body to curation judge for surgical evolve. |

---

## 6. Configuration and surfaces

### Configuration keys (`durin/config/schema.py`)

| Key | Default | Meaning |
|---|---|---|
| `skills.security.allowlist` | `DEFAULT_SKILL_ALLOWLIST` (first-party orgs) | Source-ref prefixes that skip the source-confirmation step. Code and dangerous gates have no opt-out. |
| `skills.security.github_token_secret` | `""` | Name of the durin secret holding a GitHub API token for authenticated fetches. |
| `skills.security.max_files` | 100 | Per-fetch file count cap. |
| `skills.security.max_total_bytes` | 3 MB | Per-fetch total size cap. |
| `skills.security.max_file_bytes` | 1 MB | Per-file size cap. |
| `skills.security.llm_judge.trigger` | `"off"` | When to run the semantic LLM judge: `off` (never auto), `uncertain` (when already cautioned), `always`. |
| `skills.discovery.registries` | skills.sh + clawhub enabled | List of `SkillRegistryConfig` entries. Both adapters enabled by default. |
| `skills.discovery.search_limit` | 10 | Max hits returned per registry search. |
| `skills.install_policy` | `"approve"` | Governs `skill_install_deps`: `never` (report only), `approve` (dry-run then confirm), `auto` (run without per-call confirm). All policies execute through ExecTool. |
| `memory.index_skills` | `true` | Index workspace skills as a searchable memory class (FTS + vector). |
| `agents.defaults.skills_hot_tier.enabled` | `true` | Enable the usage-ranked hot-tier. False restores full-catalog injection. |
| `agents.defaults.skills_hot_tier.frequent` | 30 | Top N skills by call count over the frequent window. |
| `agents.defaults.skills_hot_tier.recent` | 15 | Top N skills by call count over the recent window. |
| `agents.defaults.skills_hot_tier.frequent_window_hours` | 168 (7 days) | Window for the frequent slice. |
| `agents.defaults.skills_hot_tier.recent_window_hours` | 24 | Window for the recent slice. |
| `agents.defaults.disabled_skills` | `[]` | Skill names excluded from loading entirely. |
| `memory.dream.cron` | `"0 3 * * *"` | Schedule for the daily dream cron (all consolidation passes + skill curation). |
| `memory.dream.max_seconds_per_run` | 600 | Hard wall-clock cap per extract pass (yields after current session; cursor resumes on next trigger). |
| `memory.dream.skill_signals_enabled` | `true` | Run `discover_skill_signals` over post-cursor session turns during the extract pass. |
| `memory.dream.skill_suggestions_enabled` | `true` | When on, curation proposes actions for `mode=manual` skills as bandeja suggestions (accept/reject) rather than applying them directly. Disable to exclude manual skills from curation entirely. |

### CLI surfaces

| Command | What it does |
|---|---|
| `durin skill list` | List available and quarantined skills with verdict and availability. |
| `durin skill search <query>` | Search configured registries; returns ranked hits with ref and source. |
| `durin skill remove <name>` | Remove or revert-to-builtin (admin action; not an agent tool). |
| `durin memory dream` | Run the core dream passes manually (extract → derived_from → skill_extract → refine → always_on). `curate_catalog`, the document/relation passes, and workflow-improve run only in the gateway cron job, not by this command. |

### Agent tools (in-loop, `scope="core"`)

| Tool | Purpose |
|---|---|
| `skill_write` | Create a new skill (routes to `dream_create_skill`). Also registered in the dream's skill-extract sub-agent. |
| `skill_publish` | Promote a `skill-drafts/<name>/` draft into the active registry (routes to `publish_draft_skill`). |
| `skill_discard` | Delete a draft under `skill-drafts/<name>/`; never touches the active registry. |
| `skill_edit` | Bounded edit (mode-gated; forks builtins). |
| `skill_search` | Search registries; returns hits and refs. Never installs. |
| `skill_import` | Import from a source through the gate. |
| `skill_audit` | Run the static scan on an installed skill. |
| `skills_list` | List available and quarantined skills. |
| `skill_install_deps` | Install a skill's declared dependencies (dry-run by default; governed by `install_policy`; executes via ExecTool). |
| `skill_observe` | Log live skill feedback to the observation queue. Logs only — no skill is mutated in-session. |

`skill_acquire_seed` declares `_scopes={"dream"}` but is unreachable to the
in-loop agent because `ToolLoader.load` is only called with `scope="core"` or
`scope="subagent"`. The in-loop acquire-on-gap path uses `skill_search` +
`skill_import` + `ask_user_question` instead. The daily skill-extract pass
registers `skill_acquire_seed` directly — it is not loaded via `ToolLoader`.

### Web and API surfaces

`durin/agent/skills_surface.py` exposes the read model (inventory, quarantine,
verdict, review overrides) to the web panel and CLI. Web routes:
`GET /api/v1/skills` (inventory), `GET .../describe` (preview before import),
`POST .../review` (user override for a flagged active skill),
`DELETE .../review` (reopen review), `GET .../observations` (the OPEN
observation backlog behind the panel's count badge, filterable by skill),
`POST .../observations/{id}/resolve` (manual resolution: `applied` or
`declined`). Removal routes trigger `remove_skill` or revert-to-builtin.

---

## 7. Curated rationale

**Single chokepoint over multiple paths.** Create, import, dream-create, and
curation all converge on `skills_store.py` and `GitStore.auto_commit`. This
keeps the git history coherent — every skill's provenance is a commit message
with attribution trailers — and makes the security gate a single code path
rather than a per-caller responsibility.

**Two ramps, one gate.** A one-shot procedure the agent already trusts and a
script that needs a venv and real inputs to prove out have different
authoring shapes, but the same trust requirement once either is about to
become load-bearing: the same composition gate, the same security scan, the
same provenance and attribution. Splitting authoring into two ramps that both
feed into `_finalize_skill` lets the iterative ramp use ordinary, unrestricted
file and exec tools while building — nothing under `skill-drafts/` is trusted
yet, because nothing there is visible to the agent's own retrieval — without
weakening the gate a finished skill must pass to become visible.

**The registry is not a workspace subdirectory.** Before this design, `skills/`
was writable by the same generic tools as everywhere else in the workspace,
which made the security gate only as strong as every caller's discipline in
choosing to route through `skill_write`/`skill_edit` instead of a raw file
write. Refusing the write at the tool layer removes that discipline
requirement: there is no path into `skills/` from a generic tool call, gated or
not.

**Attribute before you quarantine.** A no-provenance skill is quarantined
either way — the security posture does not change based on whether it can be
attributed. But crediting it to the session whose commit introduced it turns
an anonymous quarantine entry into an actionable one: an operator reviewing
quarantine can ask that session what it was doing, instead of treating every
no-provenance skill as equally unexplained.

**Git is the original copy.** There is no separate `original/` directory. The
first commit of an imported or created skill is its canonical original. Rollback
and diff are native git operations. This avoids a second storage layer and keeps
recovery human-readable.

**Gate is in code, not prompt.** `decide_action` is a pure function called in
`install_imported_skill`. The LLM judge is an optional additive layer; the
deterministic rules (dangerous block, code/caution confirm) cannot be overridden
by a prompt or a model output. This reflects the principle that security floors
should not depend on model cooperation.

**Delta-only curation.** `curate_catalog` reviews only skills whose body changed
since the last pass or that have open observations. This means curation cost
is proportional to activity, not catalog size — a stable catalog costs nothing
to run, and a busy day's edits are reviewed without a full re-scan.

**Retrieval tiers match cost to context.** Always-injection is reserved for
skills that are genuinely load-bearing every turn. The hot-tier reduces prompt
size on the stable prefix (cache-friendly). The searchable tier is zero-cost
until the agent queries it. The three tiers together let a large skill library
coexist with a bounded context window.
