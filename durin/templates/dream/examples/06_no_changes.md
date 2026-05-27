# Example 06 — No-op: pending observations don't add anything new

## Scenario

The canonical page already covers everything the pending entries
describe. Per Rule 8 ("when in doubt, no-op is valid") the correct
output is an empty patch and a commit message explaining why.

## Input

```
ENTITY: person:marcelo

EXISTING PAGE (current canonical state):
---
type: person
name: Marcelo
aliases: [Marcelo Marmol]
attributes:
  email: mmarmol@mxhero.com
  current_residence: Spain
relations:
  - to: person:susana
    type: spouse
    since: 2010
---

EXISTING SCHEMA for this entity:
  attributes: email, current_residence
  relation types: spouse

PENDING OBSERVATIONS (2):
- episodic/2026-05-29T08-30.md: "Marcelo emailed mmarmol@mxhero.com
  about the Q4 release."
- episodic/2026-05-29T12-15.md: "Spoke to Marcelo from Spain about
  the deployment."
```

## Expected output

```
===PATCH===
[]
===BODY_DELTA===
===COMMIT===
No-op for Marcelo (pending observations re-affirm canonical)

Both pending entries re-state facts already present: the work email
and the current residence. No new attributes, relations, or
narrative material. The cursor advance from this pass alone is the
useful outcome — these entries are now consolidated and will not
re-surface.

Sources: episodic/2026-05-29T08-30.md, episodic/2026-05-29T12-15.md
Cursor-after: 2026-05-29T12:15:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- Empty array `[]` for the patch — this is the correct shape, not
  an omission of the section. The parser accepts an empty patch.
- Empty `===BODY_DELTA===` — nothing to append.
- The commit message still names what happened (a no-op pass) and
  cites the sources. Both pending entries are listed in `Sources:`
  because they were *processed*, even though no ops resulted.
- `Cursor-after:` is the timestamp of the latest entry processed,
  regardless of whether ops were emitted. This is critical — if
  the cursor didn't advance on a no-op, these entries would loop
  forever (per Rule 8 + spec §6.1 G2 invariant).
- The subject line names the no-op explicitly so a `git log --oneline`
  scan can identify which passes were definitionally empty.
