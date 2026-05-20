Extract key facts from this conversation. Only output items matching these categories, skip everything else:
- User facts: personal info, preferences, stated opinions, habits
- Decisions: choices made, conclusions reached
- Solutions: working approaches discovered through trial and error, especially non-obvious methods that succeeded after failed attempts
- Events: plans, deadlines, notable occurrences
- Preferences: communication style, tool preferences

Priority: user corrections and preferences > solutions > decisions > events > environment facts. The most valuable memory prevents the user from having to repeat themselves.

Skip: code patterns derivable from source, git history, or anything already captured in existing memory.

Output as concise bullet points, one fact per line. No preamble, no commentary.
If nothing noteworthy happened, output only: (nothing)

After your bullet list (or "(nothing)"), output a `---` line on its own, then a YAML block with two keys:
- `entities`: list of named entities mentioned (people, projects, tools, file paths, concepts). Use the names exactly as they appeared. Empty list if none.
- `topics`: list of short topic labels (1-3 words each) for what was discussed. Empty list if none.

Example output:
- user prefers terse responses, no emojis
- decided to drop the deprecated cache layer
---
entities: [marcelo, durin, cache-layer]
topics: [communication-style, architecture-cleanup]
