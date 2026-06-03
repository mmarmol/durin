# Skill discovery + registries (search) — Design

> **Scope (the user's framing, verbatim intent):** *resolver el search y las
> instalaciones de skills por fuera de nuestro sistema oficial.* NOT a port of
> hermes — we take its **model** (registries are search backends; install always
> goes through one gate) and reuse durin's already-built §6.B gate / §8.C floor /
> provenance store. Three cohesive parts:
>
> - **A — Registry search:** unified `skill_search(query)` across pluggable
>   registry adapters (skills.sh first), surfaced in chat + web + CLI.
> - **B — Gated install *and update*:** every external skill enters (and updates)
>   through §6.B. Rewrites the old clawhub-npx bypass out of existence.
> - **C — Unverified-origin → quarantine:** any skill that reached `skills/`
>   *without* durin provenance (a registry CLI, a manual drop) is relocated to
>   quarantine with an `unverified_origin` finding — inert for the agent until
>   vetted.
>
> **Status:** BUILT (2026-06-03). Parts A (search: skills.sh + clawhub, agent tool,
> CLI, web), B (upstream drift → evolution §8.D), and C (unverified-origin sweep) all
> shipped + live-verified against the real APIs and the real judge. Per-file build
> status in §5; the few pending items are flagged ⏳ there.

---

## 0. The invariant this closes

> **Every skill the agent can invoke is either a shipped builtin, or entered
> through durin's gate (carries `metadata.durin.provenance`). Anything else in
> `skills/` is quarantined as unverified — visible for vetting, never runnable.**

Today there is exactly one hole in that invariant, and it is the thing we are
closing. Verified this session (`grep` over `durin/skills/*/SKILL.md`):

- **clawhub** was the **only** external-install bypass — a builtin that told the
  agent to run `npx --yes clawhub@latest install <slug> --workdir ~/.durin/workspace`,
  dropping un-scanned skills straight into `skills/`. It also did `update --all`
  and `list` out-of-band. **Already deleted** this session (cleanup of inherited
  Nanobot builtins); this spec ensures its capability returns *only* through the
  gate.
- `update-setup` shells `pip install durin-ai` / `git pull` — that upgrades the
  **durin package**, not skills. Out of scope.
- `dream` + `skill_write`/`skill_edit` write to `skills/` without the human gate,
  but that is durin's **own** authoring (§6.A); they stamp `provenance.source =
  dream`/`agent` and are already scanned by the §8.C inventory. Internal, not an
  external install. Out of scope (noted for the radar).

So the real scope is precisely: **search + (install *and* update) gated +
detection of whatever already entered ungated.** With those three, no external
skill path bypasses durin's official system.

---

## 1. Why now / how the field does it (hermes code audit, 2026-06-03)

Read first-hand this session: `/Users/marcelo/.hermes/hermes-agent/tools/skills_hub.py`
(3054 lines, the real resolver behind hermes's `/skills`). The proven shape:

| Piece | Hermes | durin reuse / change |
|---|---|---|
| Adapter interface | `SkillSource` ABC: `search` / `fetch` / `inspect` / `source_id` / `trust_level_for` (skills_hub.py:252) | **Thinner** — durin already has `resolve_candidates` + `fetch_candidate` (= hermes `inspect`+`fetch`). A durin adapter only adds `search(query) → hits`; fetch/gate are reused. |
| skills.sh | `SkillsShSource`: `GET https://skills.sh/api/search?q=&limit=` → `{"skills":[{id,source,skillId,name,installs}]}` (skills_hub.py:941, 969-1001), then resolves the id to the GitHub repo and downloads from there | Same endpoint (**empirically verified in working code** — it is real, just undocumented in the marketing). A skills.sh hit maps to a `github:owner/repo/<skillId>` ref → rides durin's existing GitHub fetch. |
| GitHub registries | `GitHubSource` "taps": a list of `{repo, path}` searched by listing SKILL.md dirs + substring match (skills_hub.py:284, DEFAULT_TAPS = openai/skills, anthropics/skills, VoltAgent/awesome-agent-skills) | Optional `github` adapter using the **same** tap idea; durin's `resolve_candidates("github:owner/repo")` already tree-walks every SKILL.md. |
| ClawHub | `ClawHubSource`: `GET https://clawhub.ai/api/v1/skills?search=&limit=`, own zip artifact store (skills_hub.py:1409). Note in code: *"ClawHavoc incident — 341 malicious skills Feb 2026"* → all community trust | Returns as a **search adapter** (the user's "clawhub-como-search"). NOT GitHub-backed → needs a `clawhub` fetch path. Always out-of-allowlist → always gated. |
| Well-known | `WellKnownSkillSource`: `/.well-known/skills/index.json` (the agentskills.io discovery standard, skills_hub.py:707) | Clean future adapter on the same seam. |
| Trust | hardcoded `TRUSTED_REPOS` (skills_guard) | **durin keeps its user-configurable `allowlist`** ([[feedback_open_over_closed]]). A hit's `ref` hits `decide_action` exactly like a manual import — uniform, no per-registry magic. |
| Provenance / update | `HubLockFile` (lock.json) stores `content_hash`, re-fetches upstream to compare | durin already stamps `provenance.content_hash` + `provenance.source` **inline** in SKILL.md. Update = re-resolve `provenance.source` → compare hash → re-gate. **No separate lock file.** |

**Takeaway:** durin reuses its gate/scanner/store; the genuinely new surface is
**search** (one `search()` per adapter + an orchestrator) plus a small
**update** path and the **unverified sweep**. We are not re-implementing fetch,
trust, dedup, or install — those already exist and are tested.

**Design principle (inherited from the §8.C/§6.B spec, non-negotiable):** clean
skills pass frictionlessly. Search/registries add discovery; they never lower the
gate. A registry is a *place to find* a skill, never a reason to trust it.

---

## 2. Part A — Registry search

### 2.1 The adapter protocol (search-only)

Because durin already resolves + fetches + gates any `ref`, an adapter's sole job
is to turn a query into hits that carry a `ref` the existing pipeline understands.

```python
# durin/agent/skill_registry.py  (new)
@dataclass
class SkillSearchHit:
    name: str
    ref: str            # a source resolve_candidates/fetch_candidate understands:
                        #   "github:owner/repo[@branch]/dir" | "https://…/SKILL.md"
                        #   | "clawhub:<slug>" (native-artifact adapters)
    registry: str       # adapter id: "skills.sh" | "clawhub" | "github" | …
    description: str = ""
    signals: dict = field(default_factory=dict)  # installs/stars/audits — RANKING ONLY,
                                                  # never a trust input

class SkillRegistry(Protocol):
    name: str
    enabled: bool
    def search(self, query: str, *, limit: int) -> list[SkillSearchHit]: ...
```

Adapters split by **fetch path**, and this is the honest cost line:

- **GitHub-backed registries** (skills.sh, `github` taps, well-known-on-GitHub):
  a hit's `ref` is a `github:`/`https:` source → rides durin's **existing**
  `resolve_candidates` + `fetch_candidate`. **Zero new fetch code.**
- **Native-artifact registries** (clawhub: own zip store): need a registry-native
  download into quarantine → a new `clawhub` branch in `fetch_candidate` (mirrors
  the `local`/`https`/`github` branches). Modest, isolated, and the **same**
  §8.C scan + gate apply once the files land in quarantine.

### 2.2 The orchestrator + cross-source merge

**Can we use comparable metrics across sources? No — and hermes doesn't try.**
Audited `unified_search` (skills_hub.py:3033): it runs the sources in parallel,
**extends results in network-completion order** (whichever API answers first leads),
**dedupes by `name`** with `trust_level` as the only tiebreak (builtin > trusted >
community), and truncates. There is **no** cross-source relevance score and **no**
install normalization. Each source's *own* internal ranking (skills.sh server-side,
clawhub's lexical `_search_score`, github substring) is the only ranking that
exists; across sources it is pure concatenation. The escape hatch is the central
`HermesIndexSource` that pre-merges everything offline so only one source is queried
at runtime (skills_hub.py:2973-2993).

**durin's stance: agree — don't synthesize a common metric.** The signals are
incommensurable (skills.sh weekly `installs`, clawhub its own score, github taps
nothing); a fabricated global score would be noise. Trust each adapter's own
ranking (every v1 source already returns a ranked list) and have the orchestrator do
only a cheap, **deterministic** merge:

```python
def search_registries(query, *, registries, limit) -> list[SkillSearchHit]:
    # 1. fan out in parallel (ThreadPoolExecutor) with a per-source timeout;
    #    a slow / failed / rate-limited adapter contributes [] (never raises).
    # 2. dedupe by `ref` (identity — better than hermes's `name`), merging signals.
    # 3. ROUND-ROBIN interleave by configured registry order: take from each source
    #    in turn so no one source floods the top (deterministic — fixes hermes's
    #    network-completion-order bias).
    # 4. float allowlisted refs up (durin's one native trust signal — cheap, meaningful).
    # 5. truncate to `limit`.
```

Per-adapter degradation is mandatory (a down/rate-limited registry contributes
nothing, never raises — matches hermes). If all adapters yield nothing, the result
is an empty list with a short reason, not an error.

So the **only** cross-source signal durin adds is **trust (allowlist)** — a binary
the user controls — not a synthesized popularity/relevance number. `installs` and
friends stay as **display** metadata on each hit ("12k installs · skills.sh"),
informing the human's pick, never a global sort key. Within a source, that source's
ranking stands.

*Scaling note (v2, not now):* hermes's central-index trick is the real fix for "N
live API calls per search." durin gets most of it for free in v1 because
**skills.sh is itself a central index of GitHub skills**; a durin-side aggregated
index is a future optimization, not a v1 need (YAGNI).

### 2.3 v1 adapters

1. **skills.sh** (`kind="skills.sh"`) — `GET https://skills.sh/api/search?q=&limit=`,
   map each `{id:"owner/repo/skillpath", source, skillId, name, installs}` →
   `SkillSearchHit(name, ref=f"github:{source}/{skillId}", registry="skills.sh",
   signals={"installs": installs})`. Empty query → featured (homepage), optional.
   GitHub-backed → **rides the existing fetch**. This is the proof-of-protocol and
   ships first.
2. **clawhub** (`kind="clawhub"`, **v1 — confirmed**) — an **endpoint client modeled
   on hermes `ClawHubSource`**: search `GET https://clawhub.ai/api/v1/skills?search=&limit=`
   (paginated `/skills` catalog fallback, cached); a hit carries `ref=f"clawhub:{slug}"`.
   Fetch resolves the latest version and downloads the skill **zip** from
   `clawhub.ai/api/v1/skills/<slug>/download` into quarantine — the new `clawhub`
   branch in `fetch_candidate` — after which the **same** §8.C scan + gate apply. The
   ClawHavoc note stands (341 malicious skills, Feb 2026): clawhub skills are
   community-trust, never allowlist-eligible by default → **always** hit the confirm
   gate. durin's gate is exactly why integrating clawhub is safe where hermes's
   looser install was not.

**Ready on the same seam (not v1, ~30–40 LOC each):** `github` taps,
`well-known` (`/.well-known/skills/index.json`), lobehub, claude-marketplace —
all present in hermes, all expressible as one `search()` returning github/https
refs.

### 2.4 Surfaces (the search box, the tool, the CLI)

- **Agent tool `skill_search(query, limit=10)`** (new, in `core` toolset next to
  `skill_import`/`skill_audit`): returns the ranked hits. The agent then drives the
  **existing** `skill_import(action="fetch", source=<hit.ref>)` → gate → install.
  Search and install stay separate tools so the gate is never implicit.
- **Web search box** in `SkillsView.tsx`, above the tabs: `GET /api/skills/search?query=&limit=`
  → hit list with a per-hit **Import** button that runs the existing
  resolve→fetch→approve quarantine flow (already built). One new `web_skill_search`
  in `skills_store.py` + one route (GET-with-query, consistent with the rest;
  backlog P7 = POST bodies).
- **CLI `durin skill search <query>`** in `skill_cmd.py` (mirrors the existing
  `audit`/`list`/`quarantine` Typer commands): a results table; install via the
  existing `skill_import` tool / web approve.

### 2.5 Config — "Skill registries"

Registries live under the new `skills.discovery` block (config reorg — §9):

```python
class SkillRegistryConfig(Base):
    name: str                                  # display name
    kind: Literal["skills.sh", "clawhub", "github", "well-known"]
    enabled: bool = True
    api_key_secret: str = ""                   # durin secret NAME; "" → anonymous
    taps: list[str] = Field(default_factory=list)   # kind="github" only: repos to search

class SkillsDiscoveryConfig(Base):             # → skills.discovery (§9)
    registries: list[SkillRegistryConfig] = Field(
        default_factory=lambda: [SkillRegistryConfig(name="skills.sh", kind="skills.sh")])
    search_limit: int = 10
```

Surfaced in the existing **"Skills security"** settings panel (rename to **"Skills:
registries & security"**, or a new sibling section): a registries list (name /
kind / enabled toggle / optional api-key secret picker, reusing the github-token
control pattern). API keys ride durin's existing secrets (`resolve_secret`), never
inline.

---

## 3. Part B — Gated install *and* update

### 3.0 Provenance — what we record (and why it answers updates/checks)

Yes, we already keep the origin. `install_imported_skill` stamps
`metadata.durin.provenance` into the skill's own SKILL.md
(`skills_import.py:321-333`), and that block is precisely what makes update + drift
checks possible:

| field | use |
|---|---|
| `source` | the **fetchable ref** (`github:owner/repo@branch/dir` / `https://…/SKILL.md` / `clawhub:<slug>`) — what `check_upstream_drift` re-resolves (§3.2) |
| `content_hash` | drift baseline — diffed on update to detect upstream change |
| `verdict` / `confirmed` / `overridden` / `replaced` | the gate decision at install |
| `created_at` | when it entered |

**Decided AGAINST (2026-06-03): no `registry` / `registry_id` provenance fields.**
An earlier draft proposed stamping *how it was discovered* (`registry` adapter id +
the registry's own `registry_id`) so a later check could ask the registry for a
newer version. Dropped after verifying the design against code + the live APIs:

- **Update detection is content-addressed, not registry-addressed.** `source` (the
  fetchable ref) + `content_hash` + re-fetch is registry-agnostic — it works for
  github, https, clawhub, and any future registry without per-registry knowledge.
  Stamping per-registry version fields would reintroduce exactly the per-registry
  coupling the content hash eliminates (and `latestVersion` vs `commitSha` vs
  registry-N's-own-field does not generalize).
- **There is no background sync to optimize, by design.** Curation only re-fetches
  the narrow `auto` + `source=="workspace"` set, and only the change-gated `delta`
  (`needs_curation`) — see `curate_catalog` (`skill_curation.py`). Imports are
  stamped `mode="manual"` and **never** enter that delta. So no skill is re-fetched
  on a schedule; a "cheap registry version check" has no consumer. A daily
  fetch-everything sync is explicitly NOT something durin does or wants.
- **Two intrinsic archetypes, each with a native cheap primitive already in code**
  — should an on-demand, per-skill `update` ever be built (re-resolve `source`,
  re-fetch *that one* skill; pending, low priority):

  | archetype | natural "version" | cheap native check |
  |---|---|---|
  | index-over-git (skills.sh, direct github, https) | git commit / content | github commit SHA via the API — uniform across all github sources |
  | package registry (clawhub) | semver | `_clawhub_latest_version(slug)` — already exists (`skills_import.py`) |

A skills.sh skill's `provenance.source` is a **github** ref because skills.sh is an
*index over git*, not a host — it points at the repo and disappears after discovery.
clawhub's `source` is `clawhub:<slug>` because clawhub *hosts* the versioned zip.
That asymmetry is intrinsic to what each is, not an inconsistency to paper over with
extra provenance fields. A skill's declared `version` already lives in its
frontmatter (`list_skills_info` reads it); we do not duplicate it into provenance.

### 3.1 Install (reuse — nothing new)

Search → pick a hit → `skill_import(action="fetch", source=hit.ref)` →
`fetch_candidate` (quarantine + §8.C scan, caps, optional judge) → human gate
(`decide_action`/`install_imported_skill`) → installed with
`metadata.durin.provenance{source, verdict, content_hash, …}`. This path is fully
built and tested; the only new code is the search→pick handoff (§2.4).

### 3.2 Upstream drift → evolution (§8.D) — REVISED + BUILT (2026-06-03)

> The original plan was a `skill_update` command that re-imports and **replaces**
> the local skill. Wrong for durin: `auto` skills **evolve locally** (dream's
> `curate_catalog`), so a replace would destroy local work. Reframed with the user
> and BUILT: drift is a SIGNAL that feeds dream's evolution, never an overwrite. No
> imperative command (deferred — nice-to-have).

A gated skill carries `provenance.source` + `provenance.content_hash`.
`check_upstream_drift` (`durin/agent/skill_drift.py`) re-resolves the source,
re-fetches + §8.C-scans into a drift quarantine, and compares the hash:

- unchanged → no drift.
- changed → the §8.D gate (`decide_action`):
  - `allow` (safe + no code + allowlisted) → the daily dream pass (`curate_catalog`)
    feeds the upstream body to the judge, which **incorporates it via `evolve`** —
    a surgical old/new that PRESERVES local edits (validated live with the real model).
  - `confirm`/`block` (carries code / caution / out-of-allowlist / dangerous) →
    logged for human review; **never auto-merged**.

The local skill is **never replaced**, only evolved; the upstream is never trusted
because the local one was. Inline provenance (no `lock.json`) is durin's lock-file
equivalent. Drift rides the existing delta of curated real-repo skills; with an
empty allowlist (default) all drift → human (conservative).

### 3.3 clawhub rewrite

The npx-install builtin is already deleted. Its three jobs map cleanly onto the
official system: `search` → the clawhub **search adapter** (§2.3); `install` → the
gate (§3.1); `update --all` → upstream drift→evolution in the dream pass (§3.2);
`list` → the inventory surface (already built). No builtin skill returns; clawhub
lives only as a gated registry adapter.

---

## 4. Part C — Unverified-origin → quarantine

### 4.1 The behavior (converged)

A workspace skill in `skills/` **without** `metadata.durin.provenance` reached the
filesystem outside every durin path (a registry CLI like the old clawhub-npx, or a
manual copy). On detection:

1. **Move** `skills/<name>/` → `.durin/import-quarantine/<name>/`.
2. Run the §8.C `scan_skill` and **prepend an `unverified_origin` finding**
   (severity `caution`) whose detail explains the danger, verbatim target:
   > *"In `skills/` without durin provenance — it entered outside the security
   > gate (a registry CLI or a manual copy). Its instructions and any bundled
   > code were never scanned or approved; it could exfiltrate data or manipulate
   > the agent. Audit and approve to use it, or reject it."*
3. Write the `.scan.json` (source `"unverified:workspace"`, the merged verdict =
   `max(scan, caution)`, findings) so it lands in the **existing** quarantine
   surface with zero new UI.

It is now **inert for the agent for free**: `SkillsLoader` enumerates only
`workspace/skills/` + builtins (verified: `skills.py:79-84`, `load_skill` reads
only those two roots). Relocated out of `skills/`, it cannot enter
`memory_search(kind="skill")`, the hot working-set tier, or the `skills_catalog`
injection — no retrieval filter needed. Approve → re-gate (scan/judge/human) →
back in `skills/` with provenance (the **existing** `install_imported_skill` path,
`source="unverified:workspace"` → out-of-allowlist → confirm gate). Reject →
delete.

### 4.2 The precise rule (no false positives)

> Relocate **iff** the skill is under `workspace/skills/` **and** its frontmatter
> has no `metadata.durin.provenance`.

Verified safe against every durin authoring path — each stamps provenance, so none
trips the rule:

| Path | provenance.source | stays available? |
|---|---|---|
| shipped builtin | (builtin dir; not in `skills/`) | ✅ always |
| gated import (§6.B) | the import ref | ✅ |
| `fork_on_write` (builtin → workspace) | `builtin:<name>` | ✅ |
| `dream_create_skill` / `skill_write` | `dream` | ✅ |
| `dream_fuse_skills` | `dream` | ✅ |
| **clawhub-npx / manual drop** | **(none)** | ⛔ → quarantine |

A regression test asserts the invariant the rule depends on: **every** durin
write-path stamps `metadata.durin.provenance`. If a future path forgets, the sweep
would quarantine legitimate skills — the test catches it at the source.

### 4.3 Trigger points

One idempotent, cheap (dir scan + frontmatter read) sweep:

```python
def sweep_unverified_skills(workspace) -> list[str]:  # returns relocated names
```

Called: (1) once at **agent/session startup**, before the first context build, so
the agent never loads an ungated skill; and (2) at the top of `skills_inventory` /
`quarantined_skills`, so every management surface (web, CLI, chat) is consistent
the moment it is opened. Idempotent: a second call finds nothing to move.

### 4.4 Trade-off (accept + record)

The sweep **mutates the filesystem** (relocates the skill dir). Justified: it is a
security action, fully reversible (approve → returns to `skills/`), and explained
in the finding. The alternative (leave it in place + a runtime retrieval filter)
is *more* code and leaves the dangerous file one bug away from being loaded.
Relocation makes "inert" a property of where the file *is*, not of every reader
remembering to filter.

---

## 5. File map

> **Build status (2026-06-03, verified against code + live):** ✅ = built this
> session. Discovery search + drift→evolution + Part C (unverified sweep) all
> shipped. (⏳ = the few items still pending, below.)

**New**
- ✅ `durin/agent/skill_registry.py` — `SkillSearchHit`, `SkillRegistry` protocol,
  the v1 adapters (skills.sh, clawhub), `search_registries` orchestrator.
- ✅ `durin/agent/skill_drift.py` — upstream drift detector (§3.2/§8.D), report-only,
  wired into `curate_catalog`. (Replaces the old `skill_update`-replace idea.)
- ✅ `durin/agent/skill_lifecycle.py` — `sweep_unverified_skills(workspace)` (Part C).
- ✅ `durin/agent/tools/skill_search.py` — the `skill_search` agent tool.

**Modify**
- ✅ `durin/agent/skills_import.py` — the `clawhub` branch in `fetch_candidate`.
  (`registry`/`registry_id` provenance fields — decided AGAINST, §3.0: update
  detection stays content-addressed via `source` + `content_hash`, no per-registry
  version fields, no background sync to optimize.)
- ✅ `durin/agent/skill_curation.py` + `durin/templates/agent/skill_curation.md` +
  `durin/cli/commands.py` — drift incorporation into the dream pass (§3.2/§8.D).
- ✅ `durin/agent/skills_surface.py` — `sweep_unverified_skills` at the top of
  `skills_inventory` / `quarantined_skills` (Part C). The `unverified_origin`
  finding is created directly by the sweep (no `skill_scan.py` change needed —
  `Finding.category` is free-form).
- ✅ `durin/agent/context.py` — `ContextBuilder.__init__` runs the sweep before the
  loader enumerates (the startup hook — makes an ungated skill inert before the
  agent can use it).
- ✅ `durin/agent/skills_store.py` — `web_skill_search`.
- `durin/agent/tools/skill_import.py` — add the `update` action.
- `durin/config/schema.py` — the reorg (§9): new `skills: SkillsConfig`
  {`discovery`, `security`}; `SkillRegistryConfig` + `SkillsDiscoveryConfig`; rename
  `SkillImportConfig` → `SkillSecurityConfig`; move `SkillsHotTierConfig` onto
  `AgentDefaults.skills_hot_tier`; a root `model_validator(mode="before")` for legacy
  `memory.skill_import` / `memory.skills_hot_tier` back-compat.
- `durin/agent/context.py` — read `agents.defaults.skills_hot_tier` (threaded in like
  `disabled_skills`), was `memory.skills_hot_tier`.
- the `memory.skill_import` consumers (`skill_resolve.py`, `skills_store.py`,
  `tools/skill_import.py`, `tools/skill_audit.py`) → `skills.security` /
  `skills.discovery` (§9).
- `durin/channels/websocket.py` — `GET /api/skills/search`; `/api/skills/{name}/update`.
- `durin/cli/skill_cmd.py` — `durin skill search`; `durin skill update`.
- `webui/src/components/SkillsView.tsx` — search box + results + Import; the
  unverified-origin entries render in the existing Cuarentena tab with their
  finding (no new tab).
- `webui/src/components/settings/SkillsSecuritySettings.tsx` — registries list.
- `webui/src/lib/api.ts` — `searchSkills`, `updateSkill`, registry config types.
- the agent/session startup path — call `sweep_unverified_skills` before first
  context build.

---

## 6. Decisions (settled with the user, 2026-06-03)

1. **Scope = search + install/update gated + detection of ungated** — not a hermes
   port. clawhub is the only external bypass; it is gone and returns only as a
   gated search adapter.
2. **Model = hermes's `SkillSource` shape, code = durin's.** Keep durin's gate,
   §8.C floor + opt-in judge, **user-configurable allowlist** (not hardcoded
   `TRUSTED_REPOS`), inline provenance (not `lock.json`).
3. **Registries are search backends only.** Install/update *always* through §6.B.
4. **v1 adapters: skills.sh (first; GitHub-backed, rides existing fetch) +
   clawhub (search; needs the `clawhub` fetch branch).** Others ready on the seam.
5. **skills.sh API is `GET /api/search?q=&limit=`** — verified in hermes's working
   code, used as-is; the adapter degrades gracefully (empty on any error) since the
   endpoint is undocumented and may change.
6. **Update = upstream drift → evolution (§8.D), REVISED + BUILT (2026-06-03).**
   Not a replace — `auto` skills evolve locally, so drift is a SIGNAL:
   `check_upstream_drift` + the §8.D gate; `allow` → dream incorporates via `evolve`
   (never overwrites local edits), `confirm`/`block` → human. No imperative
   `skill_update` command (nice-to-have). See §3.2.
7. **Unverified origin → quarantine with an `unverified_origin` finding** (not a
   third "invalid" status, not a runtime filter). Inert because relocated out of
   `skills/`; surfaced in the existing quarantine; approve re-gates, reject deletes.
   Rule: `source==workspace AND no provenance`. Trigger: startup + inventory load.
8. **`unverified_origin` finding severity = `caution`** (confirmed with the user) —
   forces a confirm on re-approval; the deterministic scan still raises to dangerous
   on its own.
9. **No synthesized cross-source metric.** Trust each adapter's own ranking; the
   orchestrator does a deterministic round-robin merge + allowlist float + dedup by
   `ref`. `installs` etc. are display-only, never a global sort key (§2.2).
10. **Config namespace reorg (§9), approved.** `skills.*` holds only **global** skill
    governance: `skills.discovery.registries` + `skills.security` (← `memory.skill_import`).
    Per-agent skill-context tuning stays on `agents.defaults`: `disabled_skills` plus
    `skills_hot_tier` (moved off `memory.skills_hot_tier` to sit with it).
    `memory.index_skills` stays (really memory).

11. **clawhub is in v1** (confirmed) — integrated as an endpoint client like hermes
    (`clawhub.ai/api/v1` search + zip fetch), always through the gate. skills.sh
    still ships first (near-free, GitHub-backed); clawhub follows as the
    native-artifact adapter (validating the `clawhub` fetch seam).

**Open for review:** none — all decisions settled 2026-06-03.

---

## 7. Test plan / acceptance

- **Clean-skills principle (non-negotiable, inherited):** re-run the whole pipeline
  over the clean corpus (durin builtins + the real hermes/openclaw skills) — not one
  clean skill escalates, and not one gated/dream/builtin skill is swept to
  quarantine.
- **Adapter unit tests** with mocked HTTP: skills.sh `/api/search` JSON → hits with
  correct github refs; non-200 / timeout → `[]` (graceful); clawhub zip → quarantine
  + scan.
- **Orchestrator:** merge + dedupe by ref; one failing adapter doesn't sink the rest.
- **Unverified sweep:** a no-provenance workspace skill is relocated + gets the
  finding + is absent from the loader's `list_skills`/`build_skills_summary`/always
  set; a provenance-stamped one is untouched; idempotent on a second call; approve
  returns it to `skills/` with provenance; reject deletes it.
- **Update:** unchanged upstream → no-op; changed upstream → re-scan + gate + replace,
  new content_hash stamped.
- **Provenance-stamping regression:** assert every durin write-path stamps
  `metadata.durin.provenance` (guards the sweep rule).
- **Live (green unit ≠ working):** drive the real webui — search box → results →
  Import → quarantine → approve → active; drop a no-provenance skill in `skills/`,
  restart, confirm it shows in Cuarentena with the unverified-origin reason and the
  agent cannot invoke it.

---

## 8. Implementation order (when approved)

1. **Part C — unverified sweep** (smallest, independently shippable, biggest
   security win): `sweep_unverified_skills` + the `unverified_origin` finding +
   startup/inventory wiring + the provenance-stamping regression test.
2. **Part A — skills.sh adapter + orchestrator** + `skill_search` tool + the web
   search box + CLI + config.
3. **Part A — clawhub adapter** (the `clawhub` fetch branch validates the
   native-artifact seam).
4. **Part B — upstream drift → evolution (§8.D)** — `check_upstream_drift` +
   incorporation into the dream `curate_catalog` (reframed from the original
   `skill_update`-replace idea; never overwrites local edits — §3.2).
5. Registries settings panel; live verification; backend + webui suites green.

*(All of the above shipped + live-verified 2026-06-03; see the §5 build status.)*

(Task 0 — the §9 config reorg — lands first as a standalone refactor; everything
above then reads the new `skills.*` / `agents.defaults.skills_hot_tier` paths.)

---

## 9. Config namespace reorg — prerequisite refactor

The "where do registries live?" question exposed that the whole `memory.skill_import`
home is wrong. Audited every skill-touching config field and placed each by **the
subsystem that owns the behavior + the field's scope (global vs per-agent)** — and by
the user's line: what is *really* memory stays.

| Field today | Reader / scope | Verdict |
|---|---|---|
| `memory.index_skills` | `durin/memory/*`; gates the memory **index** scope (global) | **STAYS** in memory — really memory |
| `memory.skills_hot_tier` | `agent/context.py`, `skill_usage.py`; per-agent **context composition** | **MOVES → `agents.defaults.skills_hot_tier`** (see below) |
| `memory.skill_import` | skill tools / store / resolve; **global** skill governance | **MOVES → `skills.discovery` + `skills.security`** |
| `agents.defaults.disabled_skills` | loop / subagent / loader; per-agent skill loading | **STAYS** on `agents.defaults` |

**Why `skills_hot_tier` → `agents.defaults` (not `skills.*`):** the hot tier is
per-agent context composition (how many recent/frequent skills *this* agent injects,
with recency/frequency windows). It is the same *kind* of knob as its neighbors in
`agents.defaults` (`max_messages`, `consolidation_ratio`, `preemptive_compact_ratio`)
and, decisively, as `disabled_skills` — also a per-agent skill-in-context knob.
Routing `skills_hot_tier` to `skills.*` while `disabled_skills` stays on
`agents.defaults` would **split the two per-agent skill-context knobs across two
homes** — the incoherent outcome. They belong together. So `skills.*` is reserved for
**global** skill governance (discovery + security); per-agent skill-context tuning
lives on `agents.defaults`.

**Target shape:**

```python
class SkillsConfig(Base):                 # GLOBAL skill governance only
    discovery: SkillsDiscoveryConfig      # registries + search_limit (§2.5)
    security:  SkillSecurityConfig        # ← memory.skill_import (renamed): allowlist /
                                          #   caps / github_token_secret / install_specs / llm_judge

# root Config: add `skills: SkillsConfig` (peer of agents / memory / channels / …)
# memory KEEPS `index_skills`
# agents.defaults KEEPS `disabled_skills` and GAINS `skills_hot_tier`
```

**Migration (no broken configs):** a root `@model_validator(mode="before")` maps the
legacy paths and warns once — `memory.skill_import` (registries → `skills.discovery`,
the rest → `skills.security`) and `memory.skills_hot_tier →
agents.defaults.skills_hot_tier`. Then update the verified consumers: the
`memory.skill_import` readers (`skill_resolve.py:82`, `skills_store.py:450/458/467`,
`tools/skill_import.py:97/101`, `tools/skill_audit.py:100`) → `skills.security` /
`skills.discovery`; `context.py:224` → `agents.defaults.skills_hot_tier` (threaded in
like `disabled_skills` already is, not a fresh `load_config()`); plus the `/api/config`
webui keys + `SkillsSecuritySettings.tsx` + `api.ts`.

**Standalone prerequisite:** pure config hygiene + back-compat; lands *before* the
discovery feature, which then writes `registries` into `skills.discovery` from the
start.
