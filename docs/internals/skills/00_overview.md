# Skills — as-built architecture overview

> **What this is.** The single as-built reference for durin's skills subsystem: what
> it does *today*, how the pieces fit, and where each lives in code. Companion:
> [`01_format_and_interop.md`](01_format_and_interop.md) (the SKILL.md format contract).
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
copy; git *is* that layer.

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

Six capabilities: **create · import · discover ·
acquire · evolve · remove**. The first five converge on the same versioned `adapted` skill
in the git subtree; **remove** (§3a) is their inverse — the only mutation that takes a skill
out of the workspace.

---

## 3. Creation

**In-loop (the agent, mid-session).** Core tools `skill_write` (new) and `skill_edit`
(bounded edit; a `manual` skill needs `confirm=true`, a builtin forks into the workspace
first). Both route through `skills_store.py` (provenance + commit). The in-session
prompt (`templates/agent/skills_section.md`) also drives **acquire-on-gap** — see §5.

**The skill-extract pass (autonomous).** `durin/memory/dream_passes.py::run_skill_extract_pass`
→ `_skill_extract_async` is the skills arm of the daily `memory_dream` cron (§6). Its input
(`_skill_extract_messages`) is the most recent **sessions** (`max_sessions`, default 3 —
`_recent_sessions_text`, capped at ~12k chars) **plus the OPEN `new:*` gap observations**
(§6a) rendered as a `LOGGED GAPS` block — distilled, pre-named skill candidates the agent
flagged while working, so extraction can run even on a day with no sessions. It runs a
sub-agent (`durin/agent/runner.py::AgentRunner`, `max_iterations=8`) whose system prompt
(`_SKILL_EXTRACT_PROMPT`) asks it to author a skill **only when a reusable, recurring
multi-step procedure appears**, reusing/extending an existing skill rather than duplicating
it, complying with the active cross-cutting principles (§6a), and using a gap's working
name **verbatim** — after the run, `_resolve_gap_observations` marks any gap whose name
materialized as APPLIED. The sub-agent is given a minimal toolset — `ReadFileTool`,
`EditFileTool`, and `SkillWriteTool` (NO registry/acquire tools). When it calls `skill_write`
the write routes through `skills_store.py::dream_create_skill` (provenance `source=dream`,
`mode=auto`, committed). On a quiet day with no sessions and no gaps the pass returns early
(`reason="no_sessions"`) and never calls the LLM.

This is the **create** path. See §6 for the single `memory_dream` cron and its passes.

### 3a. Removal (remove / revert-to-builtin)

The inverse of import — `skills_store.py::remove_skill`, the mirror of
`install_imported_skill`. Like every mutation it routes through the one chokepoint and is a
git commit, so a removal is recoverable from history. It **only ever** operates on the
workspace dir (`<workspace>/skills/<name>/`); the package builtins (`durin/skills/`) are
never touched. `skills_store.py::removable_action` classifies the target into three cases —
the single source of truth surfaced as `removable` on each inventory row
(`skills_surface.py::skills_inventory`) so the web panel and CLI offer the right action:

| Case | Condition | `removable` | Effect |
|---|---|---|---|
| Imported / dream / fused | workspace dir exists, no builtin of same name | `remove` | skill disappears |
| Forked builtin | workspace dir exists + a builtin of the same name | `revert` | workspace copy deleted → shipped builtin reappears |
| Builtin (pure) | no workspace dir | `null` | refused — the package must not be touched |

**Index side effect.** Both cases call `_unsync_index` (FTS row dropped by uri, vector row
by id). This is correct for *revert* too: only workspace skills are indexed
(`walk_skills` / `reindex_one_skill` are workspace-only — builtins are never in the search
index), so dropping the fork's row restores the builtin's pre-fork (un-indexed) state. No
builtin re-index is needed. This matches the uniform `_unsync_index` in `dream_fuse_skills`.

**Surfaces (no agent tool — by design).** Removal is a destructive admin action, so it is
*not* exposed as an LLM-callable tool. It is reachable from: the web panel
(`GET /api/skills/{name}/remove` → `web_skill_remove`; a button in the skill detail pane with
an inline confirmation), the CLI (`durin skill remove <name> [--yes]`), and the chat command
(`/skills remove <name>`). Every path appends a `.durin/import-audit.log` entry
(`action="remove"`, `result=remove|revert`).

