# JSON Patch operations reference

You emit JSON Patch ops (RFC 6902) over the entity page's frontmatter.
The output is a single JSON array between `===PATCH===` and the next
section marker. Each element is one op object. Allowed ops below.

Every op MUST carry a `provenance` field — a string pointing to the
source observation that justified the op (its path or id, e.g.
`episodic/2026-05-23T10-12.md`). Ops without `provenance` will be
rejected.

## `add`

Adds a value at a path. Use for new attributes, new relations, new
aliases.

```json
{"op": "add", "path": "/attributes/email", "value": "marcelo@mxhero.com",
 "provenance": "episodic/2026-05-23T10-12.md"}
```

For appending to a list, use `-` as the index:

```json
{"op": "add", "path": "/relations/-", "value": {
  "to": "person:susana", "type": "spouse", "since": 2010
}, "provenance": "episodic/2026-01-15T19-00.md"}
```

For aliases:

```json
{"op": "add", "path": "/aliases/-", "value": "Marcelo Marmol",
 "provenance": "episodic/2026-05-23T10-12.md"}
```

## `replace`

Replaces a value at an existing path. Use when an attribute changes.

```json
{"op": "replace", "path": "/attributes/current_residence", "value": "Spain",
 "provenance": "episodic/2026-05-25T09-14.md"}
```

## `remove`

Removes a value. Use sparingly — only when an observation EXPLICITLY
contradicts the existing data. Prefer adding `valid_until` or unifying
keys instead.

```json
{"op": "remove", "path": "/attributes/old_role",
 "provenance": "episodic/2026-05-26T15-00.md"}
```

## Common pitfalls

- **Always include `provenance`.** Without it the op is rejected.
- **Paths use JSON Pointer syntax.** `/` is the separator; literal `/`
  in keys must be escaped as `~1`, literal `~` as `~0`.
- **Allowed path roots only**: `/attributes/*`, `/relations/*`,
  `/aliases/*`. Internal fields (`dream_processed_through`,
  `created_at`, `updated_at`, `type`, `name`) are managed by the
  runner — touching them gets the patch rejected.
- **Order matters** within the patch array — ops apply sequentially.
  Don't `replace` a path you haven't created (`add`) earlier in the
  same patch.
- **Empty patch is valid.** If the pending observations don't add
  anything new, emit `===PATCH===\n[]\n` and explain the no-op in the
  commit body.
