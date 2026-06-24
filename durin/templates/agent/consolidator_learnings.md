Extract DURABLE LEARNINGS about how to work with the user — preferences, corrections,
standing constraints, and stable personal facts — from this conversation span.

Include only:
- Stated preferences and communication style ("user wants replies in Spanish", "prefers
  brief answers over verbose ones", "code blocks must be copy-pasteable without comments")
- Explicit corrections or standing constraints the user applied to the agent's behavior
- Durable facts about who the user is (role, domain expertise, working context)
- Stable commitments or rules the user declared ("always ask before releasing", "TDD first")

Exclude:
- Anything derivable from the code or repository (git history, file contents, configs)
- Task progress, todo items, transient state, and ephemeral artifacts
- Single-session observations that show no sign of being durable preferences
- Routine decisions, findings, and blockers (those belong in the decision log)

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
- person:<slug>     — a stable fact about who the user is

The body must state: what the preference/fact is, WHY it matters, and HOW to apply it.
Keep each body under 400 characters.

If no durable learnings appear in the span, output exactly: []