---

## 4. Discovery & acquisition (§6.A discovery, §6.C acquire-on-gap)

**Registries / search.** `durin/agent/skill_registry.py` defines `SkillSearchHit`, the
`SkillRegistry` protocol, and two adapters: `SkillsShRegistry` (skills.sh → github-backed
refs `github:owner/repo/skill`) and `ClawHubRegistry` (clawhub → `clawhub:slug`, its own
versioned zip store). ClawHub search hits the **ranked** `GET /api/v1/search?q=` endpoint —
*not* `GET /api/v1/skills`, which is a recency LIST that silently ignores its query (calling
it returns the same recently-updated skills for every search). `search_registries` queries
adapters in parallel (SSRF-safe), dedupes by ref, round-robin interleaves (rank-fair across
sources — the lead source rotates per query via a stable `crc32`, so no registry permanently
owns the top slot), floats allowlisted refs first. The web UI surfaces this merged order as
its default **relevance** sort, so a registry that reports no install count (clawhub) is not
buried under install-ranked skills.sh hits; each result line carries a source tag (icon +
registry name). `build_adapters` wires skills.sh + clawhub, **both enabled by default**
(github-taps/well-known/lobehub are roadmap). Exposed as the `skill_search` core tool, plus
CLI (`durin skill search`) and web. skills.sh hits carry **no** synthetic description in
search results (the search API returns none) — the real one is fetched on preview.

**Preview / detail (`describe`).** `skills_store.py::web_skill_describe`
(`GET /api/v1/skills/describe?ref=`) is a read-only peek used by the web UI before import:
it resolves the ref like import does, reads just that one SKILL.md (github/https via raw
URL; clawhub via the registry's `GET /api/v1/skills/{slug}/file?path=SKILL.md` raw-file
endpoint), and returns its full `description` (≤1024 chars), `body` (the markdown after the
frontmatter), `platforms`, and declared `requires` (bins/env). It never executes or writes anything and degrades to empty
fields on any failure. The webui renders this as a per-result detail view (description +
rendered body + requirements) so the user can decide before importing into quarantine; the
§8.C verdict still appears in the triage pane after import.

**Acquire-on-gap (§6.C)** — durin's own initiative to acquire a skill when it lacks one.
Search is the seed; the gate (§7) is enforced. Both paths (spec
`2026-06-03-skill-acquire-on-gap-design.md`) are BUILT + live-verified:
- **Path A — in-session, interactive.** Prompt guidance in `skills_section.md`: when
  local skill search (`memory_search kind=skill`) misses on a recurring/non-trivial
  workflow, `skill_search` the registries; to reuse a hit, `skill_import(action=fetch)`
  (runs the §8.C gate); if clean → `skill_write`; if risky → present candidates to the
  user via `ask_user_question`. A human approves anything risky.
- **Path B — autonomous, safe-only.** The daily `memory_dream` **skill-extract pass**
  hosts it: `dream_passes.py::_build_skill_extract_tools` hand-registers `skill_search`
  + `skill_acquire_seed` alongside the authoring tools, and `_SKILL_EXTRACT_PROMPT`
  drives the flow — `skill_search` a candidate → `skill_acquire_seed` its ref → adapt +
  `skill_write`, else author from scratch. The seed gate is in code:
  `skill_acquire_seed.py::SkillAcquireSeedTool` (`_scopes={"dream"}`, so it never loads
  into the in-loop core agent) → `skill_acquire.py::acquire_safe_seed`, which **rejects a
  non-allowlisted ref without a download** (fast), fetches + statically scans allowlisted
  refs (**LLM judge never used** — `judge_trigger="off"`), and returns a seed **only if
  `decide_action == "allow"`** (risk enforced in code). With the default empty allowlist
  nothing auto-seeds → the dream authors from scratch (conservative). **Live-verified
  2026-06-06:** a real skill-extract run called `skill_search → skill_acquire_seed →
  skill_write`. (History: Path B first shipped in the 2h Dream's phase-2; the
  entity-centric migration deleted that host and orphaned `skill_acquire_seed` until it
  was re-homed here.)

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

