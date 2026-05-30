# Roadmap

> Forward plan after empirical refutation of the previous "smart layer" direction. See `bitacora.md` for what was discarded and why.

---

## Current state (post-T1, 2026-05-23)

Durin is a clean Nanobot baseline + plumbing + the now-shipped **entity-centric memory system** (Phases 0-6 of doc 19 + T1 wiring W1-W4 of archived doc 24):

- Plumbing additions (`local_llama_provider`, multi-channel work, generic telemetry skeleton)
- Empirically validated execution: basic ReAct loop + tools + sandbox + sessions
- No active "smart layer" (posture, plan tiers, deliberation V3 all removed)
- **Entity-centric memory** shipped + wired + verified:
  - Typed entities `type:value` in episodic entries
  - LLM dream consolidator that produces `memory/entities/<type>/<slug>.md` pages with git history
  - AliasIndex (rebuild-only, lazy)
  - Vector index (LanceDB) with entity_page rows + `entities` field
  - `memory_search` invokes entity-aware RRF ranker when query matches a known alias
  - `durin memory` CLI: dream, history, show, diff, revert, expand, absorb, absorb-suggest
  - 4365 tests passing, 16 skipped

Forward planning and deferred items: see [archive/36_post_t1_state_and_t2_horizon.md](archive/36_post_t1_state_and_t2_horizon.md).

---

## Direction: two horizons backed by industry evidence

Both directions have strong empirical or industrial precedent, unlike what we built before.

### Horizon 1a — Role-based SOUL.md routing — REFUTED (2026-05-19)

**Status**: closed. V9e ran 107 exercises × 3 conditions (none / specific / generic_agent), `max_tokens=131072`, glm-5.1. Pass rates: 69.2% / 71.0% / 73.8% — gap of 4.6pp within the noise floor (±4.4pp for N=107). The 23 divergent exercises distribute uniformly across the 6 possible patterns (chi² = 1.78, df=5, p≫0.05), and sign-test per-condition gives p=0.41–0.68 — **statistically indistinguishable from random model variance**. Error types are nearly identical across conditions (28/30/25 AssertionError, 1/1/2 setup errors). Jaccard similarity of fail-sets is 0.57–0.61 — most failures are shared difficulty, not differentiation. See `bitacora.md` and `06_log_experiments.md` for full V9e analysis.

**What initially looked promising**:
- Aider's published +33–41pp on GPT-4
- PartialOrderEval +58pp on HumanEval (arxiv 2508.03678)
- Hermes Agent's +40% speedup with skill-doc loading
- Our V9 v1: +20pp specific vs none (later shown to be a `max_tokens=4096` artifact in V9d)

**What survives as a real effect**:
- **Token efficiency**: SOUL ≠ ∅ reduces median output tokens 3–5× and reasoning chars 2.84× vs no SOUL, at identical correctness. This is robust across V9d and V9e.
- The benefit comes from **any SOUL**, not from matching role-to-task. A single generic engineering SOUL captures the effect without a router.

**Why we are NOT building the router**:
- No correctness signal in the regime where our model sits (frontier reasoning, 1M context)
- The "lyrics → none / structure → generic" anecdotal pattern (N=4/3/5 in divergent cases) is well within Bernoulli noise
- A router adds infrastructure (classifier LLM call, fragment library, integration) for an effect that wasn't measured

**What we DO carry forward**:
- Set a single generic-engineering SOUL as default in Durin's `ContextBuilder`. Cheap, captures the efficiency gain, no routing.
- The divergence-pattern hypothesis remains technically open but very low expected value — confirming it would require N≥50 repeated trials per (exercise, condition) to lift signal above noise, which costs money for no actionable downstream.

---

### Horizon 1b — Per-query dynamic context (Aider-style retrieval)

**What this is**: context-specific information pulled from the workspace or prior conversation, packed into the user message or system prompt at the start of a turn — not a SOUL.md fragment, but *information relevant to this exact question*.

**Examples in production**:
- Aider's PageRank repo map (which symbols/files are most relevant to this query)
- Cursor's @-references and codebase indexing
- Hermes Agent's skill-doc retrieval by task similarity
- Any RAG layer at the agent boundary

