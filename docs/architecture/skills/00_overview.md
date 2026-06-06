# Skills — as-built architecture overview

> **What this is.** The single as-built reference for durin's skills subsystem: what
> it does *today*, how the pieces fit, and where each lives in code. Companion:
> [`01_format_and_interop.md`](01_format_and_interop.md) (the SKILL.md format contract).
> Vision & roadmap (north star + what's deferred/discarded): `docs/archive/skills_evolutivas.md`.
> Design rationale per feature: `docs/superpowers/specs/2026-06-*-skill-*-design.md`.
>
> Citations are **file + symbol** (stable across edits); grep the symbol to land on it.

---

## 1. The model: a skill is a versioned plugin

A skill is a directory `skills/<name>/` whose `SKILL.md` carries open-standard
frontmatter ([agentskills.io](https://agentskills.io)) plus a durin namespace
(`metadata.durin.*`). Skills can bundle `scripts/`, `references/`, `assets/`.

Two locations:
- **Builtin** — shipped with durin (`durin/agent/skills.py::BUILTIN_SKILLS_DIR`). The
  stable seed; never re-curated/forked until copied into the workspace.
- **Workspace** — `<workspace>/skills/`, a **git subtree** managed by
  `durin/utils/gitstore.py::GitStore`. This is where created/imported/evolving skills live.

**Every mutation goes through one chokepoint** — `durin/agent/skills_store.py` — and
**every mutation is a git commit** (`GitStore.auto_commit`). So a skill's full history
(create → edits → fuse) is in git: rollback = `git revert`, "why" = the commit message,
and **the original (as-imported / as-created) is the first commit** — never lost,
always diffable (`git diff <import-commit>..HEAD`). There is no separate `original/`
copy; git *is* that layer (see `skills_evolutivas.md §5.2`).

### Frontmatter durin reads (`metadata.durin.*`)

| Field | Meaning | Read in |
|---|---|---|
| `mode` | `manual` (user-owned, hand-edited) or `auto` (dream-evolved). Default by origin: builtin→auto, user→manual. | `skills_store.py::read_mode` |
| `provenance` | `{source, content_hash, verdict, created_at, …}` — where it came from + the gate decision at install. | stamped in `skills_import.py::install_imported_skill`, `skills_store.py::dream_create_skill` |
| `always: true` | **Force the full SKILL.md body into the system prompt every turn** ("Active Skills"). Max incentive to use. | `skills.py::get_always_skills`, `context.py` |
| `disable_model_invocation: true` | Hide from the model's catalog (still loadable by code). | `skills.py::build_skills_summary` |
| `requires.{bins,env}` | Availability gate: a skill is "unavailable" if a required CLI/env is missing. | `skills.py::_check_requirements` |

Top-level frontmatter (name, description, `install`, `platforms`, …) is the open
standard — see `01_format_and_interop.md`.

---

## 2. Lifecycle (the whole arc)

```
                 ┌──────────── create (own experience) ──────────┐
 user/agent ──►  │  in-loop: skill_write / skill_edit            │
                 │  dream skill-extract pass: mine sessions →     │──► skills/<name>/  ──► retrieval
 registries ──►  │    skill_write (recurring procedure)          │     (git subtree)      (hot-tier +
                 │  import (§6.B) → §8.C gate → install           │         │               searchable)
                 │  acquire-on-gap (§6.C): search → safe seed     │         │
                 └───────────────────────────────────────────────┘         │
                                                                            ▼
                                  memory_dream cron, curate_catalog pass: curate (evolve/fuse) + drift (§8.D)
```

Five capabilities (vision §1, `skills_evolutivas.md`): **create · import · discover ·
acquire · evolve**. All converge on the same versioned `adapted` skill in the git subtree.

---

## 3. Creation

**In-loop (the agent, mid-session).** Core tools `skill_write` (new) and `skill_edit`
(bounded edit; a `manual` skill needs `confirm=true`, a builtin forks into the workspace
first). Both route through `skills_store.py` (provenance + commit). The in-session
prompt (`templates/agent/skills_section.md`) also drives **acquire-on-gap** — see §5.

**The skill-extract pass (autonomous).** `durin/memory/dream_passes.py::run_skill_extract_pass`
→ `_skill_extract_async` is the skills arm of the daily `memory_dream` cron (§6). It mines
the most recent **sessions** (`max_sessions`, default 3 — `_recent_sessions_text`, capped at
~12k chars) and runs a sub-agent (`durin/agent/runner.py::AgentRunner`, `max_iterations=8`)
whose system prompt (`_SKILL_EXTRACT_PROMPT`) asks it to author a skill **only when a
reusable, recurring multi-step procedure appears**, reusing/extending an existing skill
rather than duplicating it. The sub-agent is given a minimal toolset — `ReadFileTool`,
`EditFileTool`, and `SkillWriteTool` (NO registry/acquire tools). When it calls `skill_write`
the write routes through `skills_store.py::dream_create_skill` (provenance `source=dream`,
`mode=auto`, committed). On a quiet day with no sessions the pass returns early
(`reason="no_sessions"`) and never calls the LLM.

This is the **create** path. See §6 for the single `memory_dream` cron and its passes.

---

## 4. Discovery & acquisition (§6.A discovery, §6.C acquire-on-gap)

**Registries / search.** `durin/agent/skill_registry.py` defines `SkillSearchHit`, the
`SkillRegistry` protocol, and two adapters: `SkillsShRegistry` (skills.sh → github-backed
refs `github:owner/repo/skill`) and `ClawHubRegistry` (clawhub → `clawhub:slug`, its own
versioned zip store). `search_registries` queries adapters in parallel (SSRF-safe),
dedupes by ref, round-robin interleaves, floats allowlisted refs first. `build_adapters`
wires only skills.sh + clawhub today (github-taps/well-known/lobehub are roadmap). Exposed
as the `skill_search` core tool, plus CLI (`durin skill search`) and web.

**Acquire-on-gap (§6.C)** — durin's own initiative to acquire a skill when it lacks one.
Search is the seed; the gate (§7) is enforced. Two paths (spec
`2026-06-03-skill-acquire-on-gap-design.md`). Path A (in-session) is BUILT + live-verified;
Path B's safe-seed primitives exist but are **not wired into the current dream** (see below):
- **Path A — in-session, interactive.** Prompt guidance in `skills_section.md`: when
  local skill search (`memory_search kind=skill`) misses on a recurring/non-trivial
  workflow, `skill_search` the registries; to reuse a hit, `skill_import(action=fetch)`
  (runs the §8.C gate); if clean → `skill_write`; if risky → present candidates to the
  user via `ask_user_question`. A human approves anything risky.
- **Path B — autonomous, safe-only (not currently wired).** The safe-seed primitives
  exist in the tree: `durin/agent/tools/skill_acquire_seed.py::SkillAcquireSeedTool`
  (gated per-ref, **dream-scoped** `_scopes={"dream"}`) → `durin/agent/skill_acquire.py::acquire_safe_seed`,
  which **rejects a non-allowlisted ref without a download** (fast), fetches + statically
  scans allowlisted refs (**LLM judge never used here** — `judge_trigger="off"`), and
  returns a seed **only if `decide_action == "allow"`** (risk enforced in code, so an
  autonomous caller can never receive risky content). But the **only** consumer of the
  `dream` tool scope was the old two-phase dream's phase-2 toolset, deleted in the
  entity-centric migration. The current skill-extract pass (`_skill_extract_async`)
  hand-registers only `ReadFileTool` / `EditFileTool` / `SkillWriteTool` and `ToolLoader.load`
  is never called with `scope="dream"`, so today the dream **authors from scratch** — it
  does not auto-seed from registries. Re-wiring this into the skill-extract sub-agent is
  the path that would revive Path B (spec `2026-06-03-skill-acquire-on-gap-design.md`).

---

## 5. Import & security floor (§6.B + §8.C)

`durin/agent/skills_import.py` + `durin/security/skill_scan.py` + `skill_resolve.py`
implement a uniform, source-agnostic import that **everything** (manual import, discovery
install, acquire-on-gap, drift) funnels through:

```
resolve_candidates(source)        # github: / clawhub: / https:// / local → candidate(s)
  → fetch_candidate(...)          # download into .durin/import-quarantine/<name>/ (size/file caps, zip-slip safe)
  → validate_skill + scan_skill   # §8.C STATIC scan: regex on body + install specs + carries_code; verdict safe|caution|dangerous
  → decide_action(...)            # the gate (below)
  → install_imported_skill(...)   # re-scans fresh, enforces the gate IN CODE, stamps provenance, commits
```

**The §8.C gate — `decide_action(source, *, verdict, carries_code, allowlist)`:**
- `verdict == dangerous` → **block** (needs explicit override).
- `carries_code` OR `verdict == caution` OR source **not allowlisted** → **confirm**
  (needs confirmation).
- else (safe + no code + allowlisted) → **allow**.

So with the default **empty allowlist**, every external skill needs confirmation — durin
is conservative by construction. The `allowlist` (user config) only loosens the *source*
check; the code/dangerous gates have no opt-out. An optional LLM judge
(`skills.security.llm_judge.trigger`, default `off`) can add a semantic layer on top of
the static scan; it is opt-in.

**Quarantine & lifecycle.** Fetched skills land in `.durin/import-quarantine/` until the
gate passes; `reject_quarantined` discards. `durin/agent/skill_lifecycle.py::sweep_unverified_skills`
("Part C") relocates any workspace skill that reached `skills/` **without** durin
provenance (a registry CLI, a manual drop) back to quarantine — inert for the agent,
surfaced for the human. It runs at `ContextBuilder.__init__` and in the surfaces, so an
ungated skill can't be used before the agent even starts.

---

## 6. The dream (how skills work in the background)

durin has **one dream cron** — `memory_dream` (`durin/cli/commands.py`, the `on_cron_job`
handler's `memory_dream` branch; registered as a system job with schedule `memory.dream.cron`,
default `0 3 * * *`). It also fires on two reactive triggers (`post_compaction`,
`on_session_close`) that run the extract pass only. The daily cron runs its passes **in
order** — `run_extract_pass` (sessions → entity attributes) → `run_skill_extract_pass`
(sessions → skills) → `run_refine_pass` (entity dedup) → `run_always_on_pass` (distill the
always-on pin) → `curate_catalog` (skill evolution). Two of those passes touch skills:

| Pass | What it does for skills | Code |
|---|---|---|
| **skill-extract** | **CREATES** skills: mines the most recent sessions; a sub-agent calls `skill_write` when a recurring multi-step procedure appears (provenance `source=dream`, `mode=auto`, committed). See §3. | `dream_passes.py::run_skill_extract_pass` → `dream_create_skill` |
| **curate_catalog** | **EVOLVES** skills: reviews the change-gated delta, applies `evolve`/`fuse`. Appended as the dream's last step. | `cli/commands.py` (memory_dream branch) → `skill_curation.py::curate_catalog` |

(The `extract` / `refine` / `always_on` passes are entity-memory consolidation, documented
in the memory architecture docs; they don't touch skills.)

**Evolution — `curate_catalog`.** Reviews **only** the change-gated delta:
`mode=="auto"` AND `source=="workspace"` skills (dream-created + forks; never pristine
builtins) that `needs_curation` (body changed since last pass). An LLM judge proposes
`evolve` (surgical old/new on the local body) or `fuse` (merge near-duplicates) actions;
applied via `skills_store.py` (committed). Budget-capped per day with carry-over — it
**never "reviews everything,"** only the delta. Imported skills are `mode=manual` and are
**not** auto-curated.

**Drift → evolution (§8.D) — `skill_drift.py::check_upstream_drift`.** Wired into
`curate_catalog`: for a skill whose `provenance.source` is a real repo, re-resolve +
re-fetch + §8.C-scan; if the content changed and the gate says `allow`, the upstream body
is fed to the curation judge to **incorporate via `evolve`** (never overwrites local
edits); `confirm`/`block` → left for human review. With the default empty allowlist, all
drift → human (conservative).

---

## 7. Retrieval & surfacing (how skills reach the model)

Three tiers, all in `durin/agent/context.py` + `skills.py`:
1. **`# Active Skills` — forced full-body injection.** Skills flagged `always: true` have
   their entire SKILL.md injected into the stable system prompt every turn
   (`skills.py::get_always_skills` + `load_skills_for_context`). Heaviest, most-incentivized.
2. **Hot-tier catalog — name+desc+path.** The most-used working set (`skill_usage.py::compute_working_set`,
   config `agents.defaults.skills_hot_tier`) is listed (one line each) in the
   always-rendered `templates/agent/skills_section.md`; the agent reads full bodies on
   demand via `read_file`. Excludes the `always` set and `disable_model_invocation` skills.
3. **Searchable catalog — the rest.** Skills are indexed as a memory class
   (`memory.index_skills`, default on; `durin/memory/skill_page.py::SkillPage`, FTS + vector)
   and found via `memory_search kind=skill`. `skills_section.md` instructs the agent to
   search before concluding no skill exists.

`durin/agent/skills_surface.py` exposes the inventory (+ verdict/findings) and the
quarantine to CLI/web; usage signal (`skill_usage.py`) drives the hot-tier (it does **not**
drive curation — that's deliberate).

---

## 8. Runtime dependency install (P6 #1)

A skill can declare OS/package dependencies (`metadata.<vendor>.install: [{kind, …}]`).
Historically info-only (policy `never`). P6 #1 (plan `docs/archive/skills-plans/2026-06-04-skill-install-deps-p6.md`)
adds an **approved executor**:
- `skills_import.py::runnable_install_specs` turns *safe* specs into shell commands
  (brew/apt/pip/cargo/npm/go/uv), **dropping** any spec the §8.C scanner flagged
  `dangerous`, **excluding** the `download` kind, and flagging `needs_privileges` (apt).
- `durin/agent/tools/skill_install_deps.py` (core tool) is **dry-run by default**: it
  lists the commands; `confirm=true` runs them. Governed by `skills.install_policy`
  (`never` | `approve` | `auto`, default `approve`). It **runs each command through
  durin's single exec gate `ExecTool`** (allow/deny patterns + sandbox + logging) — not a
  side-channel subprocess — mirroring hermes's one-gate model. Sudo is never injected;
  privileged commands are surfaced for the user. (P6 #2 = run a skill's bundled *scripts*
  through the tool gate; #3 = per-skill FS/net sandbox — both pending, `docs/backlog.md`.)

---

## 9. Agent tools (inventory)

| Tool | Scope | Purpose |
|---|---|---|
| `skill_write` | core | Create a new skill (→ `dream_create_skill`). Also hand-registered into the dream's skill-extract sub-agent (§3). |
| `skill_edit` | core | Bounded edit of an existing skill (mode-gated; forks builtins). |
| `skill_search` | core | Search registries; returns hits + refs (never installs). |
| `skill_import` | core | Import from a source through the §8.C gate (fetch→scan→gate→install). |
| `skill_audit` | core | Run the §8.C scan on a skill; verdict + findings. |
| `skills_list` | core | List available + quarantined skills. |
| `skill_install_deps` | core | Install a skill's declared deps (dry-run→confirm, policy, via ExecTool). |
| `skill_acquire_seed` | **`dream`** (not loaded) | Gated per-ref seed retrieval for autonomous acquisition (returns risk-free seeds only). `_scopes={"dream"}`, but no agent loads the `dream` scope today — see §4 Path B. |

Tools default to `core` scope (auto-loaded into the in-loop agent) unless they declare
`_scopes` (`durin/agent/tools/loader.py::ToolLoader.load`). `skill_acquire_seed` declares
`{"dream"}`, but `ToolLoader.load` is only ever called with `scope="core"` (in-loop) or
`scope="subagent"`, so that tool is currently unreachable — the in-session agent uses the
raw `skill_search` / `skill_import` + `ask_user_question` (a human approves risky candidates)
instead, and the dream's skill-extract sub-agent gets only Read/Edit/`skill_write` (§3).

---

## 10. Config (`skills.*`, `config/schema.py`)

- `skills.security` — `allowlist` (trusted source prefixes), `github_token_secret`, size
  caps, `llm_judge` (trigger off|uncertain|always, default off).
- `skills.discovery` — `registries` (skills.sh, clawhub), `search_limit`.
- `skills.install_policy` — `never` | `approve` | `auto` (default `approve`) for P6 #1.
- `agents.defaults.skills_hot_tier` — hot-tier sizing (recent/frequent windows).
- `memory.dream` — the single `memory_dream` cron (`cron`, default `0 3 * * *`;
  `post_compaction`/`on_session_close` reactive triggers; `max_seconds_per_run`) that runs
  the skill-extract pass and carries `curate_catalog`.
- `memory.index_skills` — whether skills are indexed as a searchable memory class (default on).

---

## 11. Status (built / deferred / discarded)

**Built & live-verified:** versioning + modes (E1) · dream skill-extract authoring
(E2 Part A; `run_skill_extract_pass`) · daily catalog curation `curate_catalog` (E2 Part B) ·
interop standard (§8.B) · import + §8.C security floor (§6.B/§8.C) · discovery/registries (§6.A) ·
upstream drift→evolution (§8.D) · unverified-origin sweep (Part C) · retrieval: searchable
memory class + hot-tier (Spec-2) · acquire-on-gap Path A (in-session, §6.C) · runtime install
executor (P6 #1).

**Pending (active):** acquire-on-gap Path B (autonomous dream seed) — safe-seed primitives
(`skill_acquire_seed` / `acquire_safe_seed`) exist but the `dream` tool scope is no longer
loaded by any agent, so re-wiring into the skill-extract sub-agent is needed (§4 Path B) ·
P6 #2 (run bundled skill *scripts* through the tool gate) · P6 #3 (per-skill FS/net sandbox) ·
extra discovery adapters (github-taps / well-known / lobehub). See `docs/backlog.md`.

**Discarded (decided against, with rationale in `skills_evolutivas.md`):** §6.D
adapt-to-native-tools / §8.F GEPA-SkillOpt optimizer (no value over `curate_catalog` +
usage signal too sparse for personal skills) · a separate `original/` layer (git already
provides it) · per-registry `registry`/`registry_id` version provenance (update detection
stays content-addressed).
