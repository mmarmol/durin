# Skill-import security policy + configurable settings — Design

> Hardening pass over §6.B (shipped). Groups the review items that are facets of
> ONE thing: the import **security policy** and its **configurability**. Items:
> A1 allowlist+defaults, A3 LLM-judge, A4 settings surface, A6 GitHub token, A8
> caps, B11 install-specs consent. (A2 POST-body → backlog P7; C14 force-replace
> + C13 fuzzy-via-chat already resolved.)
>
> **Status:** DESIGN — the 5 open decisions were SETTLED with the user
> (2026-06-03); see "Decisions" at the bottom. Not yet implemented.

## Why now / what the field does (researched 2026-06-03, file:line verified)

| Dimension | Hermes | OpenClaw | durin today | durin target |
|---|---|---|---|---|
| Trust allowlist | hardcoded `TRUSTED_REPOS` (3), not user-editable | hardcoded source set → only *warns* | `allowlist=[]` (all → confirm) | user-configurable + "trust source" one-click |
| Configurable severity | no (a few toggles) | no (env scan limits only) | no | **yes** (this spec) |
| LLM-judge scan | no (regex only) | no (regex only) | no | **yes, opt-in** (durin ahead) |
| Size caps | 50 files / 1 MB / 256 KB | 500 files / 1 MB-file | 200 files / 5 MB (arbitrary) | align + configurable |
| Install-specs | never run (agent's terminal) | auto-run (hardened) | never run | **ask-then-run** (middle) |
| Dedup | enforce + `--force` | enforce + `force` | enforce + `replace` ✅ | done (C14) |
| Signing | unsigned | integrity-hash (plugins only) | `content_hash` only | defer to v2 |

Takeaway: neither peer makes security **user-configurable** and neither has an **LLM judge** — durin can lead on both, while aligning caps/consent to the field.

**Design principle (non-negotiable): clean skills pass frictionlessly.** None of
this may punish healthy skills. A safe, no-code skill must sail through the
deterministic scan + the LLM-judge with **zero false escalation**; the only
friction on a clean skill is a single trust-establishing confirm when its source
isn't yet allowlisted (and none once it is). **Acceptance criterion:** re-verify
the whole pipeline against the clean corpus (durin's 11 builtins + the ~157 real
hermes/openclaw skills) — not one clean skill may turn caution/dangerous. This is
exactly why the judge may escalate ONLY with a concrete, explained finding
(decision A3.3): no vague unease, so a skill it cannot fault stays `safe`.

---

## A3 — LLM-judge semantic scan (priority)

**What:** after the deterministic §8.C scan, an optional LLM pass reads the skill
(SKILL.md body + bundled scripts, truncated to a budget) and returns a semantic
verdict + findings the regex cannot see: disguised malicious logic, intent,
subtle data-exfil, social-engineering of the agent.

**Integration:** runs in `scan_skill`'s caller during **import only** (never the
800-doc bench bursts — that was the §8.C cost rationale). Its verdict is **merged
by max-severity** with the deterministic one — it can only *raise*, never lower
(defense-in-depth; a clean regex scan + a worried judge → caution/dangerous).

**Model:** a durin aux model. Add `aux_models.skill_audit` (fall back to the
default judge model). Costs one call per imported skill — acceptable for a manual
action.

**Packaging:** ships as a direct pipeline call with a versioned prompt/criteria.
The user-extensible form is the "auditor skill" pattern (§8.C v2 note) — a builtin
skill the agent can run for a deeper, tool-using audit; out of scope here.

**Settled (2026-06-03):**
1. **Cap at `caution`.** The judge raises to at most caution (forces a confirm);
   it NEVER blocks on its own — only the deterministic rules block.
2. **ON by default** (`enabled=true`), **degrading gracefully**: if no aux model
   resolves, skip the judge silently (don't error, don't block the import).
3. **Escalate only with a concrete, explained finding.** Every judge finding MUST
   state *exactly* what and why — the specific code/snippet/behavior and the
   threat — surfaced verbatim in the gate. No vague "looks suspicious": a skill
   the judge cannot concretely fault stays `safe` (serves the clean-skills
   principle). The prompt/criteria derive from the §8.C threat taxonomy and force
   a structured `{verdict, findings:[{what, why, where}]}` output.

---

## A1 — Allowlist + defaults

durin's philosophy ([[feedback_open_over_closed]], [[feedback_user_configurable_optional_features]])
argues against hardcoding someone else's trusted repos AND against per-source-kind
"magic" — github isn't the only host (gitlab / bitbucket / internal exist). So
trust is a **uniform, user-configured list of patterns** matched (by prefix)
against the source `ref`, identical for every source kind:
- Keep `memory.skill_import.allowlist: list[str] = []` (empty default) — already a
  prefix list. Examples: `github:acme/`, `https://gitlab.com/acme/`,
  `https://bitbucket.org/myorg/`.
- Surface it as an **advanced "trust patterns" editor** in settings (A4).
- Optional convenience: a **"trust this source"** button on approve that
  **pre-fills the imported skill's exact `ref`** into the editor for the user to
  trim to the prefix they want — it does NOT auto-decide repo-vs-org-vs-host.
- `ref` shapes by kind: github → `github:owner/repo@branch/dir`; https →
  `https://host/path/SKILL.md`; local → absolute path (own machine; rarely needs
  trusting). Allowlisted + safe + no-code → installs with no confirm (existing
  `decide_action`).

