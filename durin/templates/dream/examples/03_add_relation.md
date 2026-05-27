# Example 03 — Add a relation to a populated page

## Scenario

Existing canonical page has attributes + one relation. A new
observation introduces a second relation.

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
relations:
  - to: person:susana
    type: spouse
    since: 2010
---

EXISTING SCHEMA for this entity:
  attributes: email
  relation types: spouse

PENDING OBSERVATIONS (1):
- episodic/2026-05-27T16-20.md: "Marcelo is the maintainer of the
  durin project — confirmed in the architecture review."
```

## Expected output

```
===PATCH===
[
  {"op": "add", "path": "/relations/-",
   "value": {"to": "project:durin", "type": "maintains",
             "intensity": "high", "since": "2024"},
   "provenance": "episodic/2026-05-27T16-20.md"}
]
===BODY_DELTA===
===COMMIT===
Add Marcelo → project:durin (maintains) relation

The May 27 architecture review confirmed Marcelo as the maintainer.
Since: 2024 is a best-guess from the surrounding context; refine on
a future observation if the precise date matters.

Sources: episodic/2026-05-27T16-20.md
Cursor-after: 2026-05-27T16:20:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- `add` to `/relations/-` appends to the list. The existing spouse
  relation is preserved untouched.
- `to: project:durin` follows the `<type>:<slug>` canonical form.
- Extra metadata keys (`intensity`, `since`) are allowed — they're
  free-form per relation per `01_data_and_entities.md` §3.5.
- The commit body documents the imprecise `since: "2024"` so a
  future maintainer doesn't mistake it for a verified date.
