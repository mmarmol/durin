# Commit message format

Your `===COMMIT===` section becomes a git commit message. Format:

```
<subject, max 70 chars>

<optional body, explaining non-obvious decisions>

Sources: <comma-separated entry paths or IDs>
Cursor-after: <ISO timestamp of latest entry processed>
Entities-touched: <entity_id>
```

`Trigger:` and `Run-id:` trailers are added by the runner
automatically. Do not include them in your output.

If you omit one of the LLM-supplied trailers
(`Sources` / `Cursor-after` / `Entities-touched`), the runner fills it
in from its state and logs a warning. Prefer to include them.

## Examples

Good:

```
Update Marcelo's email and add spouse relation

Two observations confirmed the email change from the May 23
conversation and introduced the spouse relation from a 2010 episodic.

Sources: episodic/2026-05-23T10-12.md, episodic/2026-01-15T19-00.md
Cursor-after: 2026-05-23T10:12:00Z
Entities-touched: person:marcelo
```

Bad (missing trailers, opaque subject):

```
Updated email
```