*Future option (not v1):* glob patterns (`https://*.acme.com/`) if prefix proves
too rigid.

---

## A8 — Size caps (align + configurable)

Make the `skill_resolve`/`fetch_candidate` caps config-driven and align defaults
to the field (durin's 200/5 MB is lax vs hermes 50/1 MB):
- `max_files` default **100**, `max_total_bytes` default **3 MB**, add
  `max_file_bytes` default **1 MB** (skip/parse-limit per file, like both peers).
*Open decision:* exact numbers. *Recommend the above.*

---

## B11 — Install-specs consent (ask-then-run)

A skill declares dependency installs (`metadata.*.install`: brew/apt/pip/…).
durin validates their shape (§8.C `validate_install_specs`) but never runs them.
The middle ground between hermes (never) and openclaw (auto):
- Config `install_specs_policy: "never" | "ask" | "auto" = "ask"`.
- On install (or first use), if specs exist and policy is `ask`, surface them and
  run **only on explicit user approval**, each command built from the validated
  safe pattern (reuse the OpenClaw-derived allowlists already in §8.C) + node
  `--ignore-scripts` hardening.
**Scope flag:** this needs a new **executor** (run the validated commands) — a real
security surface. *Recommend phasing:* v1 = `never` + **surface the declared deps
to the user as info** (no executor); v1.1 = `ask` with the executor. Ties to backlog
**P6** (runtime execution consent). *Open decision:* build the executor now or
phase it.

---

## A6 — GitHub token via durin secrets (in skills config)

GitHub resolution/fetch is currently anonymous (60 req/h, no private repos). Wire
it to durin's **existing** secrets system (`~/.durin/secrets.json`,
`durin secrets set`, `resolve_secret`) — no new secret machinery.
- Config `memory.skill_import.github_token_secret: str = ""` — the **name** of a
  secret holding a GitHub token.
- `_gh_get_json`/`_http_get_bytes` add `Authorization: Bearer <resolve_secret(name)>`
  when set.
- **Settings UX (in the skills security section):** a control to **pick an existing
  secret** (from `/api/secrets`) **or create a new one**, plus a **Test** button →
  calls GitHub `GET /rate_limit` (or `/user`) with the token and shows
  ok + remaining-quota (or the auth error). The test reuses the existing secrets +
  config routes.

---

## A4 — Configurable security settings (the surface that holds the above)

A new **"Skills security"** section in the webui settings + config keys. Reuse the
existing config mechanism (`/api/config` + `/api/config/set`) — minimal new surface.

**Config schema** (under `memory.skill_import`, extending today's `allowlist`):

```python
class SkillImportConfig(Base):
    allowlist: list[str] = Field(default_factory=list)
    github_token_secret: str = ""                       # A6
    max_files: int = 100                                # A8
    max_total_bytes: int = 3 * 1024 * 1024              # A8
    max_file_bytes: int = 1024 * 1024                   # A8
    install_specs_policy: Literal["never", "ask", "auto"] = "never"  # B11 (v1)
    llm_judge: SkillJudgeConfig = Field(default_factory=SkillJudgeConfig)  # A3

class SkillJudgeConfig(Base):
    enabled: bool = True                                # A3 — on by default; graceful degrade if no aux model
    max_severity: Literal["caution", "dangerous"] = "caution"  # A3 — settled: caution
    model: str = ""                                     # aux_models.skill_audit; "" → default judge model
```

**Settings panel:** allowlist editor (add/remove prefixes), the GitHub-token
control (A6, with Test), caps (3 numbers), install-specs policy (select), and the
LLM-judge toggle + model picker. Mirrors the existing settings panels (BYOK/secrets
patterns).

---

## Decisions (settled 2026-06-03 with the user)
1. **LLM-judge cap:** `caution` — never blocks alone; only deterministic rules
   block. Plus: every judge finding must give the **exact** what/why.
2. **LLM-judge default:** **ON**, degrading gracefully when no aux model resolves.
   Model: `aux_models.skill_audit`, falling back to the default judge model.
3. **Caps:** 100 files / 3 MB total / 1 MB per file.
4. **Install-specs:** v1 = **info-only** (`never`); the approval executor → v1.1.
5. **Trust:** a **uniform advanced-config list of prefix patterns** over the
   source ref (any host — github/gitlab/bitbucket/https alike); optional
   pre-fill-the-ref convenience button; no per-source-kind magic. Glob = future.

**Acceptance criterion (clean-skills principle, non-negotiable):** re-verify the
whole pipeline (deterministic scan + LLM-judge + gate) against the clean corpus
(durin builtins + ~157 real skills) — **not one clean skill may escalate** to
caution/dangerous.

## Implementation order (when approved)
1. Config schema (`SkillImportConfig` + `SkillJudgeConfig`) + wire caps into
   `skill_resolve`/`fetch_candidate`.
2. A6 token: `resolve_secret` in the GitHub fetch + the settings control + Test route.
3. A1 "trust source" one-click + allowlist editor.
4. A3 LLM-judge: the pipeline pass + `aux_models.skill_audit` + merge logic + the
   versioned prompt; opt-in toggle.
5. A4 settings panel assembling all of the above.
6. B11 per the chosen scope (info-only v1, or executor).
7. Verify live; backend + webui suites green.
