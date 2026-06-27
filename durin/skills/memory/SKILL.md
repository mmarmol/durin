---
name: memory
description: Markdown memory — entity pages, references, sessions, skills; author facts with memory_upsert_entity.
always: true
---

# Memory

durin's memory is Obsidian-compatible markdown under `memory/`:

- **`entities/<type>/<slug>.md`** — one page per *thing* (person, company,
  product, topic, project, place, …): its name, aliases, relations to other
  entities, the prose you wrote, and structured attributes the system
  extracts from that prose.
- **`references/<slug>.md`** — documents you ingested, kept whole. When an
  entity is distilled from one, link it with
  `memory_upsert_entity(derived_from=["reference:<slug>"])` so the source is
  reachable from the entity.
- **`sessions/`** — the conversation record. The system distils what matters
  into entities and summaries; you don't write here.
- **Skills** — procedures under `skills/<name>/SKILL.md`.

Author a fact about a thing with `memory_upsert_entity`, ingest a document
with `memory_ingest`, and **always `memory_search` before answering from
memory** rather than cold recall. (See your Memory instructions for the full
routing — search first to extend an entity instead of duplicating it.)
`memory_upsert_entity` **appends** the `body` by default (adds, never loses);
pass `body_mode="replace"` only to rewrite the whole body to correct it, with
the full current body in context. Read a whole entity page with
`memory_read_entity`, and remove a fact that is wrong with `memory_forget`.

## SOUL.md

`SOUL.md` is the **user's** control point for your personality and style. It
is user-authored — treat it as authoritative and never overwrite it.
