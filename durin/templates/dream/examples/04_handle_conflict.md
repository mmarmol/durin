# Example 04 — Two pending observations contradict each other

## Scenario

The pending batch contains two observations about the same attribute
(`current_residence`) that conflict. The earlier observation says
Spain, the later says Portugal. Resolve via temporal validity rather
than overwriting silently.

## Input

```
ENTITY: person:marcelo

EXISTING PAGE (current canonical state):
---
type: person
name: Marcelo
aliases: []
attributes:
  current_residence: Spain
---

EXISTING SCHEMA for this entity:
  attributes: current_residence
  relation types:

PENDING OBSERVATIONS (2):
- episodic/2026-05-24T14-30.md: "Marcelo lives in Spain — confirmed
  during the timezone discussion."
- episodic/2026-05-27T09-00.md: "Marcelo moved to Portugal last
  week."
```

## Expected output

```
===PATCH===
[
  {"op": "replace", "path": "/attributes/current_residence",
   "value": {"value": "Portugal",
             "valid_from": "2026-05-20",
             "previous": {"value": "Spain",
                          "valid_until": "2026-05-20"}},
   "provenance": "episodic/2026-05-27T09-00.md"}
]
===BODY_DELTA===
Marcelo relocated from Spain to Portugal in mid-May 2026 (move date
inferred from "last week" relative to the May 27 observation).
===COMMIT===
Marcelo moved Spain → Portugal (mid-May 2026)

The two pending observations are NOT contradictions — they describe
two states across time. Encoded as a temporal attribute (current
value + previous value) rather than a flat string, so the audit
trail survives in the structured frontmatter, not just in git log.

Sources: episodic/2026-05-27T09-00.md
Cursor-after: 2026-05-27T09:00:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- Per Rule 4 ("preserve by default"), don't blindly overwrite the
  Spain fact. The newer observation explicitly contradicts the
  older state but does NOT erase the historical fact.
- The attribute value becomes a small dict with `value`,
  `valid_from`, and `previous`. Free-form values are allowed in
  `attributes` (the schema spec — `01_data_and_entities.md` §3.5 —
  says scalar or nested dict).
- Only the later observation needs to be in `provenance` for the
  `replace` op — the earlier one is implicitly captured in
  `previous`.
- The body delta gives a one-sentence prose summary so a reader of
  the entity page sees the transition without parsing the dict.
- Only the May 27 entry appears in `Sources:` because the May 24
  entry's information was already in the canonical (no new op was
  generated for it).
