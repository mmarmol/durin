---
name: import-skill
description: Import a skill from anywhere (local path, URL, or GitHub repo) into durin through the security floor — scanned, gated, and audited before install.
metadata: {"durin":{"emoji":"📥"}}
---

# Import Skill

Bring a skill from anywhere — a local path, a direct `SKILL.md` URL, or a GitHub
repo — into durin through the security floor. Every import is scanned and gated
before install; nothing third-party lands in `skills/` unvetted.

## When to use

- "import the skill at `<path / url / repo>`"
- "install `<github repo>` as a skill"
- "add this skill from `<link>`"

Use this skill to import any agentskills.io skill from a path, URL, or GitHub
repo — always through the security scan and the human gate.

## Flow

Drive everything through the `skill_import` tool. Never write to `skills/` yourself.

1. **Resolve the source.** `skill_import(action="resolve", source="<what the user gave you>")`.
   A source is rarely a direct `SKILL.md`: a repo may hold many skills, and a
   link may need a look. If the result carries an `unresolved_reason`, the source
   is fuzzy — investigate it: use `web_fetch` to read the page, or the `github`
   skill to browse the repo, work out the concrete source (a
   `github:owner/repo/<subdir>` or a direct `SKILL.md` URL), and resolve again.

2. **Disambiguate.** If `resolve` returns several `candidates`, show the user the
   list (name + ref) and ask which one(s) they meant via `AskUserQuestion`.
   Never import them all silently — the user usually means one.

3. **Fetch into quarantine.** `skill_import(action="fetch", source="<the chosen candidate's ref>")`.
   This downloads the skill and runs the scan. Read back `verdict`, `findings`,
   and `needs`.

4. **Show the user, then gate.** Surface the `verdict` and `findings` plainly with
   `AskUserQuestion`:
   - `needs == "allow"` → safe and trusted; you may install without extra ceremony.
   - `needs == "confirm"` → it carries code, is caution, or comes from a source
     not on the allowlist. Ask the user to confirm before installing.
   - `verdict == "dangerous"` → the scan found a serious risk (a prompt-injection,
     a fetch-and-execute, a destructive command). Install is **blocked** unless the
     user **explicitly** tells you to force it. Decide nothing about this on their
     behalf — pass `override=true` only when the user has said, in their own words,
     to install it anyway.

5. **Check for duplicates.** Before installing, `memory_search` for an existing
   skill of the same name so you don't shadow one the user already has.

6. **Install (or discard).**
   `skill_import(action="install", name="<the quarantined name>", confirm=<true if confirmed>, override=<true only on explicit user force>)`.
   To discard instead: `skill_import(action="reject", name="<name>")`.

7. **Tell the user to start a new session** to load the newly installed skill.

## Rules

- The gate is enforced in code: a dangerous skill will not install without
  `override`, and a code / caution / un-allowlisted skill will not install
  without `confirm`. The tool refuses and tells you what it needs — relay that to
  the user; do not try to work around it.
- You surface the verdict and the reasons. The user approves. Trust is theirs to
  grant, never yours to assume.