**Hypothesis**:
For tasks with a non-trivial codebase or knowledge base, query-conditioned context retrieval improves outcomes beyond what a static SOUL can provide. Where SOUL routing answers "what role should the agent be?", per-query context answers "what specific information does this query need?".

**Why kept separate from 1a**:
- Different mechanism (categorical routing vs procedural retrieval)
- Different signal (task type vs specific content)
- Different storage (small fragment library vs full codebase index or memory graph)
- This converges naturally with Horizon 2 (memory): retrieval ARGUMENTS the dynamic context from past steps/milestones. The architectural pattern is the same — only the source differs (codebase vs experiential memory).

**Sequencing note**:
Horizon 1b can be implemented standalone (Aider-style repo map), but is more naturally built as a consumer of the Horizon 2 memory system once that exists. We'll revisit after 1a delivers a result.

---

### Horizon 2 — Memory system (graph-based, dynamic projection)

**Evidence base**:
- Hermes Agent skill loop (solve → document → reuse): **+40% speedup**, validated in production
- Aider's PageRank repo map: validated by adoption and benchmark results
- Reflexion (academic): episodic failure memory measurably improves recovery
- Doc 03 design predates our experiments; the design is internally consistent and aligns with these patterns

**Hypothesis to validate during construction**:
A persistent graph with role-typed nodes (session, goal, pending, step, milestone) + dynamic projection biased by relevance → better cross-task and cross-session performance than session-scoped context alone.

**What to build** (see `03_memory_design.md` for the full design):
- Five node types per Doc 03 §3.2
- Importance-based milestone promotion at step exit
- Dynamic context projection (which milestones enter the active window)
- Cross-session persistence with decay

**Refinements based on industry research**:
- Adopt Hermes's pattern of *agent-written skill documents* alongside agent-written milestones
- Consider Aider's PageRank-style relevance ranking for code-related milestones
- Reflexion-style explicit failure-pattern tracking

**Decision rule for memory features**:
Each subcomponent must demonstrate measurable lift before we layer more on top. We will NOT build the full graph and then check if it works — we'll build minimum-viable retrieval (Hermes-style flat skill docs), measure, then incrementally add structure.

---

## What we are explicitly NOT doing

These have been tested or have strong reasons against. See `bitacora.md` for full rationale.

- ❌ Posture vector (5-axis dynamic behavioral state)
- ❌ Plan tiers / phases / forced verification gate / cycle escalation
- ❌ Deliberation V3 (single-call multi-perspective in one model)
- ❌ Phase-aware temperatures
- ❌ Self-verification / self-review loops (same-model)
- ❌ Pre-completion Critic (without genuinely different model)
- ❌ **Role-based SOUL.md router** (refuted V9e, 2026-05-19) — no correctness signal beyond noise; efficiency gain captured by single default SOUL without routing

---

## Sequencing

**Phase 1a — closed (refuted 2026-05-19)**. See section above. Action item: set a single generic-engineering SOUL as Durin's default to capture the efficiency gain.

**Phase 1c (Tool I/O hygiene + telemetry — current focus, May 2026)**:
SWE-agent's central NeurIPS 2024 finding — *tool I/O quality matters more than loop quality* — is the empirically strongest direction we haven't measured in Durin. Both candidates (windowed file viewer, capped search) are already implemented in our `read_file` and `grep` tools at permissive defaults. What's missing is per-call telemetry to know if the defaults are sized correctly for 1M-context frontier models or if information is silently dropped.

