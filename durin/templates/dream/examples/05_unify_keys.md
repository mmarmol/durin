# Example 05 — Detect duplicate keys and unify

## Scenario

The canonical page has accumulated `email` and `e-mail` as two
separate attributes for the same person (probably from prior
inconsistent Dream passes). One observation reaffirms the value
under one of the keys. This is an opportunity to unify per Rule 1
("coherence over rigidity").

## Input

```
ENTITY: person:marcelo

EXISTING PAGE (current canonical state):
---
type: person
name: Marcelo
aliases: []
attributes:
  email: mmarmol@mxhero.com
  e-mail: marcelo@personal.com
---

EXISTING SCHEMA for this entity:
  attributes: email, e-mail
  relation types:

PENDING OBSERVATIONS (1):
- episodic/2026-05-28T11-00.md: "Marcelo's primary work address is
  mmarmol@mxhero.com — he uses marcelo@personal.com for personal
  contacts."
```

## Expected output

```
===PATCH===
[
  {"op": "remove", "path": "/attributes/e-mail",
   "provenance": "episodic/2026-05-28T11-00.md"},
  {"op": "replace", "path": "/attributes/email",
   "value": {"work": "mmarmol@mxhero.com",
             "personal": "marcelo@personal.com"},
   "provenance": "episodic/2026-05-28T11-00.md"}
]
===BODY_DELTA===
===COMMIT===
Unify email/e-mail into a single typed attribute

The existing keys `email` and `e-mail` were synonyms holding two
distinct values (work + personal). The May 28 observation
distinguished the two roles, so this pass collapses them into one
canonical `email` attribute with `work` and `personal` slots.
Removed `e-mail` to eliminate the synonym.

Sources: episodic/2026-05-28T11-00.md
Cursor-after: 2026-05-28T11:00:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- Two ops, in order: first `remove` the synonym key, then `replace`
  the canonical key with a richer value that captures the
  semantically distinct addresses. The order matters — applied in
  sequence, both old values are accounted for in the new structure.
- This unification is justified by Rule 1: "if you notice two
  existing keys mean the same thing, unify them in this pass."
- The commit body explains the merge so a future reviewer
  understands why an attribute key was removed.
- Both ops cite the same `provenance` — the new observation is what
  surfaced the synonym and gave us the role split.
