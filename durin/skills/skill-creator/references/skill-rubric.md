# Skill Quality Rubric

The contract for what a good durin skill looks like. Cited by the skill-creator
process; designed to also be cited by autonomous curation (dream) judges.

## 1. Scriptability test (the central criterion)

For each capability the skill promises, ask: **are the branches and inputs closed?**

- Closed branches, fixed sequence: write a **script** in `scripts/`. Prose only says
  when to invoke it and what its output means.
- Closed branches, parameterizable: script with arguments. Prose explains the
  parameters, not the steps.
- Requires runtime judgment (context-dependent, multiple valid approaches): **prose**,
  and explain the why behind each instruction so the agent can generalize. No naked
  MUSTs.

The burden of proof is on prose: every step-by-step instruction block must justify why
it is not a script. If you can't articulate the judgment the agent must exercise
mid-procedure, it's a script.

**Subagent rule:** delegating to a subagent is justified only when the delegated task
itself requires judgment (or context isolation). If the subagent would just follow a
fixed recipe, replace it with a script or threaded tool calls.

**Empirical signal:** if during verification (dry-run) the agent writes helper code the
skill does not bundle, that code was a missing script. Bundle it and re-verify. The same
applies post-ship: repeated similar code across sessions using the skill means the skill
is missing a script.

## 2. Output discipline (token economy)

Skill responses and bundled scripts are read by an agent paying per token.

Every bundled script must:
- Print the minimum the agent needs to decide its next step. Silence is the correct
  output for success when there is nothing to decide (exit code 0 carries the message).
- Document its exit codes (0 = success).
- Emit one-line, actionable errors (what failed + what to do), not stack traces, unless
  `--verbose` is passed.
- Use compact JSON (no pretty-printing) when output is structured for the agent.
- Be verbose only behind an opt-in flag (`--verbose`), never by default.

## 3. Trigger-query methodology (description design)

The description is the skill's only triggering mechanism, and agents under-trigger
skills. Before the description is final:

1. Write ~10 realistic queries that MUST trigger the skill: formal and casual phrasings,
   with typos, with file paths and concrete details, without naming the skill.
2. Write ~10 near-misses that must NOT trigger: genuinely hard neighbors that share
   keywords but need something else — not obviously irrelevant queries.
3. Read the description against each query and adjust until the boundary is right.

Description requirements:
- Third person. What the skill does AND when to use it — all "when to use" information
  lives here, never in the body (the body loads only after triggering).
- Deliberately pushy: name the user phrases and contexts that should pull the skill in,
  including the "even if the user doesn't say X" cases.
- English only, like the rest of the skill. Skills are authored entirely in English —
  name, description, body, scripts, comments — regardless of the user's language.
  Triggering is done by a multilingual model, so instead of duplicating trigger
  phrases per language, state that the triggers apply "regardless of the language the
  user writes in".

Bad query for the MUST list: "Create a skill" (too clean). Good: "ok the thing I do
every monday with the sales csv, can we turn that into a skill or something so i don't
have to explain it every time".

## 4. Structure and conventions

- Recommended section skeleton for created skills: Overview, actionable body sections,
  Common Pitfalls (numbered mistakes + fixes), Verification Checklist (post-action
  checkboxes).
- Before creating: read 2-3 neighboring skills in the target catalog. Prefer extending
  an existing skill over creating a narrow sibling.
- Real pointers to bundled resources are markdown links — `[rubric](references/skill-rubric.md)`
  — and the validator checks they resolve. Illustrative paths go in inline code and are
  not validated.
- Mechanical rules (frontmatter keys, naming, description limits, allowed directories,
  script syntax) are NOT restated in prose anywhere: `scripts/quick_validate.py` is the
  source of truth. Run it; fix what it reports.
- Optional: cross-reference sibling skills via `metadata` when it genuinely helps
  discovery. Not required.
