# Creation Process Detail

Supporting detail for the skill-creator process. Read the section the current step
points you to; skip the rest.

## Progressive disclosure patterns (step 3 and 5)

Skills load in three levels: metadata (always in context), SKILL.md body (on trigger),
bundled resources (on demand). Keep SKILL.md to essentials; split when approaching the
500-line budget. When splitting, reference each file from SKILL.md with a markdown link
and say when to read it — unreferenced files are undiscoverable.

**Pattern 1 — High-level guide with references.** SKILL.md holds the workflow and
selection guidance; per-topic depth lives in one reference file each, linked with a
one-line "read this when" rule.

**Pattern 2 — Domain/variant organization.** A skill covering multiple domains
(finance/sales/product) or variants (aws/gcp/azure) keeps only selection logic in
SKILL.md and one reference file per variant. The agent reads only the relevant one.

**Pattern 3 — Conditional details.** Basic content inline; advanced cases linked
("For tracked changes: see `references/redlining.md`").

Guidelines: keep references one level deep from SKILL.md; give files longer than 100
lines a table of contents at the top.

## Dry-run methodology (step 6: Verify)

Goal: observe the skill in use before shipping, with fresh eyes — yours are
contaminated by having written it.

1. Write 1-2 realistic prompts a user would actually type (concrete details, file
   paths, casual phrasing — not clean spec language).
2. For each prompt, dispatch a subagent whose instructions are only: the skill's
   SKILL.md path, the prompt, and where to save outputs. Do not explain the skill in
   the dispatch — the skill must stand alone.
3. Read the transcript, not just the outputs, and check:
   - Did it follow the skill, or improvise around it? Improvisations mark unclear or
     missing instructions.
   - Did it write helper code the skill does not bundle? That code was a missing
     script (rubric, block 1) — bundle it and re-run.
   - Did it load reference files at the right moments? If it missed one, the link or
     its "when to read" rule is weak.
   - Where did it waste tokens? Wasted effort marks prose that should be a script or
     dead weight to cut.
4. Fix and re-run until the dry-run is boring. Boring is done.

## Frontmatter notes (durin)

`name` and `description` are required; `metadata`, `always`, `license`, and
`allowed-tools` are accepted. Do not add other keys — run `scripts/quick_validate.py`
to see the enforced list. In durin, workspace skills live under the active workspace
`skills/` directory and are discovered automatically at runtime.
