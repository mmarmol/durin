# Skill Import (§6.B) + Security Floor (§8.C) — Design & Decision

> **Status:** Decision record, written autonomously 2026-06-03 per the architect's mandate
> (plan the import; v1 **includes code-bearing skills**, so build §8.C; "no depende de la
> fuente sino de reglas claras"; "es un skill" → the orchestrator is a builtin skill).
> Resolves **§6.B** and **§8.C** of [`docs/plans/skills_evolutivas.md`](../../plans/skills_evolutivas.md).
> Builds on the adopted standard ([`2026-06-03-skill-interop-standard-design.md`](2026-06-03-skill-interop-standard-design.md)).
> **The architect will review the security policy (§4) and adjust on return.**

---

## §1 — The shape (one diagram)

Because durin adopted agentskills.io, a fetched skill is *already in our format* — import is **source-agnostic rules**, not per-marketplace adapters. The only source-aware part is the thin **fetch**. Everything after is universal: validate → gate → stamp → install.

```
 source (path / URL / GitHub)            DETERMINISTIC CORE (code/tools)        agent-facing
 ─────────────────────────────          ──────────────────────────────         ────────────
        fetch (thin adapter)  ─────────▶  quarantine (.durin/import-quarantine/<name>/)
                                          │
                                          ▼
                                   validate_skill()  ── agentskills.io lint + CODE detection
                                          │                 (scripts/ , install specs)
                                          ▼
                                   requires_confirmation(source, report)   ◀── §8.C invariant floor
                                          │   (out-of-allowlist OR carries-code → reason)
                                          ▼
         orchestrator SKILL  ──────────▶  [if reason] AskUserQuestion(source + code artifacts)
         (builtin import-skill,           dedup vs existing (memory_search)
          the §5.6 meta-skill seed)       [optional] Etapa-2 adapt to durin tools (judgment)
                                          │
                                          ▼
                                   install_imported_skill(..., confirmed)
                                     ── REFUSES if a confirmation was required and not given
                                     ── multi-file copy → workspace/skills/<name>/
                                     ── stamp metadata.durin.provenance + mode=manual
                                     ── commit + index sync (reuses E1 store + Spec-2 indexer)
```

**The split that matters (the architect's "prompt + lint, es un skill" — refined):** the orchestrator IS a skill (dogfooding, the §5.6 seed), but it **calls deterministic tools** for the mechanics. The LLM must not free-hand the fetch, the format check, the security gate, or the file writes — because those need reliability (commit + index sync via the sanctioned store, not raw writes), and **safety (the §8.C gate can't be a prompt the LLM can rationalize past)**. Skill = conductor; tools = instruments.

## §2 — The deterministic core (code)

| Component | Responsibility | Reuses |
|---|---|---|
| **fetch** (source adapters) | `path://` (local dir/file), `https://…/SKILL.md` (single file), `github:owner/repo/path` (skill dir). Lands the skill in a **quarantine** dir. Source-aware, thin. | `_safe_name` (traversal guard) |
| **`validate_skill(dir) → Report`** | agentskills.io conformance (name 1-64 lowercase/digits/hyphens + matches dir, description non-empty ≤1024, frontmatter parses) → `errors`/`warnings`; **code detection** → `carries_code: bool` + `code_artifacts: [paths/commands]` (presence of `scripts/`, or `metadata.*.install` specs). Deterministic — no LLM. | `split_frontmatter` |
| **`requires_confirmation(source, report) → reason\|None`** | The **§8.C floor** as a pure function: returns a reason when source ∉ allowlist OR `report.carries_code`. See §4. | config allowlist |
| **`install_imported_skill(quarantine_dir, *, source, confirmed) → result`** | **Enforces the floor**: raises/refuses if `requires_confirmation(...)` is set and `confirmed` is False. Otherwise: multi-file copy quarantine→`skills/<name>/`, stamp `metadata.durin.provenance` (`source`, `imported_at`, `confirmed`, `carried_code`) + `mode="manual"`, commit + index. | `skills_store` write path, Spec-2 `_sync_index` |
| **tool `skill_import`** | Agent entry point. `skill_import(source)` → fetch+validate, returns the Report + (if a confirmation is required) a `needs_confirmation` response listing the code artifacts. `skill_import(source, confirm=true)` → installs (the code still re-checks the floor). Mirrors `apply_skill_edit`'s `confirm` pattern. | tool base |

## §3 — The orchestrator skill (the "es un skill" part)

Builtin `durin/skills/import-skill/SKILL.md` — the agent reads it to know the procedure. It is `mode: auto` provenance `builtin`, and it is the **seed of the §5.6 meta-skill** (later it learns the user's import preferences via `USER.md`). Its procedure:
1. `skill_import(source)` → read the lint + scan Report (findings + verdict).
2. If `needs_confirmation` (verdict `caution`, carries code, or out-of-allowlist): `AskUserQuestion` showing the source + verdict + the exact findings/code artifacts, get an explicit decision. If verdict `dangerous`: do **not** auto-proceed — present the critical findings and require the user's deliberate force (override); never override on the agent's own judgment.
3. Dedup: `memory_search(kind="skill")` for overlap; if a near-duplicate exists, surface it (merge vs keep vs replace) instead of blindly adding.
4. *(Optional, Etapa-2 — judgment)* adapt foreign tool references to durin-native tools; the opt-out is "import as-is" (stop at Etapa-1).
5. `skill_import(source, confirm=true)` (or, only on the user's explicit force, `override=true`) → install.

The skill is *guidance*; the **guarantees live in the code** (`install_imported_skill` refuses unconfirmed `confirm`-installs and blocks `dangerous` without an explicit `override`, regardless of what the skill or the LLM does).

## §4 — The security floor §8.C (research-grounded; policy steered by the architect)

> **Why this section grew:** deep-dived Hermes + OpenClaw and surveyed the 2026 ecosystem (NVIDIA SkillSpector, Snyk, Cisco, AgentShield, Sentinel-AI, Prompt-Shield, OWASP Agentic-Skills-Top-10, SkillScan's 31k-skill study). The threat is empirical, not hypothetical: **13–26% of marketplace skills carry critical/malicious patterns**; ClawHub had 800+ malicious skills injected in weeks. The ecosystem **converged** on: *deterministic static scan (cheap first layer) → trust×verdict policy matrix → human gate*, with signing/sandbox/LLM-judge as later layers. Citations in §8.

**The biggest correction to the baseline:** the #1 attack vector is **prompt injection in the `SKILL.md` body itself** (91% of malicious skills; OWASP LLM01) — the agent *reads and obeys* the SKILL.md, so a malicious body is worse than a malicious script. The floor must scan the **body**, not just flag `scripts/`. (Skills with scripts are 2.12× more vulnerable, but body-injection is the majority.)

**The pipeline (quarantine → scan → matrix → human gate → audited install):**

1. **Quarantine** (Hermes pattern): fetched skill lands in `.durin/import-quarantine/<name>/`; nothing reaches active `skills/` until approved.
2. **`scan_skill(dir) → {findings, verdict}`** — a **deterministic** regex/pattern scanner (no LLM, no YARA dep, no external API; honest about finite recall) over the **SKILL.md body AND bundled scripts**, in curated categories drawn from SkillSpector/Prompt-Shield/AgentShield:
   - **prompt-injection-in-body** — "ignore previous/all instructions", role-override ("you are now"), hidden/exfil directives, zero-width/invisible-unicode, base64-in-body. *(critical/high)*
   - **dangerous-code** (scripts) — `eval`/`exec`/`os.system`/`subprocess(shell=True)`, `curl|bash`, `rm -rf /`, reverse shells, `process.env`→network (env-exfil), obfuscation. *(critical)*
   - **secrets** — provider regexes (AWS `AKIA…`, `sk-…`, GH PAT) + entropy. *(caution)*
   - **supply-chain** — unpinned deps, `curl|sh`, typosquat-ish. *(caution)*
   - verdict = max severity → `safe` | `caution` | `dangerous` (Hermes' model).
3. **Install-spec validation** (OpenClaw's concrete regexes, copied) when `metadata.<vendor>.install` is present: brew/node/go/uv/download safe-patterns — reject `..`, `\`, `://` (except download which is HTTPS-only), leading `-`. An invalid spec is a finding.
4. **Trust × verdict policy matrix** (Hermes pattern; the architect chose **block-with-explicit-override on `dangerous`**, 2026-06-03):

   | source \ verdict | safe | caution | dangerous |
   |---|---|---|---|
   | **allowlisted** | allow¹ | confirm | **block (override)** |
   | **out-of-allowlist** | confirm | confirm | **block (override)** |

   ¹ *unless the skill carries code → always at least `confirm` (installing code is a conscious act).*
5. **The invariant — enforced in `install_imported_skill` (code, not prompt):**
   - `dangerous` verdict → refuse unless an **explicit override** (`override=True`, distinct from a normal confirm). The agent/meta-skill cannot reach `override` without the user's deliberate force.
   - `confirm` required (caution / carries-code / out-of-allowlist) → refuse unless `confirmed=True`.
   - `allow` → install.
   The human gate is never bypassable by the LLM; tested as an invariant.
6. **Human confirmation** (the orchestrator skill's `AskUserQuestion`) **surfaces the findings + verdict** — the scanner *surfaces*, the human *decides*. The scanner's finite recall is acknowledged; it raises the verdict and informs the human, it is not a guarantee.
7. **Provenance + audit + integrity:** stamp `metadata.durin.provenance = { source, imported_at, verdict, confirmed, overridden, content_hash }` and append a one-line entry to an import audit log (`.durin/import-audit.log`, Hermes' `audit.log` pattern). `content_hash` = sha256 of the installed tree.

**Config (only loosens the *source* check, never the verdict/code floor):** `memory.skill_import.allowlist: [<prefix>, …]` — trusted source prefixes. **Default empty** (every source confirms once; you grow the list). No config can lower the dangerous-block or the carries-code confirm.

**Deferred to v2 (the ecosystem's full "verify → install → constrain"):** Ed25519 signing + offline verification (NVIDIA/STSS), SLSA provenance, SBOM, transparency logs, **LLM-judge semantic scan** (catches context-aware injection the regex misses), runtime sandboxing/containment, running installers (brew/npm), per-execution code gating. Each only ADDS assurance; none removes the human gate.

**Settled defaults:** allowlist empty; imported skills `mode=manual`; gate at import-time (not per-execution) in v1.

## §5 — Scope

**In (v1):** sources = local path / direct URL / GitHub dir; `validate_skill` (agentskills.io lint) + `scan_skill` (deterministic security scan, body+scripts) + install-spec validation; the §8.C trust×verdict floor with block-on-dangerous-override, enforced in `install`; quarantine; multi-file install with provenance + audit + content-hash + commit + index; the `skill_import` tool; the builtin `import-skill` orchestrator skill; dedup via `memory_search`.

**Deferred (own etapas):** §6.C marketplace/federated search + acquire-on-gap; signing/SLSA/SBOM/transparency-logs; LLM-judge scan; runtime sandbox + per-execution gating; running installers; automatic Etapa-2 adaptation; export/publish.

## §6 — Rationale

- **Source-agnostic by construction:** §8.B already made every compliant skill drop-in. So import ≠ adapters; import = validate + gate + stamp. The architect's instinct, realized.
- **Skill-as-conductor, code-as-floor:** packaging the *procedure* as a skill is the right dogfooding (and the meta-skill seed), but security and reliability can't be prompt-deep. The `install` function is the single chokepoint that makes the §8.C floor a real, tested invariant rather than a guideline the LLM might skip.
- **Scan surfaces, human decides — both, not either:** the ecosystem converged on a deterministic scan FEEDING a human gate, and so do we. The scan raises the verdict and shows the findings (honest about finite recall); the human is the floor. We don't ship a scanner that pretends to catch all malice, nor a gate blind to the obvious patterns everyone else catches.
- **Block-with-override on `dangerous`:** the architect chose deliberate friction for critical findings — the agent cannot install a `dangerous`-verdict skill; only the user's explicit force does. The cheap regex catches the dominant patterns (env-exfil, injection, `curl|bash`); the human ratifies the rest.
- **Quarantine + provenance + audit:** nothing untrusted lands in active `skills/` un-reviewed; every import records source, verdict, the decision, and a content hash.

## §7 — Success criteria

1. A standard skill from an **allowlisted** source with **no code** and a **safe** scan verdict installs with **no confirmation**, fully indexed and searchable.
2. A skill that **carries `scripts/`** (any source) → `skill_import` returns `needs_confirmation` listing the scripts; install **refuses** without `confirmed=True` (tested invariant); with it, provenance records `carried_code` + `verdict`.
3. A skill whose **SKILL.md body** contains a prompt-injection pattern (e.g. "ignore previous instructions") or whose script does env-exfil → scan verdict `dangerous` → install **refuses** without an explicit `override=True` (tested invariant); the orchestrator surfaces the exact findings.
4. A skill from an **out-of-allowlist** source → confirmation required even when safe + no code; adding the prefix to `allowlist` removes the *source* confirmation but **not** the code/dangerous gates.
5. An imported skill round-trips (foreign frontmatter intact — §8.B), is `mode=manual`, and `provenance` carries `source`/`verdict`/`content_hash`.
6. The builtin `import-skill` orchestrator drives the flow via the tools; the agent **cannot** install a `dangerous` or unconfirmed-code skill by ignoring the skill — the code refuses.

## §8 — Research basis (sources)

Design grounded in (not invented):
- **Hermes** (`tools/skills_guard.py`): 4 trust levels + (trust×verdict)→action matrix; 188-pattern deterministic regex scanner across 14 categories; quarantine→scan→approve; `audit.log` + `lock.json`; gate enforced in code; agent-created scan off-by-default (agent can already exec).
- **OpenClaw** (`src/agents/skills/frontmatter.ts`, `src/security/skill-scanner.ts`): deterministic install-spec validation (brew/node/go/uv/download safe-patterns — no `..`/`\`/`://`, HTTPS-only); regex code scanner with comment-stripping; `requires.{bins,anyBins,env,config}` gating.
- **Ecosystem** (URLs): [OWASP Agentic Skills Top 10](https://owasp.org/www-project-agentic-skills-top-10/); [Snyk ToxicSkills — ClawHub 800+ malicious](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/); [NVIDIA SkillSpector — 16 categories, two-stage](https://github.com/NVIDIA/SkillSpector); [SkillScan — 31k-skill study, 26.1% vulnerable](https://github.com/NMitchem/SkillScan); [Prompt-Shield — 75 zero-dep regex](https://github.com/LuciferForge/prompt-shield); [AgentShield — 102 rules](https://github.com/affaan-m/agentshield); [Sentinel-AI — 11 zero-dep scanners](https://github.com/MaxwellCalkin/sentinel-ai); [agentskills.io spec is security-silent](https://agentskills.io/specification); [NVIDIA Verified Skills / Ed25519 signing (v2 ref)](https://developer.nvidia.com/blog/nvidia-verified-agent-skills-provide-capability-governance-for-ai-agents/).
- **Key empirical findings driving the design:** prompt-injection-in-the-body is the #1 vector (91% of malicious skills) → scan the body, not just scripts; scripts are 2.12× more vulnerable → carries-code always confirms; deterministic regex catches ~85% with low false positives but **finite recall** → it feeds the human gate, it is not the gate.
