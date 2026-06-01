# Example 01 — First touch on a placeholder entity

## Scenario

Three episodic entries mention `person:marcelo` for the first time.
The canonical page exists only as a placeholder created by the
runner (just `type` + `name` + `aliases`). The patch builds the
attributes and relations from scratch.

## Input

```
ENTITY: person:marcelo

EXISTING PAGE (current canonical state):
---
type: person
name: Marcelo
aliases: []
---

EXISTING SCHEMA for this entity:
  attributes:
  relation types:

PENDING OBSERVATIONS (3):
- episodic/2026-05-23T10-12.md: "Marcelo (mmarmol@mxhero.com) joined
  the Q4 planning call."
- episodic/2026-05-24T14-30.md: "Marcelo lives in Spain — confirmed
  during the timezone discussion."
- episodic/2026-01-15T19-00.md: "Marcelo's wedding anniversary —
  married Susana in 2010."
```

## Expected output

```
===PATCH===
[
  {"op": "add", "path": "/attributes/email",
   "value": "mmarmol@mxhero.com",
   "provenance": "episodic/2026-05-23T10-12.md"},
  {"op": "add", "path": "/attributes/current_residence",
   "value": "Spain",
   "provenance": "episodic/2026-05-24T14-30.md"},
  {"op": "add", "path": "/relations/-",
   "value": {"to": "person:susana", "type": "spouse", "since": 2010},
   "provenance": "episodic/2026-01-15T19-00.md"}
]
===BODY_DELTA===
Marcelo is part of the Q4 planning rotation. Married to Susana since
2010.
===COMMIT===
Bootstrap Marcelo's canonical page

Three observations populate this entity for the first time: an email
identifier, the current residence, and the spouse relation.

Sources: episodic/2026-05-23T10-12.md, episodic/2026-05-24T14-30.md, episodic/2026-01-15T19-00.md
Cursor-after: 2026-05-24T14:30:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- Three `add` ops because all three facts are new — no `replace` is
  warranted on an empty page.
- The spouse goes under `/relations/-` (list append) per the JSON
  Patch reference; `to` is the canonical entity ref string.
- The body delta is short prose context that doesn't fit in
  attributes (the Q4 rotation fact has no obvious key).
- Subject line is ≤ 70 chars and names what changed.
- All three sources appear in the `Sources:` trailer.
- `Cursor-after` is the timestamp of the latest observation
  processed (the May 24 entry, not the January one — order is by
  timestamp, not by line order in the prompt).
