# Example 02 — Replace an existing attribute (email change)

## Scenario

The canonical page already has `attributes.email = old@example.com`.
A new observation reports a different email for the same person.

## Input

```
ENTITY: person:marcelo

EXISTING PAGE (current canonical state):
---
type: person
name: Marcelo
aliases: [Marcelo Marmol]
attributes:
  email: marcelo@oldcompany.com
  current_residence: Spain
relations:
  - to: person:susana
    type: spouse
    since: 2010
---
Joined Q4 planning. Married Susana 2010.

EXISTING SCHEMA for this entity:
  attributes: email, current_residence
  relation types: spouse

PENDING OBSERVATIONS (1):
- episodic/2026-05-26T08-45.md: "Marcelo's new work email is
  mmarmol@mxhero.com — confirmed on the standup channel."
```

## Expected output

```
===PATCH===
[
  {"op": "replace", "path": "/attributes/email",
   "value": "mmarmol@mxhero.com",
   "provenance": "episodic/2026-05-26T08-45.md"}
]
===BODY_DELTA===
===COMMIT===
Update Marcelo's email (mxhero replaces oldcompany)

The May 26 standup confirmed the new work email. `replace` (not
`add`) because the canonical already carried an email value.

Sources: episodic/2026-05-26T08-45.md
Cursor-after: 2026-05-26T08:45:00Z
Entities-touched: person:marcelo
===END===
```

## Why this is the expected output

- `replace` not `add` because the `/attributes/email` path already
  exists. Using `add` over an existing path would still work in
  JSON Patch but is less expressive — `replace` makes the intent
  clear.
- No body delta: the email change doesn't need narrative context;
  the patch + commit body are sufficient audit.
- The old value isn't lost from git history — the commit shows the
  before/after via the .md diff, and `git log -p` recovers either
  value. We do NOT need a `valid_until` here because the new
  observation **explicitly contradicts** the old fact (per Rule 4).
