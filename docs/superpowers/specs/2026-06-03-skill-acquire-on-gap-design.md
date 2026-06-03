# §6.C — Acquire-on-gap: search-before-create for skills

**Status:** DESIGN (2026-06-03) · grounded against code + Hermes/skill-creator prior art
**Vision doc:** `docs/plans/skills_evolutivas.md` (§1.5, §6.C, §8.E)
**Related:** discovery/registries spec `2026-06-03-skill-discovery-registries-design.md` (search substrate, BUILT)

## 1. What this is

durin's §1.5 promise: when durin faces complex work and has **no skill for it**, it
acquires one *on its own initiative* — searching skill registries, comparing
candidates, and using a hit as a **seed** rather than authoring from a blank page.

This spec connects two things that already exist but are **disconnected**: the
federated registry **search** (`skill_search` / `search_registries`, BUILT) and the
skill **authoring** path (`skill_write` → `dream_create_skill`, BUILT). Today no
creation path consults the search. §6.C is that connection, plus the risk gate that
governs when a remote seed may be used autonomously vs. needs the user.

**Scope boundary:** §6.C = *acquire + seed* (use a hit as starting content).
Translating a seeded skill to durin's **native tools** is §6.D and is out of scope
here. A §6.C seed is a starting SKILL.md, not a finished, fully-adapted skill.

## 2. Prior art (verified, not assumed)

- **Hermes** (`/Users/marcelo/git_personal/hermes-agent`): the reference model.
  - **No mid-task gap detection.** Reflection is triggered by an *accumulated*
    tool-iteration counter (`_iters_since_skill ≥ 10`, persists across turns, resets
    only on fire or on a skill action — `conversation_loop.py:655/4047/314`,
    `tool_executor.py:94/531`). The counter spawns a **deferred background review**
    *after* the turn (`background_review.py`), never mid-task.
  - Creation vs. patch is an **LLM judgment** inside that review; "create new" is the
    **last resort** (prefer patch loaded → umbrella → support file → new).
  - **In-session signal is prompt-level**, not structural: `SKILLS_GUIDANCE`
    (`prompt_builder.py:179`) + the `skill_manage` tool description ("Create when:
    complex task succeeded 5+ calls… offer to save… confirm with user"). The hard
    counter does **not** inject into the foreground; it only spawns the background
    fork. So Hermes uses prompt reinforcement *and* a structural backstop.
  - **Neither Hermes nor skill-creator seeds from a remote registry** — both author
    from scratch. Remote seeding is durin's novelty.
- **Claude skill-creator** (the builtin): interactive, foreground, human-in-the-loop,
  eval-driven (draft → test → review → iterate). The other end of the spectrum.

**Rejected approach:** an explicit "capability-gap detector" (the original §6.C
framing). Hermes deliberately avoids it — a mid-task gap signal is false-positive
prone and adds latency that breaks flow. The robust trigger is **complexity /
recurrence**, judged by the LLM, deferred.

## 3. What already exists (substrate — reuse, don't rebuild)

| Piece | Where | State |
|---|---|---|
| Federated search | `skill_search` tool + `search_registries` (skills.sh + clawhub) | BUILT, in **core** toolset (no `_scopes` → default `{"core"}`) |
| Authoring | `skill_write` → `dream_create_skill` (provenance=dream, mode=auto, commit) | BUILT, in **core** + dream toolset |
| Security gate (§8.C) | `skill_import(action=fetch)` → quarantine + `scan_skill` + `decide_action` (`allow`/`confirm`/`block`) | BUILT |
| Dream creation | `dream_phase1.md` flags `[SKILL]` (workflow 2+ times) → `dream_phase2.md` authors via `skill_write` with dedup | BUILT |
| In-session skill search prompt | `skills_section.md` already tells the agent to `memory_search kind=skill` before concluding no skill exists | BUILT |

**Empirical basis for the prompt path:** durin's memory tooling (`memory_search`,
`memory_ingest`) is demonstrably used by the agent in-loop without trouble, and
`skills_section.md` already rides that same pattern for skill *search*. The
"tool-description text is a weak signal" lesson ([[feedback_tool_description_weak_signal]])
applies to **passive micro-text in descriptions**, not to a **first-class tool the
agent reaches for** with system-prompt guidance — which durin has proven works.

## 4. Design — two paths (mirrors Hermes's dual design)

The same substrate (`skill_search` → §8.C gate → `skill_write`) is driven by two
triggers that differ only in **whether a human is present**, which decides how risk
is handled.

### Path A — In-session (interactive, opportunistic)

- **Trigger (prompt):** a `SKILLS_GUIDANCE`-style nudge added to the in-loop system
  prompt (extend `skills_section.md` or a sibling snippet). Intent: *when, mid-task,
  you hit a recurring or non-trivial workflow and local skill search
  (`memory_search kind=skill`) finds nothing, use `skill_search` to look for prior
  art in the registries before reinventing it.*
- **Flow:** `skill_search` → candidate hits → agent picks → `skill_import(action=fetch)`
  → §8.C gate (`decide_action`):
  - **safe** (`allow`: safe verdict + no code + allowlisted) → seed → `skill_write`.
  - **risk** (`confirm`/`block`) → **do not proceed autonomously.** Surface the
    candidates to the user for approval (recommended option first; flag which require
    installing tools). Only the user's choice proceeds.