Steps:
1. Instrument `read_file` and `grep` with per-call JSONL events: params, file/result sizes, truncation flags, follow-up patterns ✅ done
2. Collect baseline over real workloads (V9e re-run with telemetry on; longer agentic sessions)
3. Decide if defaults need tightening (toward SWE-agent's 100-line / 50-result limits) or are already correct
4. **No defaults change without supporting data** — the cost of dropping information the model needs is asymmetric vs the token savings

External agents review (May 2026): see `archive/34_external_agents_review.md` for code-level analysis of OpenHands, Hermes Agent, OpenCode, OpenClaude. That review surfaced concrete tool/loop adoption candidates with explicit weighing.

**Sprint A — Tool I/O hygiene (completed 2026-05-19)**: `repo_overview` tool, `read_file` suggestion-on-miss, block-anchor matcher in `edit_file`, `exec` output spill to disk. All 4 quick-wins shipped with telemetry. 35 new tests, full suite at 3,102.

**Sprint B — Permission-as-data agent modes (completed 2026-05-19)**: `/plan`, `/build`, `/mode` slash commands work in every channel via the shared `CommandRouter`. `enter_plan_mode` / `exit_plan_mode` tools for the LLM. Read-only filtering in the runner. Telemetry covers turn-start mode, mode switches, tool denials, and plan presentation. 51 new tests, full suite at 3,153. See `docs/ARCHITECTURE.md` §"Sprint B" for the per-channel autocomplete improvements that remain as polish (CLI completer, Telegram BotCommand registration).

**Subsequent pivot (May 2026)**: archive subsystem we'd been building was found redundant with `session.json` (which is now confirmed immutable, since TTL/autocompact was removed). Replaced with per-session `<safe_key>.meta.json` sidecar — single file per session indexing lifecycle events (plans today, extensible by `type` for future patterns). See `docs/bitacora.md` §"Pivot: session immutable + per-session meta file".

---

### Tools roadmap (May 2026 — 12-item plan)

Compiled from the comparative review of OpenHands / Hermes / OpenCode / OpenClaude tools (`archive/34_external_agents_review.md`) filtered through Marcelo's priorities. Each row is independent unless dependencies are noted. Order reflects: trivial-first, then user-flagged high-interest, then multimodal chain, then heavyweight investments, then memory-foundation skills.

| # | Tool | Complexity | Value | Adopters | Depends on | Rationale |
|---|---|---|---|---|---|---|
| 1 | **TodoWrite** ✅ (2026-05-19) | LOW (1d) | High | All 4 (OpenHands, Hermes, OpenCode, OpenClaude) | — | Shipped as `todo_write`. Replaces full list each call. Echoed in runtime context. Allowed in plan mode. See bitácora. |
| 2 | **Sleep** ✅ (2026-05-19) | LOW (~2h) | Low-Medium | OpenClaude | — | Shipped as `sleep`. Bounded 0–300s; clamps over-asks; telemetry start/end. |
| 3 | **AskUserQuestion** ✅ (2026-05-19) | LOW-MED (2d) | High | OpenClaude, Hermes (`clarify`) | — | Shipped as `ask_user_question`. V1 yield-and-resume (no in-turn block); stores `pending_question` on session metadata for channel rendering. |
| 4 | **session_search** ✅ (2026-05-19) | LOW (2d) | Medium | Hermes | — | Shipped as `session_search`. Keyword/regex over `session.messages`, role filter, snippet around match, msg_index pointer. Allowed in plan mode. |
| 5 | **Subagent lifecycle expansion** ✅ (2026-05-19) | MED (~1 week) | High | OpenClaude (`TaskCreate/Get/Update/List/Stop/Output`), OpenCode, OpenHands | — | Shipped 4 tools: `subagent_list`, `subagent_status`, `subagent_stop`, `subagent_output`. Session-scoped security, LRU status retention, allowed in plan mode. |
| 6 | **Monitor** ✅ (2026-05-19) | LOW-MED (2-3d) | Medium | OpenClaude | #5 | Shipped as `subagent_monitor`. Cursor-based diff polling (`after_event` → `next_cursor`); finished output bundled when task completes. |
| 7 | **Cron extension** ✅ (2026-05-19) | LOW-MED (2-3d) | Medium | Hermes, OpenClaude | — | List + remove already existed. Added `action='update'`: rename, change message, swap schedule, toggle delivery. Requires ≥1 actual change. |
| 8 | **RemoteTrigger** | MED (3-5d) | Medium-High | OpenClaude | — | Launch agent run from external webhook. Requires HTTP endpoint + queue plumbing. |
| 9 | **Vision tool** (capability bridge to aux model) ✅ (2026-05-19) | MED (~1 week) | High | Hermes (`vision_analyze`), OpenClaude (`browser_vision`), OpenHands (browser get_state) | capabilities snapshot + aux_providers | Shipped as `interpret_image`. Config-gated (only registers when `aux_models.vision` is set); routes one-shot questions to the aux LLM with OpenAI-compat `image_url` block. Verified E2E (glm-5.1 primary → glm-5v-turbo aux). |
| 9b | **Audio bridge** (capability bridge to aux model) ✅ (2026-05-20) | MED | High | new — out-of-scope of original 12-list | capabilities snapshot + aux_providers | Shipped as `interpret_audio`. Same pattern as `interpret_image` for audio chat-multimodal. Verified E2E via Gemini 2.5 Flash (Ollama / LM Studio do not yet expose audio encoders; transcription-only path documented as future `transcribe_audio` tool). |
| 10 | **Document extraction enriched** (PDF/Office/OCR) | MED (~1 week) | High | Partial in all 4 | #9 | Already parse PDF/Office basic in `read_file`. Extend with OCR of embedded images (via #9), structured tables, layout-aware paragraphs. **Not started.** |
| 11 | **Browser minimal** (navigate + scrape + optional screenshot) | HIGH (~2 weeks) | High | Hermes (Playwright/CDP full), OpenHands (BrowserToolSet), OpenClaude (`WebBrowserTool`) | uses #9 for screenshots | Start minimal: navigate URL → text content + optional screenshot. Marcelo: "important for research". **Not started.** |
| 12 | **Skill progressive disclosure** ✅ partial (2026-05-20) | MED-HIGH (~2 weeks) | High | All 4 | — | Already partially in place: `SkillsLoader` indexes skills in summary + loads on-demand. Today's `disable_model_invocation` (pi-compat) closes the remaining gap (skills can be programmatic-only). Phase 2 memory work no longer blocked on this item. |

**Estimated totals**:
- Items 1-4 (independent UX quick wins): ~5 days
- Items 5-8 (async/orchestration, mostly independent): ~3 weeks
- Items 9-10 (multimodal pipeline, chained): ~2 weeks
- Item 11 (browser, heavy but independent): ~2 weeks
- Item 12 (skills, memory-foundation): ~2 weeks

Total ~ 2.5-3 months if strictly sequential. Items 9 + 11 can parallelize, dropping to ~2 months.

**Explicitly rejected** (with reason):
- `apply_patch` (Codex envelope) — only useful with OpenAI-family models, not our case
- LSP-as-tool — per-language maintenance burden
- Worktree (git) — no multi-branch workflows in our use cases
- `kanban_*` — over-structured vs TodoWrite, no demand
- `TeamCreate/Delete` (swarms) — over-engineering
- `mixture_of_agents` — N× cost without demonstrated use case
- Channel integrations (Discord/Feishu/HomeAssistant/etc.) — already have channels system, one-off integrations don't scale

**Decision rule for additions to this list**: must be either (a) language-agnostic and adopted by ≥2 of the 4 reference agents, or (b) explicitly flagged by the user for daily-driver use. Other "would be nice" tools deferred unless they meet one of those criteria.

### Additions beyond the original 12-list (May 2026)

Items that emerged from running the tools roadmap and turned out to be worth shipping out-of-order:

| # | Addition | Date | Why |
|---|---|---|---|
| A | **Capability metadata + consensus snapshot** (`durin/providers/capabilities.py` + `data/model_capabilities.json` + `scripts/refresh_model_capabilities.py`) | 2026-05-19 | Pre-req for any "delegate to aux model" pattern; consolidates LiteLLM + OpenRouter + models.dev into one vendor-filtered consensus file (785 models) |
| B | **`AuxModelConfig` + `aux_providers`** plumbing (config schema + AgentLoop + ToolContext) | 2026-05-19 | Enables config-gated capability bridges. Supports `vision`, `audio`, extensible to others |
| C | **Mid-loop `context_transform` hook** (pi-inspired) | 2026-05-20 | One-line per-request transform of message list right before provider call. Foundation for token-budget pruning and dynamic context |
| D | **`disable_model_invocation` skill frontmatter flag** (pi-inspired) | 2026-05-20 | Skills hidden from LLM system prompt but still loadable programmatically. Closes most of item #12 |
| E | **Per-tool head/tail truncation** (pi-inspired) | 2026-05-20 | `shell`/`exec` output truncated from head (keep tail = errors); reads keep head. Small refinement of an old uniformity |
| F | **Real prompt-tokens anchor** (pi-inspired, perf C.1) | 2026-05-20 | Provider's actual `prompt_tokens` stamped on assistant messages → `estimate_prompt_tokens_chain` uses real numbers instead of estimating the whole chain |
| G | **`cache.usage` telemetry event** (pi-inspired, perf C.2) | 2026-05-20 | Per-turn structured event with `prompt_tokens`, `cached_tokens`, `cache_ratio_pct`. Surfaces existing server-side cache savings (e.g. z.ai's automatic prefix cache returns ~99% hits) |
| H | **Secrets subsystem — Phase 1+2** (`durin/security/secrets.py`, `durin secret` CLI) | 2026-05-22 | API keys out of plaintext config: a `~/.durin/secrets.json` store (0600), `${secret:}` references, `service`/`scope` axes, migration, redaction of secret values from tool results, `exec`-scoped subprocess injection. Design: `docs/archive/11_secrets_design.md`. Phase 3 → row I |
| I | **Secrets Phase 3 — interactive `request_secret`** | 2026-05-22 | The agent *requests* a credential; the user types it into a masked TUI modal / web panel; the value rides the websocket (never a URL query, never the chat) straight to the store. The LLM only learns the metadata. Closes the secrets subsystem |
| J | **Web config parity** | 2026-05-22 | The dashboard configures all of durin without the CLI: generic `/api/config` + schema-driven "All settings", a Secrets section, refined Settings IA (Providers / Web search / Channels / General model hub) |
| K | **Interactive tool renders** | 2026-05-22 | Purpose-built TUI + web renders for `ask_user_question` (option chips + editable answer) and `request_secret` (masked input), replacing the generic tool-call block |
| L | **TUI Pilot harness** (`durin/cli/tui/probe.py`, `scripts/tui_smoke.py`) | 2026-05-22 | Headless screen-text capture of the Textual TUI — the agent and tests can see what's painted without a real terminal |
| M | **Unified design system** (`design/DESIGN.md`, `design/tokens.css`, `durin/cli/theme.py`) | 2026-05-22 | One palette system (Ithildin / Forge / Mithril × light/dark) across web, TUI and wizard; an anti-drift test pins the Python mirror to the CSS |

---

**Phase 1b (Per-query dynamic context)**:
Hold until Phase 2 (memory) provides the natural substrate. Standalone implementation would duplicate work the memory system will subsume.

**Phase 2 (Horizon 2 — Memory)**:
Higher investment, higher potential differentiation. Now unblocked: Phase 2 was waiting on Phase 1c telemetry + the perf infra (anchored tokens + cache visibility) to make compaction decisions over real data, both of which are now in place. Build incrementally: start with flat skill docs (Hermes-style), then add structure as evidence warrants. **Before designing**, read `~/git_personal/hermes-agent/agent/background_review.py` + `curator.py` + `memory_manager.py` — Hermes ships an essentially-complete production implementation of the Doc 03 pattern (forked agent with tool whitelist, ContextVar-gated provenance, frozen 3-tier system prompt). See `archive/34_external_agents_review.md` §L1, §L2.

Once Phase 2 has retrieval, Phase 1b reduces to "use the memory retriever to fetch query-relevant context" — small additional work.

---

## Decision rules (carried over from bitácora lessons)

1. **No component without empirical or industrial precedent.** "It seems like it should help" is not enough.
2. **Mechanisms must demonstrably activate in realistic tests.** If the main code path never runs, the component is overhead.
3. **Distrust same-model self-verification.** Need ground truth (tests) or different models.
4. **Specificity > abstraction.** "Be cautious" doesn't change behavior; concrete rules do.
5. **3+ trials minimum** for any quantitative claim.
6. **Test in regimes where baseline can fail.** Ceiling-effect scenarios prove nothing.

---

## Last updated: 2026-05-24 08:40 (§2.D auto-absorb shipped)

> Post-T1 pass (2026-05-23): Phases 0-6 of the entity-centric memory plan
> ([archive/35_entity_centric_plan.md](archive/35_entity_centric_plan.md) +
> [archive/19_implementation_plan.md](archive/19_implementation_plan.md)) shipped, and
> the T1 wiring sweep (W1-W4 in archived doc 24) closed the runtime
> integration gap — `memory_search` now invokes the entity-aware ranker,
> `durin memory dream` upserts consolidated entity pages into LanceDB,
> AliasIndex builds lazily on first call, and `durin memory absorb` plus
> `absorb-suggest` are exposed in the CLI. 4 hermetic E2E tests cover
> the wired path; 4365 tests pass. Deferred T2 items (auto-trigger dream,
> identifier-based extraction, shared AliasIndex, auto-absorb) captured
> in [archive/36_post_t1_state_and_t2_horizon.md](archive/36_post_t1_state_and_t2_horizon.md)
> without commitment. Six docs moved to archive (the Phase 2 proposal,
> the typed-entities proposal, doc 21 integration plan, doc 22 critique
> validation, doc 23 T1 implementation plan, doc 24 wiring + e2e plan).

> §2.D auto-absorb post-dream shipped (2026-05-24 08:40): closes the
> loop between dream consolidation and entity absorption. After every
> dream pass that consolidated ≥1 entity, an LLM-judge evaluates
> alias-overlap candidates (skipping cross-type and pages younger
> than 24h to block premature consolidation), and auto-merges those
> at confidence ≥95. Reuses our existing git substrate for evidence
> + restore: commit body carries full judge reasoning, trailer
> `Judge-Confidence: N` distinguishes auto from manual, archived
> page survives at `entities/<type>/<canonical>/archive/`. `cmd_revert`
> upgraded from stub-with-guidance to actual `git revert` subprocess +
> emits `memory.absorb.reverted` for §2.E regret-rate tracking.
> Pre-existing bug in `absorb()` fixed at the same time: canonical
> wasn't re-upserted to vector index after merge (glm peer review C4).
> Opt-in default OFF — `durin config set memory.dream.auto_absorb.enabled true`.
> 31 new tests + smoke e2e; suite 4457 passing.

> §2.A.1 β.2 shipped (2026-05-24 02:10): the remaining 3 sub-daily
> triggers + manual route refactor. `MemoryStoreTool` now counts
> per-entity post-cursor entries after each write and spawns a daemon
> thread with `DreamRunner.run(trigger="threshold", entity_filter=ref)`
> when `threshold_entries` is crossed. `Consolidator.on_post_compaction`
> + `AgentLoop.on_session_close` callback attributes added; `cmd_new`
> invokes the session-close hook; both wired in `cli/commands.py`
> startup to spawn daemon threads when enabled in config. `cmd_dream`
> refactored to route through `DreamRunner` (manual now shares the
> lock with auto-triggers). 11 new tests + smoke e2e covering all 4
> triggers end-to-end. Suite at 4442 passing. §2.A.1 is now SHIPPED
> in full: 4/4 triggers in production.

> §2.A.1 β.1 shipped (2026-05-24 01:50): entity-centric dream
> auto-trigger — first half of the per-entity dispatcher. New
> `memory.dream` config block (cron, threshold_entries, post_compaction,
> on_session_close, min_seconds_between_runs). `DreamRunner` with
> atomic lock (`memory/.dream.lock`), stale-lock recovery, throttle,
> and three telemetry events (`memory.dream.start` / `.end` /
> `.skipped`). Cron daily registered as system job `memory_dream`
> (default `0 3 * * *`); routed via `asyncio.to_thread` so the cron
> loop stays responsive. 14 new tests, suite 4431. β.2 (post-compaction
> + session-close + threshold-per-entity hooks) pending — they all
> reuse the same `DreamRunner.run()` entry point.

> §2.H shipped (2026-05-24 01:30): fragment/canonical retrieval
> contract — closes the doc 18 §6 promise ("LLM reconcilia con
> timestamps y contexto") on both delivery paths. `Result` carries
> `kind` / `class_name` / `valid_from` / `entities`; `render_block()`
> wraps results in `=== CANONICAL/FRAGMENT: ... ===` blocks (same
> marker convention as compaction's `=== ARCHIVED SUMMARY ===`).
> `memory_search` tool exposes `rendered` per result. `search_dreamed`
> grep path now also surfaces entity pages (previously vector-only).
> `hot_layer` adds two new sections — Canonical pages (top N entity
> pages) + Recent fragments (post-cursor episodic entries that tag
> entities and post-date the cursor), both with markers. 21 new tests,
> suite at 4417 passing. See bitácora 2026-05-24.

> §2.C shipped (2026-05-24 00:15): shared AliasIndex via
> `durin.memory.aliases_cache` — process-wide cache keyed by
> `memory_root`, double-checked locking, mutation API
> (`refresh_for` / `remove`) propagates writes across consumers
> in-place so no explicit invalidation is needed in the common flow.
> 15 new tests, suite at 4396 passing. Closes the §2.C item from doc
> 25 — see bitácora 2026-05-24 for the rationale on choosing module-
> level over `ToolContext`-scoped (CLI subcommands don't have a
> ToolContext).

> Post-T1 design review (2026-05-23 22:55): identified that doc 18 §6
> ("LLM reconcilia con timestamps y contexto" + "página consolidada y
> entries post-cursor coexisten en los resultados") was a design intent
> that did NOT survive into the LLM delivery contract.
> `Result.to_dict()` drops `valid_from` / `entities` / `class_name`
> before the LLM sees them, and `hot_layer` reads from the legacy
> memory classes (`memory/<class>/*.md`) instead of the new entity
> pages (`memory/entities/<type>/<slug>.md`). Captured as **§2.H** in
> doc 25: fragment/canonical retrieval contract — treated as complete-
> T1, not T2, because it closes a wiring gap, not a new feature. §2.A
> (auto-trigger dream) split into **A.1** (per-entity dispatcher with 4
> triggers: cron daily, post-compaction, session-close, threshold) and
> **A.2** (daily cross-session learning — pattern detection across
> entities, trend tracking — deferred to a separate design doc since
> the current consolidator prompt is single-entity only).

> Post-v0.1.0a7 pass (2026-05-22): closed the secrets subsystem with the
> interactive `request_secret` flow (Phase 3); shipped full **web config
> parity** (the dashboard configures all of durin, no CLI needed);
> **interactive tool renders** for `ask_user_question` / `request_secret`;
> a headless **TUI Pilot harness**; and a **unified design system**
> (`design/DESIGN.md`) — one palette across web, TUI and wizard. Stale
> plan/report docs (code audit, daily-driver plan, smoke test, Textual
> migration, secrets/web-parity/interactive-render designs) moved to
> `docs/archive/`. **Next major direction: Memory Phase 2** — see
> `archive/08_memory_phase2_proposal.md` §0d (now superseded by the
> entity-centric path documented in 18+19).

> Earlier pass: items #1–9 of the original 12-list shipped (incl. capability bridges for vision + audio), item #12 mostly closed by the `disable_model_invocation` flag. Capability metadata pipeline (3-source consensus snapshot) shipped as a foundation. Pi coding agent reviewed and four refinements adopted: `context_transform` hook, skill disable flag, head/tail truncation per tool, anchored token accounting, cache visibility. Stale planning docs (`04_agent_strategies_catalog`, `05_log_swebench`, `06_log_experiments`) moved to `docs/archive/`. Memory (Phase 2) is now unblocked.

> Daily driver lifecycle (D6 + D7 + D8 in `archive/09_daily_driver_plan.md`): `durin config`, `durin upgrade`, `durin uninstall`, `durin doctor`, plus README + INSTALL.md shipped. Distribution renamed to `durin-agent` on PyPI; tag-triggered workflow builds + publishes wheel/sdist to GitHub Releases + PyPI via OIDC trusted publishing. The install/configure/upgrade/diagnose/uninstall surfaces are now complete; the operator no longer needs to hand-edit JSON or guess where state lives, and can install via `pipx install --pre durin-agent` without a checkout.

> **v0.1.0a7 — first consistent release (2026-05-22)**. Beyond D6-D8: split config layout (`~/.durin/config.json.d/` per-topic files with auto-migration + noise pruning), gateway daemon mode (`durin gateway start/stop/restart/status/logs`), webui structured tool-blocks (D9.1), `status`/`doctor` split into snapshot-vs-diagnostics, task-oriented `onboard` wizard that's re-runnable (keeps configured steps), model-capability auto-sync on default-model pick, and a full rebrand from the nanobot fork (⚒️ — durin is Tolkien's dwarf-king, not a cat). All prior `daily-driver-*` and `v0.1.0aN` tags were cleared; `v0.1.0a7` is the baseline.
