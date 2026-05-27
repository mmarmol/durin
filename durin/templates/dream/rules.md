# Dream consolidator rules

These rules govern what you may and may not do when emitting the
`===PATCH===` / `===BODY_DELTA===` / `===COMMIT===` sections. They
exist because the apply pipeline is unforgiving — a bad patch is
rolled back, the cursor does not advance, and the same entries come
back at you on the next pass.

## Rule 1 — Coherence over rigidity

Prefer existing attribute keys and relation types when the new
information has the same semantic meaning. Do not invent `e-mail` if
`email` is already present. BUT: if you notice two existing keys mean
the same thing, **unify them in this pass** — emit ops that move both
sources of truth onto one canonical key. Coherent evolution, not
preservation.

## Rule 2 — Single entity per pass

Your task is to update ONE entity (the `entity_id` in the prompt). If
a pending observation mentions a different entity, do NOT include
it in this pass's PATCH. It will be processed in its own pass.

## Rule 3 — Provenance is non-negotiable

Every PATCH op must include a `provenance` field pointing to the
source entry (its `id` or path) that justified the op. Without
provenance, the op will be rejected by the apply pipeline.

## Rule 4 — Preserve by default

Do NOT remove attributes or relations unless an observation EXPLICITLY
contradicts them. When in doubt:

- Append history via `valid_from` / `valid_until` fields inside the
  attribute or relation, instead of overwriting.
- Add the new fact alongside the existing one, with notes in the
  body delta.
- Emit a `remove` op only when an observation says "this is no longer
  true" or equivalent.

## Rule 5 — Respect recent decisions

The RECENT GIT HISTORY block in the prompt shows commits to this
entity in the last 30 days. If a recent commit updated something, be
cautious about reverting that update based on older observations.
Newer evidence wins; older evidence enriches.

## Rule 6 — Body delta is for prose, not data

The `===BODY_DELTA===` section is appended to the entity's narrative
body. Use it for prose context that doesn't fit attributes or
relations: relationships between facts, anecdotes, important context.
Leave it empty when the patch alone tells the whole story.

## Rule 7 — Commit message is the audit trail

The `===COMMIT===` section becomes a git commit message. Format per
`commit_format.md`. Subject ≤ 70 chars. The body of the message
should explain non-obvious decisions you made (why `replace` vs
`add`, why you didn't merge two keys, why a `remove` was justified).

## Rule 8 — When in doubt, no-op is valid

If the pending observations don't add new facts beyond what's already
in the canonical page, emit an empty patch (`[]`), an empty body
delta, and a commit message that says so. This is a successful pass
— the runner advances the cursor and moves on.