- **Why here:** a human is present, so the risky-path confirmation is just a normal
  conversation turn (agent proposes, user replies).

### Path B — Dream phase 2 (autonomous, backstop)

This is the **2h `dream` job** (`agent.dream.run()`, phase 2) — the **create** path.
It is NOT the daily `curate_catalog` (which only evolves/fuses existing skills + §8.D
drift, never creates). §6.C touches only the create path; the daily evolve pass is
untouched.

- **Trigger:** the existing `[SKILL]` flag (workflow seen 2+ times in history) — no
  new trigger.
- **Flow:** before authoring a flagged `[SKILL]` from scratch, search the registries
  for prior art:
  - **safe hit** (`decide_action == allow`) → fetch + seed → `skill_write`.
  - **no hit / risky hit** → author from scratch (current behavior). A **risky** hit
    is **never auto-seeded**; it is logged/quarantined for later human review
    (mirrors the drift `confirm`/`block` path).
- **Why here:** no human is present in the 2h cron, so seeding is **safe-only**;
  anything needing a decision waits for a human.

### Risk rule (user-set, governs both paths)

> Search-before-create may seed **autonomously only when risk-free**
> (`decide_action == allow`). Any risk → discarded from the autonomous path; only the
> user may approve it, in-session, via an explicit confirmation.

This reuses `decide_action` verbatim — there is **no new policy engine**. §8.E
("when to escalate/block") = `decide_action`'s existing `confirm`/`block` verdicts.

## 5. Open design decisions (to settle before/within the plan)

1. **Path B mechanism — RESOLVED: (a) give phase-2 the `skill_search` tool.** The
   dream phase-2 toolset (`read_file` / `edit_file` / `skill_write`,
   `memory.py:_build_tools`) gains `skill_search` + a `dream_phase2.md` instruction to
   search before authoring a `[SKILL]`. Uniform with Path A (same agent-driven tools;
   the LLM decides relevance). Rejected (b) deterministic orchestrator pre-fetch — less
   flexible, diverges from Path A. Network note: `search_registries` uses
   `ssrf_safe_async_client` over public HTTP (not MCP), so it is reachable from the
   cron/headless dream.
2. **In-session confirmation surface — RESOLVED.** durin has a native
   `ask_user_question` tool (`durin/agent/tools/ask_user.py`,
   `AskUserQuestionTool`): `question` + `options` (array), with a
   `session.metadata['pending_question']` hook for structured UI rendering. Path A
   surfaces risky candidates as `options` (recommended first; flag which need tool
   installs) and waits for the user's pick. No new mechanism needed.
3. **In-session "recurring workflow" signal.** In-session the agent sees only the
   current session, not the 2+-occurrence history signal phase-1 uses. The nudge must
   lean on *complexity / "you just reinvented this"* (Hermes's `SKILLS_GUIDANCE`
   shape), not a cross-session counter.

## 6. Components to build (modest — substrate already exists)

1. **In-session prompt nudge** — extend `skills_section.md` (or a new snippet) with
   the search→seed→create guidance + the risk rule. (Path A trigger.)
2. **Path B seed hook** — per decision 5.1: add `skill_search` to the dream toolset
   and a `dream_phase2.md` instruction, **or** an orchestrator pre-fetch. Gate every
   seed through `decide_action`; risky → quarantine/log, not auto-seed.
3. **Risk-gate wiring** — reuse `decide_action`. In-session risky → in-conversation
   approval; dream risky → quarantine for human review.

No new pipeline, no new trigger, no new policy engine.

## 7. Testing (TDD)

- **Gate logic:** `decide_action == allow` → seed used; `confirm`/`block` → seed
  withheld (Path B → quarantine/log; Path A → routed to user approval). Mock
  `search_registries` + the fetch/scan so tests are offline and deterministic.
- **Path B seed hook:** given a flagged `[SKILL]` and a safe registry hit, phase-2
  authors from the seed; given no hit or a risky hit, it authors from scratch
  (current behavior unchanged — regression guard).
- **Prompt nudge (Path A):** prompt text is hard to unit-test for behavior; test the
  *wiring* (tool availability, gate routing) and validate the behavior **live**
  against the real agent ([[feedback_verify_live]]).

## 8. Out of scope (explicit)

- §6.D native-tool adaptation of a seeded skill (separate spec).
- §8.F GEPA/SkillOpt offline scoring.
- Any change to update detection / provenance (settled in the discovery spec §3.0 —
  content-addressed, no per-registry version fields).
- **A protected `system` skill tier** (never auto-evolves, always inserted when
  available, refuses user/dream edits — like Hermes's bundled/pinned classes).
  durin today has no true protection (builtins fork-on-write; nothing refuses edits).
  This is a **mode-taxonomy / governance change** (parked, own future spec); §6.C
  acquisitions land as `auto` and the existing model is unchanged. Open questions for
  that spec: "available when" (unconditional vs context-gated) and what "can't change"
  enforces given fork-on-write.
