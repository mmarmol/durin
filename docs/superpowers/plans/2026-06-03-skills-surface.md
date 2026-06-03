# Skills Surface — exposure in web + both chats (Module 2 of 3)

> **OUTLINE** (detailed when built — after §8.C, before §6.B). The shared presentation/control layer over the skills system. Builds on E1 (`/skills`, `SkillsSettings` web panel, `list_skills_info`) + §8.C audit. Built in the middle so §8.C's audit gets a surface immediately on existing skills; §6.B then fills the quarantine section.

**Goal:** One inventory + actions service, rendered in the **web panel** and **both chats** (TUI + webui chat). Nothing implemented three times.

**Shared backend (one source of truth):**
- `skills_inventory(workspace) -> [{name, source, mode, provenance, status: "active"|"quarantined", verdict, findings}]` — extend E1's `list_skills_info` with `status` + lazily-computed/cached `verdict`+`findings` (from §8.C `scan_skill`). Quarantined entries read `.durin/import-quarantine/<name>/.scan.json`.
- Actions: `audit(name)` (run §8.C scan), `approve(name)` / `reject(name)` (resolve a quarantined skill — §6.B fills these), `import(source)` (delegates to §6.B's tool).

**Tasks (outline):**
1. `skills_inventory()` service + `skill_status`/verdict caching (extend `list_skills_info`). Tests.
2. **Chat (both)**: extend the `/skills` slash command — `list` (active + status badge), `audit <name>`, `quarantine` (list quarantined + reasons), `approve|reject <name>`. The `skill_audit` tool (§8.C) is already the agent-driven path. Tests against the command builtin.
3. **Web panel**: extend `SkillsSettings.tsx` — verdict badge per skill + a **Quarantine** section (findings + approve/reject buttons) + an Audit button. New GET `/api/skills` fields (status/verdict/findings) + POST `/api/skills/<name>/approve|reject`. (React + the websockets channel routes — mirror the existing skills web routes from E1.)
4. Verify-live: list + audit an existing skill via CLI, chat slash, and the web endpoint; the quarantine section renders empty (no imports yet) without error.

**Boundary:** read/act only; the floor logic is §8.C, the import action is §6.B. The quarantine list + approve/reject are scaffolded here and **filled** by §6.B.

---

## Status — SHIPPED 2026-06-03

**Backend** (commits `5eebb7c`, `6b6713a`, `498117e`): `skills_inventory`/`quarantined_skills` read model; CLI `durin skill list|quarantine|audit`; web `GET /api/skills` (now carries `status`/`verdict`/`findings`) + `GET /api/skills/quarantine`; chat tools `skill_audit` + `skills_list`.

**Frontend** (commit `daca080`): `SkillsView.tsx` + `api.ts` (`SkillVerdict`/`SkillFinding`/`QuarantineRow`, `listQuarantine`) + en/es i18n. Verdict badge per skill (caution|dangerous only — safe shows none), an **Active/Quarantine** tab bar, the Quarantine list with inline findings, and a Security section in the active-skill detail (severity-colored). Tests: `skills-api` + `skills-view`; webui suite green; tsc+vite clean. **Verified live** against the real `WebSocketChannel` serving the real `durin/web/dist` bundle (flagged active skill → Peligrosa + findings; quarantined skill with curl|bash + injection reasons; 0 console errors).

**Design decisions:**
- Safe skills get **no** verdict badge (absence = safe); a green chip on every row is noise.
- Findings render in two contexts via one `FindingsList`: the active-skill detail (audit view) and inline on each quarantine row.
- i18n: en + es only; the other 7 locales fall back to en rather than ship English mislabeled as translated.

**Deferred to §6.B (Module 3):** the quarantine **approve/reject** actions. They need backend POST handlers (`install_imported_skill` / delete-quarantine) that don't exist yet, and the quarantine is empty until import lands. The read surface (list + reasons + empty state) is complete; Module 3 wires the two buttons onto the existing quarantine rows.
