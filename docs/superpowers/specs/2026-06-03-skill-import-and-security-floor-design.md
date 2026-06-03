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
1. `skill_import(source)` → read the lint Report.
2. If `needs_confirmation` (out-of-allowlist or carries code): use `AskUserQuestion` to show the source + the exact code artifacts + lint warnings, and get an explicit decision. Never auto-approve.
3. Dedup: `memory_search(kind="skill")` for overlap; if a near-duplicate exists, surface it (merge vs keep vs replace) instead of blindly adding.
4. *(Optional, Etapa-2 — judgment)* adapt foreign tool references to durin-native tools; the opt-out is "import as-is" (stop at Etapa-1).
5. `skill_import(source, confirm=true)` → install.

The skill is *guidance*; the **guarantees live in the code** (`install_imported_skill` refuses unconfirmed code/out-of-allowlist installs regardless of what the skill or the LLM does).

## §4 — The security floor §8.C (POLICY — review this)

**Principle:** import the *document* freely; **gate consciously accepting code or an untrusted source.** Quarantine + human review, not a fragile auto-scanner.

**The invariant (cannot be lowered by config or by the agent):**
- A skill that **carries code** (`scripts/` present, or an `install` spec) → install **requires confirmation**, always, regardless of source.
- A source **outside the allowlist** → install **requires confirmation**, always.
- Enforced in `install_imported_skill` (code refuses without `confirmed=True` when required) — so the agent/meta-skill **cannot rationalize past it**. Tested as an invariant.

**What config CAN do (only loosen the *source* check, never the code check):**
- `skills.import.allowlist: [<prefix>, …]` — trusted source prefixes (e.g. `github:anthropics/`, `github:NousResearch/`). A source matching the allowlist skips the *source* confirmation. **Default: empty** (nothing pre-trusted → first import from any source confirms; the user grows the list). The **code-carrying** confirmation has NO opt-out.

**What we deliberately do NOT claim:** a malware scanner. The confirmation **surfaces the actual scripts/commands** for the human to review (the human is the reviewer). An LLM-judge scan (flag exfil / destructive / prompt-injection patterns) is a **follow-up** that can only ADD friction, never remove the human gate. (Heuristic keyword scanners are explicitly avoided — fragile + multilingual-blind.)

**Provenance records the trust decision:** `metadata.durin.provenance = { source, imported_at, confirmed: true/false, carried_code: true/false }` — an audit trail of what was accepted and why.

**Scope of "code" in v1:** copying a skill's `scripts/` + flagging install specs, gated at import. **Running** an imported skill's script later goes through durin's normal exec (already user-controlled); v1's floor secures the *conscious-acceptance-at-import* moment. Per-execution gating of imported code, and *running installers* (brew/npm specs), are deferred follow-ups.

**Open policy choices flagged for the architect:**
1. Default allowlist empty (everything confirms) vs seed a few trusted orgs (anthropics, NousResearch). — I chose **empty** (safest; you add what you trust).
2. Imported skills default `mode=manual` (you review before the dream evolves them) vs `auto`. — I chose **manual**.
3. v1 gates at import-time only (not per-execution). — chosen for v1; per-exec is a follow-up.

## §5 — Scope

**In (v1):** sources = local path / direct URL / GitHub dir; `validate_skill` lint + code detection; the §8.C floor (allowlist + invariant code/source confirmation, enforced in `install`); quarantine; multi-file install with provenance + commit + index; the `skill_import` tool; the builtin `import-skill` orchestrator skill; dedup via `memory_search` (skill-guided).

**Deferred (own etapas):** §6.C marketplace/federated search + acquire-on-gap; running install specs (brew/npm) and per-execution code gating; LLM-judge security scan; automatic Etapa-2 adaptation; export/publish (the inverse of import).

## §6 — Rationale

- **Source-agnostic by construction:** §8.B already made every compliant skill drop-in. So import ≠ adapters; import = validate + gate + stamp. The architect's instinct, realized.
- **Skill-as-conductor, code-as-floor:** packaging the *procedure* as a skill is the right dogfooding (and the meta-skill seed), but security and reliability can't be prompt-deep. The `install` function is the single chokepoint that makes the §8.C floor a real, tested invariant rather than a guideline the LLM might skip.
- **Human review over auto-scan:** an honest gate that shows the user the code beats a scanner that claims to catch malice and misses it. We don't ship a false sense of safety.
- **Quarantine + provenance:** nothing untrusted lands in active `skills/` un-reviewed, and every import carries an audit trail of the trust decision.

## §7 — Success criteria

1. A standard skill from a local path / GitHub with **no code** and an **allowlisted** source installs with **no confirmation**, fully indexed and searchable.
2. A skill that **carries `scripts/`** (any source, even allowlisted) → `skill_import` returns `needs_confirmation` listing the scripts; `install_imported_skill(..., confirmed=False)` **refuses** (tested invariant); with `confirmed=True` it installs and provenance records `carried_code: true, confirmed: true`.
3. A skill from an **out-of-allowlist** source → confirmation required even with no code.
4. Adding the source prefix to `skills.import.allowlist` removes the *source* confirmation but **not** the code confirmation.
5. An imported skill round-trips (foreign frontmatter intact — already guaranteed by §8.B) and is `mode=manual` with `provenance.source` set.
6. The builtin `import-skill` orchestrator exists and drives the flow via the tools (the agent cannot install unconfirmed code by ignoring the skill — the code refuses).
