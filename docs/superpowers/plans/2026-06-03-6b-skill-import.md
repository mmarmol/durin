# §6.B — Skill Import + Quarantine (Module 3 of 3)

> **OUTLINE** (detailed when built — last, after §8.C floor + the Skills Surface). The feature that CONSUMES the §8.C floor and FILLS the Skills-Surface quarantine. Spec: [`2026-06-03-skill-import-and-security-floor-design.md`](../specs/2026-06-03-skill-import-and-security-floor-design.md) §1-§3, §5.

**Goal:** Import any agentskills.io skill (local path / URL / GitHub) — source-agnostic — through the §8.C floor: fetch → quarantine → scan → trust×verdict gate → audited install. The gate is enforced in code (`install_imported_skill` refuses block-without-override and confirm-without-confirmation).

**Tasks (outline):**
1. `fetch_skill(source, *, quarantine_root)` → `.durin/import-quarantine/<name>/` — `path://` first-class; `https://…/SKILL.md` + `github:owner/repo/path` via durin's HTTP helper (stub in tests). Persist a `.scan.json` (verdict+findings+source) next to the quarantined skill so the Surface can list it with reasons.
2. `install_imported_skill(workspace, quarantine_dir, *, source, allowlist, confirmed=False, override=False)` — THE INVARIANT: runs `validate_skill`+`scan_skill`, `decide_action`; `block`→refuse unless `override`, `confirm`→refuse unless `confirmed`; else copytree → stamp `metadata.durin.provenance{source,verdict,confirmed,overridden,content_hash}` + `mode=manual` → commit → `_sync_index` → append `.durin/import-audit.log`. `SkillImportRefused`. Tests = the refusal invariants (incl. dangerous→block-without-override) + provenance/hash/audit.
3. `skill_import(source, confirm=false, override=false)` tool — fetch+scan, return `{needs_confirmation: action, verdict, findings, code_artifacts}` or install. Wire the Surface's `import`/`approve`/`reject` actions to it (approve = install the quarantined skill with the user's confirm/override).
4. Builtin `durin/skills/import-skill/SKILL.md` orchestrator (§5.6 meta-skill seed): `skill_import(source)` → surface verdict+findings via `AskUserQuestion` (dangerous needs explicit user force=override) → dedup `memory_search(kind="skill")` → optional Etapa-2 adapt → install. The code refuses regardless of the LLM.
5. Quarantine UX wiring: the Surface's Quarantine section now lists real entries (from `.durin/import-quarantine/`) with verdict/findings + approve(install)/reject(delete).
6. VERIFY LIVE: real Hermes code-bearing skill (confirm flow) + crafted malicious skill (dangerous→block→override) + clean allowlisted (no confirm) + the whole flow visible in chat slash + web quarantine panel.

**Reuse:** §8.C `scan_skill`/`decide_action`; E1 store (`_store_init`/`auto_commit`); Spec-2 `_sync_index`; §8.B round-trip fidelity (foreign frontmatter survives the import stamp); the Skills-Surface for listing/approve.
