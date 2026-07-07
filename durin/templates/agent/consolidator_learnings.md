Extract DURABLE LEARNINGS about how to work with the user — preferences, corrections,
standing constraints, and stable personal facts — from this conversation span.

KNOWN LEARNINGS — reuse, do not duplicate:
Each existing learning is shown below with its EXACT ref and its FULL current body.
If a learning you find already exists here, output its EXACT ref — its body will be
REPLACED by what you return, so return a REFINED body that PRESERVES the still-valid
content of the current one and folds in the new nuance. Do NOT drop what the current
body already says. Only mint a new ref for a genuinely new learning.

EXISTING (ref — name, then full body):
{{ existing }}

Include only:
- Stated preferences and communication style ("user wants replies in Spanish", "prefers
  brief answers over verbose ones", "code blocks must be copy-pasteable without comments")
- Explicit corrections or standing constraints the user applied to the agent's behavior
- Stable commitments or rules the user declared ("always ask before releasing", "TDD first")

Exclude:
- Anything derivable from the code or repository (git history, file contents, configs)
- Task progress, todo items, transient state, and ephemeral artifacts
- Single-session observations that show no sign of being durable preferences
- Routine decisions, findings, and blockers (those belong in the decision log)
- Content the user merely SHOWED, not asserted: third-party quotes/reviews,
  advertisements or marketing copy, transcribed audio samples, pasted documents

Output a JSON array of objects, one per durable learning:

[
  {
    "ref": "<type>:<slug>",
    "name": "<short display name>",
    "body": "<what the learning is, why it matters, and how to apply it>"
  }
]

For "ref", use one of these types:
- feedback:<slug>   — preference, correction, or rule about how to work with the user
- stance:<slug>     — a durable principle or architectural position the user holds
- practice:<slug>   — a durable workflow or process habit the user follows

The body must state: what the preference/fact is, WHY it matters, and HOW to apply it.
Keep each body under 400 characters.

If no durable learnings appear in the span, output exactly: []
