# Skill Interop Standard (§8.B) — Design & Decision

> **Status:** Decision record, written autonomously 2026-06-03 per the architect's mandate
> ("fijar el estándar primero; el resto son imports; que lo más fácil sea el modelo
> estándar y abierto; Hermes pesa más pero verificá que cumpla nuestro skill design").
> Resolves open point **§8.B** of [`docs/plans/skills_evolutivas.md`](../../plans/skills_evolutivas.md).
> The architect will review and adjust on return.

---

## §1 — The decision (one line)

**durin adopts [agentskills.io](https://agentskills.io/specification) as its `SKILL.md` standard**, keeping durin's own behavior under the `metadata.durin.*` vendor namespace and guaranteeing **round-trip fidelity** (foreign frontmatter survives durin edits). This makes importing any agentskills.io skill — from Hermes, Claude Code, Cursor, Codex, or any of the 490k+ marketplace skills — a near-no-op.

## §2 — Why agentskills.io (the research, not a guess)

We compared the real code/docs of the agents in `~/git_personal/` and confirmed on the web. The result is unusually unambiguous: **there is no competing standard.**

| Framework | Cites | Required core | Vendor namespace | Parser |
|---|---|---|---|---|
| **Hermes** (`hermes-agent`) | "Compatible with the [agentskills.io](https://agentskills.io) open standard" (`README.md`, `tools/skills_tool.py:28`) | `name`, `description` | `metadata.hermes.*` | permissive — unknown keys preserved; only name+desc validated |
| **OpenClaw** | "AgentSkills-compatible … follows the AgentSkills spec" (`docs/tools/skills.md:11,202`) | `name`, `description` | `metadata.openclaw.*` (legacy `clawdbot`) | permissive; unknown keys preserved |
| **Pi** | "implements the [Agent Skills standard](https://agentskills.io/specification)" (`packages/coding-agent/docs/skills.md:7,139`) | `name`, `description` | (root `metadata`) | permissive, warn-not-reject; preserves `[key]: unknown` |
| OpenHands / OpenCode / OpenClaude / mem0 / mempalace / cognee | all `SKILL.md` + frontmatter; mem0 README cites the "[skills standard](https://github.com/anthropics/skills)" | `name`, `description` | varies | permissive |

**Web confirmation:** agentskills.io **is** Anthropic's "Agent Skills" spec (published Dec 18 2025), released as a true open standard and now governed cross-vendor. ~32+ tools (VS Code, Codex, Gemini CLI, Cursor, JetBrains Junie, Kiro, Goose, Amp…) and 490k+ skills across three marketplaces (SkillsMP, Skills.sh, ClawHub) implement it as of mid-2026. So the user's "not the Claude format" is moot in the right way: **agentskills.io *is* the open, vendor-neutral form of that format** — adopting it is adopting the open standard, not Anthropic's product.

**The portable core (agentskills.io `/specification`):**

```yaml
---
name: skill-name          # REQUIRED — 1-64 chars, lowercase letters/digits/hyphens, matches dir name
description: ...           # REQUIRED — 1-1024 chars, "what it does + when to use it"
license: MIT              # optional
compatibility: ...        # optional — free-form env requirements (≤500 chars)
metadata: { ... }        # optional — arbitrary map; vendors namespace under metadata.<vendor>
allowed-tools: a b c     # optional — space-separated pre-approved tools (experimental)
---
(markdown body — progressive disclosure: description always in prompt, body on demand,
 references/scripts/assets/templates loaded only when needed)
```

**The load-bearing ecosystem pattern (this is what makes import work):** every parser is **permissive** — it keeps the standard core at root, lets each vendor put behavior under `metadata.<vendor>.*`, and **never drops unknown keys**. A skill round-trips through any compliant tool without losing data.

## §3 — Does Hermes comply with durin's skill design? (the architect's question)

**Yes — they are siblings under the same standard.** Hermes = agentskills.io core + `metadata.hermes.*`. durin = agentskills.io core + `metadata.durin.*`. Importing a Hermes skill into durin:

- `name`, `description`, `version`, `license`, `platforms` (root, standard) → durin reads/preserves them.
- `metadata.hermes.*` (Hermes' tags, related_skills, conditional-activation) → foreign to durin; **preserved untouched** on round-trip, ignored functionally.
- durin **adds** `metadata.durin.*` on import (`mode`, `provenance.source="marketplace:…"`) without disturbing the original.
- The skill's *content* (the procedure) works as-is.

The **only** functional adaptations durin needs are: (a) honor the standard root `platforms` field (Hermes/most use it; durin doesn't yet), and (b) at import time, optionally map a foreign requirement declaration (`metadata.hermes.requires_*` / `required_environment_variables` / `metadata.openclaw.requires`) onto durin's `metadata.durin.requires` if we want durin to gate on it. Neither is a conflict — they're additive.

## §4 — durin today vs the standard (verified in code, not assumed)

durin is **already a near-compliant citizen** — the format work is small:

| Aspect | durin today | Standard | Gap |
|---|---|---|---|
| Required core | `name`, `description` at root (`skills_frontmatter.split_frontmatter`) | same | ✅ none |
| Vendor namespace | `metadata.durin.*` (`ensure_durin`) — `mode`, `provenance`, `requires`, `always`, `curated` | `metadata.<vendor>` | ✅ already the right pattern |
| Round-trip fidelity | `_update_md` = `split_frontmatter` → mutate → `join_frontmatter(sort_keys=False)` | preserve unknown keys | ✅ structurally safe — **lock with tests** |
| `requires` (bins/env) | `metadata.durin.requires.{bins,env}` (`_check_requirements`) | vendor-namespaced | ✅ ours; map foreign on import |
| `disable_model_invocation` | root `disable_model_invocation` / `disableModelInvocation` | OpenClaw uses root kebab `disable-model-invocation`; agentskills core doesn't define it | ⚠️ also accept the kebab form for import |
| `platforms` (OS gating) | **not parsed at all** (`grep` → 0 hits) | `platforms: [macos, linux, windows]` | ❌ **the one real functional gap** |
| `version`, `license`, `compatibility` | preserved by round-trip, but not surfaced | optional standard fields | ⚠️ keep preserving; surface `version`/`license` in skill info |

## §5 — What we build (§8.B scope) and what we defer

**In scope (this spec → its plan):** make durin a *faithful* agentskills.io citizen.
1. **Round-trip fidelity, locked by tests** — foreign root keys (`license`, `version`, unknown) and foreign vendor blocks (`metadata.hermes.*`) survive `dream_create_skill` / `apply_skill_edit` / `save_skill_content` / `set_mode` / `mark_curated` / `dream_fuse_skills`. (Mostly already true via `_update_md`; prove it, fix any path that rewrites from scratch.)
2. **Honor `platforms`** — root `platforms: [macos|linux|windows]` gates availability (skill hidden/unavailable off-platform), with OpenClaw aliases (`darwin`→macos, `win32`→windows) accepted for import. Slots into the existing availability check.
3. **Accept standard spellings / preserve standard fields** — also read root kebab `disable-model-invocation`; keep `version`/`license`/`compatibility` on round-trip and surface `version`/`license` in `list_skills_info`.
4. **Document the contract** — a canonical doc: durin `SKILL.md` = agentskills.io core (root) + `metadata.durin.*` (behavior) + the round-trip/import guarantee. This *is* the "fijar el estándar" artifact.

**Deferred to the next plan (§6.B — "el resto son imports"):** the actual import command — fetch a skill (URL / GitHub / marketplace) → copy the whole skill directory (`SKILL.md` + `references/`/`scripts/`/`assets/`/`templates/`) → stamp `metadata.durin.provenance.source` + `mode` → map foreign requirements → commit. With §8.B done, import is "copy the dir + stamp our namespace"; everything else already works because we share the standard. A separate plan covers it.

**Out of scope (own etapas):** §6.C acquire-on-gap (remote federated search), §6.D evolution gate, §8.C security floor (governs the "skill = plugin" code-install path — required before *executing* imported code, but not before *importing* the document), §8.D upstream drift, §8.F GEPA/SkillOpt.

## §6 — Rationale & alternatives (the "por qué")

- **Why adopt, not invent:** the ecosystem already converged; inventing a durin format would make us the only non-importable agent. The user's priority is "import easy" — adoption *is* that.
- **Why a superset, not a wholesale replace:** durin's behavior (git-backed evolution, manual-mode approval, hot/cold retrieval, dream curation) needs metadata the standard doesn't define. The ecosystem answer is `metadata.<vendor>` — so we keep `metadata.durin.*` and adopt the standard *root*. Open vocabulary, flat where it counts.
- **Why round-trip fidelity is the keystone, not a nicety:** the whole value of a standard is portability. If durin dropped `metadata.hermes.*` or `license` on the first edit, an imported skill would silently degrade and couldn't round-trip back to its origin. Preserve-by-default is what every compliant parser does; we make it a tested invariant.
- **Why `platforms` is the one functional add:** it's the only *standard root* field with runtime semantics (visibility) that durin doesn't honor — so an imported `platforms: [linux]` skill would wrongly show on macOS. Everything else durin already does (requires) or can ignore-but-preserve (license/compatibility/allowed-tools).
- **Rejected — "native Claude-Code format":** the user rejected it, and it's the wrong frame anyway: agentskills.io is the open, governed superset of exactly that format. Choosing agentskills.io gets Claude-Code compatibility *and* every other tool's.

## §7 — Success criteria

1. A real Hermes/OpenClaw/Claude-Code `SKILL.md` (with `metadata.hermes.*` / `license` / `version` / `platforms`) dropped into `workspace/skills/<name>/` loads in durin, is searchable/injectable, and **survives an `apply_skill_edit` + `set_mode` + `mark_curated` cycle with every foreign key intact** (round-trip test).
2. A skill with `platforms: [linux]` is unavailable on macOS and available on linux.
3. durin's own authored/curated skills still validate as agentskills.io-core-compliant (name+description present; durin behavior strictly under `metadata.durin`).
4. The contract is documented as the canonical durin skill spec.
