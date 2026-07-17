Extract key facts from this conversation. Only output items matching these categories, skip everything else:
- User facts: personal info, preferences, stated opinions, habits
- Decisions: choices made, conclusions reached
- Solutions: working approaches discovered through trial and error, especially non-obvious methods that succeeded after failed attempts
- Locations: files, directories, commands, or URLs discovered or examined that would be needed to find a resource again. Always keep the full path exactly as it appeared.
- Events: plans, deadlines, notable occurrences
- Preferences: communication style, tool preferences

Priority: user corrections and preferences > solutions > locations > decisions > events > environment facts. The most valuable memory prevents the user from having to repeat themselves.

Skip: implementation details that re-reading a file already listed under Locations would recover; git history; anything already captured in existing memory.

Output as concise bullet points, one fact per line. No preamble, no commentary.
If nothing noteworthy happened, output only: (nothing)

After your bullet list (or "(nothing)"), output a `---` line on its own, then a YAML block with two keys:
- `entities`: list of typed entity references, each shaped `<type>:<value>` with a lowercase type — e.g. `person:marcelo`, `project:durin`, `tool:pipx`, `file:docs/guide/install.md`, `concept:compaction`. Use the value exactly as it appeared. Empty list if none.
- `topics`: list of short topic labels (1-3 words each) for what was discussed. Empty list if none.

Example output:
- user prefers terse responses, no emojis
- decided to drop the deprecated cache layer
- support-triage skill lives at workspace-legolas/skills/zendesk-ticket-evaluation/SKILL.md
---
entities: [person:marcelo, project:durin, file:src/cache_layer.py]
topics: [communication-style, architecture-cleanup]
