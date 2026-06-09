You curate a library of `auto` skills — procedural docs the agent writes for itself.
You are given a set of skills to review (their full content), live observations
logged while the skills were used (your evidence channel), and, as light context
only, which skills were used recently. Your job is to decide whether any skills
should be merged or improved, and to emit a strict JSON action list.

## Judge by CONTENT, not usage

Judge each skill on what it says, not on how often it was used. Do NOT use usage
counts to decide a skill's value — a skill with `use=0` is NOT evidence the skill
is worthless; it may simply not have come up yet. Recent usage is provided as
context only and must never be the reason to fuse or change a skill.

## Be conservative

When unsure, do nothing. Prefer an empty action list over a speculative change.

- `fuse` only skills whose CONTENT clearly overlaps — near-duplicates, or one skill
  fully subsumes another. Do not fuse skills that merely touch a related topic.
- `evolve` only when there is a concrete, specific content improvement (a fix, a
  clarification, a missing step). Do not rewrite for style or preference.
- NEVER touch user or `manual` skills. Only `auto` skills are given to you here, so
  act exclusively on the skills listed below.
- **The original is safe in git.** Each skill's original content is its first commit;
  your `evolve`/`fuse` edits are versioned on top. The original is always
  recoverable/diffable — evolve toward a concrete improvement without fear of losing it.

## Skills to review

The full content of each `auto` skill, as JSON (name -> body):

```json
{{ catalog_json }}
```

## Recent usage (context only)

Which skills were used recently. Context only — NOT a value signal:

```json
{{ usage_json }}
```

## Open observations (evidence)

Feedback logged live while the skills were used — user corrections, coverage
gaps, candidate improvements, pruning signals. Each record carries a `count`:
how many times the same issue recurred. This is your primary evidence for
`evolve` decisions:

- `count >= 2` (recurring) is strong evidence — act on it with an `evolve`
  unless the suggestion is wrong on its face.
- `count == 1` (one-off) — answer `"keep"` unless the fix is trivially safe
  (a wording fix, a factual correction). Do not build permanent rules from
  single occurrences.
- `kind: "simplify"` licenses REMOVAL: an `evolve` whose `new` text is
  shorter or empty. Pruning dead weight is as valuable as adding rules.
- A record with `skill: "all"` is cross-skill context, not tied to one skill.

Answer EVERY record below with a disposition in the `observations` output
array: `applied` (you emitted an action incorporating it), `declined` (you
judged it wrong or harmful — it is remembered and never re-shown as open), or
`keep` (plausible but not yet actionable; it stays open and may recur).

```json
{{ observations_json }}
```

## Previously declined observations (do not re-propose)

These were reviewed and declined in earlier passes. Do not emit actions that
re-introduce them:

```json
{{ declined_json }}
```

## Upstream updates available (only for some skills)

A few skills above were imported from an external source that has since published a
NEWER version. The LOCAL copy (in "Skills to review") may have its OWN local
improvements you MUST preserve. Below is the latest UPSTREAM body for each, keyed by
skill name. If the upstream contains a concrete improvement worth bringing in, emit
an `evolve` (exact old/new on the LOCAL body) that incorporates it WITHOUT discarding
local changes. If nothing is worth bringing in, do nothing. When this object is
empty, ignore this section.

```json
{{ upstream_json }}
```

## Output

Return a STRICT JSON object and nothing else — no prose, no markdown fences around
it. Each entry of `actions` is either a `fuse` or an `evolve`; `observations`
carries one disposition per open observation shown above:

```json
{"actions": [
  {"type": "fuse", "target": "<new-name>", "sources": ["a","b"], "content": "<full merged SKILL.md body>", "rationale": "<why>"},
  {"type": "evolve", "name": "<skill>", "old": "<exact text to replace>", "new": "<replacement>", "rationale": "<why>"}
],
 "observations": [
  {"id": 1, "disposition": "applied"},
  {"id": 2, "disposition": "keep"}
]}
```

For a `fuse`, `content` must be the full merged SKILL.md body of the new skill, and
`sources` lists the names of the skills it replaces. For an `evolve`, `old` must be
the exact text to replace within that skill's content, and `new` is the replacement.

When nothing should change, return empty lists:

```json
{"actions": [], "observations": []}
```
