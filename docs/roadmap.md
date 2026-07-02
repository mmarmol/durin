# Roadmap & Pending Work

> Single source of truth for direction and open work: where Durin is, where it's
> going, what it deliberately won't build, and what's still pending.
>
> **Discipline:** when a pending item ships, delete it from here — the closing
> commit is the canonical record (`git log` finds it by message). Partial progress
> is updated in place. Detailed history (refuted experiments, the day-by-day
> implementation log) lives in `git log`; as-built internals live in
> `docs/internals/`.

---

## Current state (2026-06-22)

Durin is a Nanobot baseline + a full entity-centric memory system + daily-driver
lifecycle + capability bridges (vision/audio) + secrets + an HTTP/WebSocket service
platform + web/TUI parity.

**Shipped subsystems** (as-built detail in `docs/internals/`):

- **Memory** (entity-centric): 5 tools (`memory_search`, `memory_upsert_entity`,
  `memory_ingest`, `memory_drill`, `memory_forget`) over FTS5 + LanceDB + grep + RRF
  + entity-aware ranker + optional cross-encoder rerank. Default embedding
  `intfloat/multilingual-e5-small`. Dream consolidator + entity pages + auto-absorb
  (opt-in). Obsidian-compatible workspace. Skills are a searchable memory
  pseudo-class + hot working-set tier. LoCoMo ≈ 0.79.
- **API platform**: transport-agnostic service core (`durin/service/`) behind a
  unified Starlette + uvicorn ASGI front door (`durin/api/`), pydantic-derived
  OpenAPI contract, persisted auth. All mutations are POST/DELETE with JSON bodies
  (the old GET-with-query handshake surface is gone).
- **MCP**: best-in-class client (fidelity, reconnect/circuit-breaker, OAuth,
  SSRF/injection guards, server→client roots/logging/sampling) + registry
  discovery/install (web + `durin mcp` CLI + TUI).
- **Skills**: discovery + preview, import-time vetting (AST + OSV dependency scan +
  multilingual detectors), install-deps tool gated by exec policy, active-skill
  review override.
- **Model picker**: provider-first picker (web + TUI), fuzzy multi-section, vendored
  capability metadata.
- **TUI/webui polish**: agent-activity cluster, prompt history, error cards, toasts,
  "Esc to stop" hint, file-picker button. (All 7 phases of the former
  TUI/webui-improvements roadmap shipped.)

**SOUL.md**: a single generic-engineering SOUL (`durin/templates/SOUL.md`),
auto-synced to the workspace on bootstrap — captures the token-efficiency effect
without a role router (see Horizon 1a).

---

## Direction

### Horizon 1a — Role-based SOUL.md routing — REFUTED (2026-05-19)

V9e (107 exercises × 3 conditions) showed no correctness signal beyond the noise
floor. What survives as a real effect is **token efficiency**: any non-empty SOUL
cuts median output tokens 3–5× at equal correctness — and that comes from *any*
SOUL, not from role-to-task matching. So we ship a single generic SOUL and do
**not** build the router. (Full V9e stats in `git log`.)

### Horizon 1b — Per-query dynamic context

Query-conditioned context retrieval (Aider-style repo map, Cursor @-refs, Hermes
skill-doc retrieval). Mostly converged with Horizon 2: `memory_search` already
retrieves per query, and skill-doc retrieval shipped (skills as a searchable memory
pseudo-class). The one piece still **open** is *codebase-aware* retrieval — a
PageRank-style repo map over the user's code (not over memory). Not prioritized
today; tracked under Pending work.

### Horizon 2 — Memory system — SHIPPED

Entity-centric pages + classes + LLM-driven dream consolidator + opt-in auto-absorb.
The final shape differs from the original "5 node types with milestone promotion"
design. See `docs/internals/memory/`.

---

## What we are explicitly NOT doing

Tested, or strong reasons against. (Detailed experiment analysis in `git log`.)

- ❌ Posture vector (5-axis dynamic behavioral state)
- ❌ Plan tiers / phases / forced verification gate / cycle escalation
- ❌ Deliberation V3 (single-call multi-perspective, same model)
- ❌ Phase-aware temperatures
- ❌ Self-verification / self-review loops (same model)
- ❌ Pre-completion Critic (without a genuinely different model)
- ❌ Role-based SOUL.md router (tested; no correctness gain) — the efficiency win
  is captured by a single default SOUL with no routing
