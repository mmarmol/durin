# Skill-import security policy + configurable settings — Design

> Hardening pass over §6.B (shipped). Groups the review items that are facets of
> ONE thing: the import **security policy** and its **configurability**. Items:
> A1 allowlist+defaults, A3 LLM-judge, A4 settings surface, A6 GitHub token, A8
> caps, B11 install-specs consent. (A2 POST-body → backlog P7; C14 force-replace
> + C13 fuzzy-via-chat already resolved.)
>
> **Status:** DESIGN — not approved, not implemented. Decisions below are my
> recommendations; open questions are flagged for the user to settle.

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

**Open decisions:**
1. Can the judge raise to **`dangerous` (block)**, or cap it at **`caution`
   (confirm)** to avoid false-blocks from hallucination? *Recommend: cap at
   caution in v1* (judge informs + can force a confirm, but only the deterministic
   rules block). Revisit once we trust the judge on the corpus.
2. **Default on or opt-in?** *Recommend opt-in* (`enabled=false`) — it needs an
   aux model configured and adds latency/cost; turn on in settings.
3. Prompt/criteria content — draft from the §8.C threat taxonomy.

---

## A1 — Allowlist + defaults

durin's philosophy ([[feedback_open_over_closed]], [[feedback_user_configurable_optional_features]])
argues against hardcoding someone else's trusted repos. Instead:
- Keep `memory.skill_import.allowlist: list[str] = []` (empty default).
- Make it **editable in settings** (A4).
- Add a **"trust this source" one-click** on the approve flow: after importing
  from `github:owner/repo/...`, offer to add `github:owner/repo` (or the host) to
  the allowlist, so the user builds their own allowlist organically. Allowlisted +
  safe + no-code then installs without confirm (the existing `decide_action` path).

*Open decision:* prefix granularity for "trust source" — repo (`github:owner/repo`)
vs org (`github:owner/`) vs host. *Recommend offer both repo and org.*

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
    enabled: bool = False                               # A3 opt-in
    max_severity: Literal["caution", "dangerous"] = "caution"  # A3 open-decision #1
    model: str = ""                                     # aux model name; "" → default judge
```

**Settings panel:** allowlist editor (add/remove prefixes), the GitHub-token
control (A6, with Test), caps (3 numbers), install-specs policy (select), and the
LLM-judge toggle + model picker. Mirrors the existing settings panels (BYOK/secrets
patterns).

---

## Open decisions to settle before building
1. LLM-judge: cap at `caution` or allow `dangerous`-block? (rec: caution v1)
2. LLM-judge: opt-in default? (rec: yes) + which aux model?
3. Caps: the three numbers (rec: 100 / 3 MB / 1 MB)
4. Install-specs: build the executor now (`ask`) or phase it (`never` + info v1)?
5. "Trust source" granularity: repo, org, both? (rec: both)

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
