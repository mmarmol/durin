---
name: import-skill
description: Import a skill from anywhere (local path, URL, GitHub repo, or a registry hit from skill_search) into durin through the security floor — scanned, gated, and audited before install.
metadata: {"durin":{"emoji":"📥"}}
---

# Import Skill

Bring a skill from anywhere — a local path, a direct `SKILL.md` URL, a GitHub
repo, or a registry hit — into durin through the security floor. Every import is
scanned and gated before install; nothing third-party lands in `skills/` unvetted.

## When to use

- "import the skill at `<path / url / repo>`"
- "install `<github repo>` as a skill"
- "add this skill from `<link>`"
- "find me a skill that does X" — search the registries first, then import the pick

## Flow

Drive everything through the `skill_import` tool. Never write to `skills/` yourself.

0. **No concrete source? Search first.** When the user wants a capability but has
   no path, URL, or repo in hand, `skill_search(query=...)` returns ranked hits
   from the configured registries, each with a `ref` (e.g. `clawhub:<slug>`,
   `github:owner/repo/<subdir>`). Pick with the user, then feed that `ref` through
   this same flow — search never installs.

1. **Resolve the source.** `skill_import(action="resolve", source="<what the user gave you>")`.
   A source is rarely a direct `SKILL.md`: a repo may hold many skills, and a
   link may need a look. If the result carries an `unresolved_reason`, the source
   is fuzzy — investigate it: use `web_fetch` to read the page or browse the repo,
   work out the concrete source (a `github:owner/repo/<subdir>` or a direct
   `SKILL.md` URL), and resolve again.

2. **Disambiguate.** If `resolve` returns several `candidates`, show the user the
   list (name + ref) and ask which one(s) they meant via `ask_user_question`.
   Never import them all silently — the user usually means one.

3. **Fetch into quarantine.** `skill_import(action="fetch", source="<the chosen candidate's ref>")`.
   This downloads the skill and runs the scan. Read back `verdict`, `findings`,
   and `needs`.

4. **Show the user what the scan found.** Surface the `verdict` and `findings`
   plainly (a short note, not necessarily a question). The `needs` field is
   informational — what the install gate will require next:
   - `needs == "allow"` → safe and trusted; install lands at once.
   - `needs == "confirm"` → it carries code, is caution, or comes from a source
     not on the allowlist.
   - `needs == "block"` → the scan found a serious risk (a prompt-injection, a
     fetch-and-execute, a destructive command). Only a person can accept this.

5. **Install (or discard).** `skill_import(action="install", name="<the quarantined name>")`.
   Nothing you pass authorizes a flagged install — the tool itself asks the user
   to approve it in this chat when it needs to; do not call `ask_user_question`
   for that. Read the `status` it returns:
   - `applied` → installed; tell the user.
   - `rejected` → the user declined. Continue without the skill; do not retry
     and do not reach the same effect another way (e.g. a manual copy).
   - `pending` → nobody could be asked right now (no live chat consumer). Tell
     the user it is waiting for their approval (`durin approvals`) and continue
     without it.
   - A refusal with `refused == "exists"` means a skill of that name is already
     installed. Show the user; re-run with `replace=true` only if they want to
     overwrite it.
   - Dependencies: with `skills.install_policy: auto` the skill's declared install
     specs run right after install; otherwise use `skill_install_deps(name=...)`,
     which dry-runs the exact commands for the user to approve.
   To discard instead: `skill_import(action="reject", name="<name>")`.

6. **It is usable immediately.** No restart or new session: the skill is indexed
   for `memory_search` at install time and `skill_view` loads it on demand. Offer
   to exercise it right away.

7. **Check composition.** The security gate says the skill is safe; it does not
   say it is well built. Compare the installed body against `list_workflows`:
   if it narrates by hand a procedure a local workflow automates (multi-source
   fan-out, gather, synthesize, verify), or walks a deterministic transformation
   that plainly wants a script, **propose** adapting it — keep its domain
   knowledge, delegate the orchestration to the workflow (or bundle the steps as
   a script) — and apply only if the user agrees. Never rewrite an import
   silently; the user chose this skill as-is.

## Rules

- The gate is enforced server-side, never by a value you pass: a flagged or
  dangerous skill is decided by policy, by the skills judge, or by the user —
  asked in this chat by the tool itself, or left waiting for approval
  (`durin approvals`) when nobody is reachable. An existing name will not be
  overwritten without `replace`.
- You surface the verdict and the reasons. The user approves. Trust is theirs to
  grant, never yours to assume.
