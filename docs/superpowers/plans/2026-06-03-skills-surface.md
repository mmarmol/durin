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