- ❌ Temporal decay in `memory_search` ranking (removed) — search must be
  faithful retrieval, not pre-judge what the LLM should decide

---

## Decision rules

1. **No component without empirical or industrial precedent.** "It seems like it
   should help" is not enough.
2. **Mechanisms must demonstrably activate in realistic tests.** If the main code
   path never runs, the component is overhead.
3. **Distrust same-model self-verification.** Need ground truth (tests) or a
   different model.
4. **Specificity > abstraction.** "Be cautious" doesn't change behavior; concrete
   rules do.
5. **3+ trials minimum** for any quantitative claim.
6. **Test in regimes where the baseline can fail.** Ceiling-effect scenarios prove
   nothing.
7. **Search is the product.** Durin is memory; search-misses are the primary bug,
   everything else is detail.
8. **Search is faithful retrieval.** Rankings come from query-derived signals, not
   from heuristics that pre-judge what the LLM should decide.

---

## Pending work

### Worth doing

**Skill execution hardening (P6 #2 / #3).** The import gate is install-time, and
install-deps already runs through the exec gate (#1, shipped). Open: (#2) run a
skill's *bundled* scripts through Durin's exec gate; (#3) a real per-skill
FS/network sandbox. (#3 is a large v2 — measure need first.)

**Skill file editor — broaden validation + highlighting.** Save-time syntax lint
(`durin/agent/skills_store.py::_lint_script`) now covers `.py`/`.sh` plus the
config formats `.json`/`.toml`/`.yaml` (in-process parsers, no new deps). View
highlighting (`webui/.../SkillsView.tsx`) still covers only `.py`/`.sh`. Open:
(1) extend the View extension→language map (Prism already supports js/ts/json/yaml/…) —
trivial, zero risk; (2) optional `.js` lint via `node --check`, degrading gracefully
when the interpreter is absent (mirror the `bash -n` best-effort pattern).

**Workflows: auto-mode self-improvement + auto-merge.** Two open pieces in the
workflow engine (`docs/internals/workflow.md`). (1) Self-improvement currently
only proposes recommendations for a human to review and apply (`manual` mode);
`auto` mode (apply directly) needs an external validation anchor — some signal
independent of the workflow's own gates — so an automatic edit can't win by
quietly loosening the criteria it's judged against. (2) Parallel `union`
reconcile currently aborts a run when two writing branches touch the same file
with different content; an auto-merge step (diff-and-combine, or an LLM
merge judge) would resolve the common case without a human in the loop, falling
back to today's abort when the merge itself is ambiguous.

**Unified GitHub credential section + connect UX.** GitHub recurs across skills
(`skills.security.github_token_secret`), MCP discovery (`mcp_discovery.github_token_secret`), the
GitHub MCP server, and the Copilot OAuth provider — each configures its token in its own place.
Consolidate into one neutral `config.github` credential consumed by all, with a webui "Connect
GitHub" section: OAuth **device flow** (reuse the working Copilot path in
`durin/providers/github_copilot_provider.py` — public client_id, no secret; needs a durin-owned
GitHub OAuth App with `repo`/`read:user` scope), a guided PAT (deep link to GitHub's
scope-prefilled token page) as fallback, and `gh` CLI passthrough. Skills + MCP keep their
per-feature key for now and migrate to read `config.github` once it lands; contextual "Connect
GitHub to improve results" buttons in skills/MCP deep-link there. Design spec pending.

### Deferred / no trigger

- **`list_dir` recursive perf** — switch the pure-Python walk to `os.scandir` (no
  deps, always testable) only if large recursive trees become a real bottleneck
  (`durin/agent/tools/filesystem.py`). Not `fd` (not installed by default; would
  ship untested in CI).
- **Horizon 1b codebase-aware retrieval** — PageRank-style repo map over the user's
  code. Needs a design before any build.
- **Extra skill discovery adapters** — github-taps / well-known / lobehub sources
  beyond the current registries.
- **TUI/webui "deferred" ideas** (high value, high investment): full diff viewer,
  command palette, sidebar panels, which-key popup, prompt stash, frecency `@file`
  ranking, latency footer, model variant/effort picker.

---

## Last updated: 2026-06-22 (removed shipped MCP-catalog item; refreshed state date)