**Static scan internals (`skill_scan.py` + `skill_ast.py`).** Bundled Python scripts also
get a stdlib-AST behavioral pass that flags dynamic-execution call shapes. `compile` is
rated **caution**, not dangerous: it produces a code object but does not execute —
execution needs a subsequent `exec`/`eval`, which is flagged dangerous on its own (so
`exec(compile(...))` stays dangerous). This keeps pure syntax-checkers / linters (e.g.
`skill-creator/scripts/quick_validate.py`) from tripping the gate as false positives.

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
| **curate_catalog** | **EVOLVES** skills: reviews the change-gated delta, applies `evolve`/`fuse`/`retire`. Appended as the dream's last step. | `cli/commands.py` (memory_dream branch) → `skill_curation.py::curate_catalog` |

(The `extract` / `refine` / `always_on` passes are entity-memory consolidation, documented
in the memory architecture docs; they don't touch skills.)

**Evolution — `curate_catalog`.** Reviews **only** the change-gated delta:
`mode=="auto"` AND `source=="workspace"` skills (dream-created + forks; never pristine
builtins) that `needs_curation` (body changed since last pass) — **plus** any auto
workspace skill with an OPEN observation (§6a), even if its body is unchanged. An LLM
judge proposes `evolve` (surgical old/new on the local body), `fuse` (merge
near-duplicates), `retire` (delete a fully-obsolete skill via `remove_skill`,
git-recoverable — the only path to drop a skill from the catalog vs. evolving its
body toward empty), `principle`, or `retire_principle` actions; applied via
`skills_store.py` / `skill_observations.py` (committed). Budget-capped per day with
carry-over — it **never "reviews everything,"** only the delta. Imported skills are
`mode=manual` and are **not** auto-curated.

### 6a. Observation queue & cross-cutting principles (the evidence channel)

The task-observer pattern, adopted 2026-06-10 — capture live feedback about
skills the moment it occurs, queue it with states, and let the daily curation
judge consume it as evidence (the cheap alternative to GEPA-style measured
optimization, which needs an eval harness and per-run budget). This section is
the as-built reference. Code: `durin/agent/skill_observations.py`.

**Capture (in-loop, live).** The `skill_observe` core tool logs feedback the moment it
occurs — `kind`: `correction` (user corrected output produced under a skill), `gap` (no
skill covers a recurring procedure; `skill="new:<working-name>"`), `improvement`, or
`simplify` (a rule/section is dead weight). The structural trigger is a per-turn block in
`templates/agent/identity.md` ("Skill observations") — the tool description alone is a
weak signal. **Log, don't act:** nothing mutates a skill in-session.

In addition to the agent's voluntary calls, an `improvement` observation is logged
**automatically** whenever the `skill_edit` *tool* applies an edit to an `auto` skill in
the loop (`tools/skill_edit.py`): a direct edit is itself an improvement signal, so it
feeds the queue without depending on the agent also remembering to call `skill_observe`.
Curation edits (`apply_skill_edit` with `actor="curation"`) do not go through the tool, so
they are not re-logged; manual-mode edits only return a proposed diff (not applied) and so
log nothing.

**Capture (hindsight, at dream time).** Because the agent rarely calls `skill_observe`
mid-task (judging whether a correction *generalizes* is hard in the moment), the queue is
also fed in hindsight: stage 3 of the extract dream (`skill_signals_enabled`, default ON)
runs `discover_skill_signals` (`durin/agent/skill_signals.py`) over each session's
post-cursor turns — the same turns the entity `discover_entities` stage uses, but
**tail-windowed** (`turns[-12000:]`, not head): a correction lands at the END of an
interaction, so the most recent turns matter (live-verified — head-truncation missed
corrections in a long session). A focused
LLM call detects `correction`/`gap` signals (attributed via the turn-indexed
`skill_calls`, `skill_usage.py`) and logs them as observations. This is the skill
analogue of memory's `discover_entities`: the agent creates by initiative, the dream
discovers in hindsight. Detection only — `count`/recurrence still gates what curation
acts on; `memory.dream.skill_signals` telemetry measures precision.

**Store.** `<workspace>/skills/.observations.jsonl` in the skills GitStore (every change
committed). Write-time dedup: a near-same issue for the same skill bumps `count` /
`last_seen` instead of appending — `count >= 2` is the recurrence signal. States:
`OPEN → APPLIED | DECLINED` (set by curation dispositions). APPLIED records get one
pass of visibility, then `archive_resolved` moves them to
`.observations.archive.jsonl` at the start of the next pass; DECLINED records stay in
the active file as the judge's memory against re-proposing rejected changes.

**Consumption (curation).** The judge receives OPEN observations for the skills under
review (plus `skill:"all"` records) and the compact DECLINED history, with instructions:
recurring (`count>=2`) records license `evolve`; one-offs stay `keep` unless trivially
safe; `simplify` licenses removal. It answers every shown record with a disposition
(`applied`/`declined`/`keep`) → `apply_dispositions`. Observations on `manual` skills or
pristine builtins stay OPEN untouched (manual = user-owned; builtins join the evolving
set only once forked). `new:*` records never reach curation — they are skill-extract
input (§3).

**Principles.** A generalizable lesson (typically a recurring `skill:"all"` observation)
can be promoted by the judge via a `principle` action into
`<workspace>/skills/.principles.jsonl` (capped at `PRINCIPLES_CAP=12`;
`retire_principle` frees slots). Active principles are injected as a compliance
checklist into BOTH the curation prompt (non-compliant skills get evolved) and the
skill-extract prompt (new skills are born compliant).

**Telemetry.** The cron summary line logs `obs_applied/obs_declined/obs_kept/obs_open` +
`principles` per run (`cli/commands.py`); the skill-extract pass emits `gaps_closed`.

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

**Active-skill review overrides ("Revisada").** A flagged *active* skill can be cleared by
a user or the LLM judge without mutating the package (builtins are read-only).
`durin/security/skill_reviews.py` stores reviews in `.durin/skill-reviews.json`, keyed by a
content hash **and** the set of acked finding fingerprints: a review is valid only while
the content is unchanged AND no new finding appeared — either a content edit or a
newly-detected finding (e.g. a scanner upgrade) re-opens it. `skills_inventory` surfaces a
`review` block; the deterministic verdict/findings are **preserved, not mutated**, and the
webui shows a "Revisada" chip in place of the verdict badge. Two paths clear a skill: the
websocket judge (`_run_skill_audit` on an active skill → `skills_store.record_review_from_judge`,
recorded only when the judge does **not** confirm dangerous) and an explicit user override
(`POST /api/v1/skills/{name}/review` → `web_skill_review_user`); `DELETE …/review`
(`web_skill_unreview`) re-opens it.

---

## 8. Runtime dependency install (P6 #1)

A skill can declare OS/package dependencies (`metadata.<vendor>.install: [{kind, …}]`).
Historically info-only (policy `never`). P6 #1
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
  through the tool gate; #3 = per-skill FS/net sandbox — both pending, see `docs/roadmap.md`.)

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
| `skill_observe` | core | Log live skill feedback (correction/gap/improvement/simplify) to the observation queue (§6a). Log, don't act. |
| `skill_acquire_seed` | **`dream`** (skill-extract pass) | Gated per-ref seed retrieval for autonomous acquisition (returns risk-free seeds only). `_scopes={"dream"}` keeps it out of the in-loop core agent; hand-registered in the daily skill-extract pass (`_build_skill_extract_tools`) — see §4 Path B. |

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
memory class + hot-tier (Spec-2) · acquire-on-gap **Paths A + B** (in-session + autonomous
skill-extract, §6.C; Path B re-homed + live-verified 2026-06-06) · runtime install
executor (P6 #1) · observation queue + cross-cutting principles (§6a, task-observer
pattern, 2026-06-10) · removal: remove / revert-to-builtin (§3a; web + CLI + chat, no
agent tool, 2026-06-11).

**Pending (active):** P6 #2 (run bundled skill *scripts* through the tool gate) · P6 #3 (per-skill FS/net sandbox) ·
extra discovery adapters (github-taps / well-known / lobehub). See `docs/roadmap.md`.

**Discarded (decided against):**
adapt-to-native-tools / GEPA-SkillOpt optimizer (no value over `curate_catalog` +
usage signal too sparse for personal skills) · a separate `original/` layer (git already
provides it) · per-registry `registry`/`registry_id` version provenance (update detection
stays content-addressed).
