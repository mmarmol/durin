---
name: documents
description: How durin handles the user's documents and files — reading one now versus remembering it into the Library, and how to reach an ingested document later. Load when the user shares or points at a file (PDF, Word, Excel, PowerPoint, EPUB, a path on disk, an attachment), asks durin to read/summarize/learn/remember a document or book, or asks a question that a document they gave you earlier would answer ("what did that contract say", "per the handbook", "in the report you have"). Not for facts about a person/project (that is memory_upsert_entity) or web pages (web_fetch first).
metadata: {"durin":{"emoji":"📚"}}
---

# Documents

A document the user hands you is **source material**, not a fact about their world.
It has two homes and you pick based on what the user wants.

## Read now vs. remember

- **Read it now** → `convert_to_markdown(path)`. Returns the document as clean
  markdown into this turn so you can summarize, quote, or answer about it. It
  saves **nothing**. Use it for "what does this say", "summarize this", a
  one-off question about a file.
- **Remember it** → `memory_ingest(path)`. Stores the document in durin's
  **Library**: the original is kept, the text is chunked, and the nightly dream
  distills it into an outline plus entities. Use it for "keep this", "remember
  this", "learn this book", anything the user will refer back to.

When it is ambiguous, prefer reading now and offer to remember it:
"I've read it — want me to keep it in your library so you can ask about it later?"

## An attached file already carries its path — never ask for one

When the user **attaches** a document in chat, you receive its extracted text
inlined **and** its on-disk path in the marker:
`[File: <name> — saved on disk at <path>]`. So you have already read it. If the
user then asks to **remember / keep / save** it, call `memory_ingest("<path>")`
with that exact path — you have it. Do **not** tell the user the file "isn't on
disk" or ask them for a path; the attachment is persisted and the path is right
there in the marker.

## The one rule: never shovel a document into memory as a fact

Do **not** paste a document's text into `memory_upsert_entity` (or ask to
"remember" it by copying its contents into an observation). That is what the
Library is for. `memory_upsert_entity` is for a discrete fact about a *thing*
(a person, project, company) — not for source material. A whole document pasted
as a fact pollutes recall; ingested into the Library it stays out of the way
until asked for.

## Reaching an ingested document later

Ingested documents are deliberately **kept out of default recall** — a normal
`memory_search` will not return their raw chunks, so your everyday memory stays
clean. Two things bridge them back:

- **Distilled entities** from a document *do* surface in normal search. When an
  entity was distilled from (or references) a document, its block carries a
  **`Sources: reference:<slug>, …`** line. That line is the thread back to the
  source: drill any `reference:<slug>` to read the document behind the entity.
- The **document library catalog** in your pinned context lists what has been
  ingested (one line each). It tells you a document exists.

So when a question is about a specific document, or a `Sources:` line / the
catalog points at one, reach for it explicitly:

- `memory_search(query, scope="library")` — searches only ingested documents.
- `memory_drill("reference:<slug>")` — pull a whole ingested document (append
  `#<heading>` for one section); also drills a truncated chunk from a
  `scope="library"` hit.

## Forgetting a document

When the user wants an ingested document gone — "forget that", "remove that
document", "that was a mistake" — call `memory_forget("reference:<slug>")` with
the document's ref. It archives the whole document (reversible) and drops its
search-index rows, so it stops surfacing entirely. Do **not** `rm` files under
`memory/references/` by shell: that orphans the document's index rows. This
forgets the *source document*, not entities distilled from it — an entity the
dream already extracted stays until forgotten on its own.

## Provenance

Knowledge distilled from a document is stamped `derived_from` the document, and
is dream-authored. The user's own stated facts always take precedence over what
a document claims — if they conflict, trust the user and say so.

## Quick reference

| Situation | Do |
|---|---|
| "What does this PDF say?" / one-off | `convert_to_markdown(path)` |
| "Remember / keep / learn this doc" | `memory_ingest(path)` |
| Question about a doc you ingested before | `memory_search(scope="library")`, then drill |
| "Forget / remove that document" | `memory_forget("reference:<slug>")` |
| A fact about a person/project | `memory_upsert_entity` (never the document tools) |
| A web page | `web_fetch(url)` first, then decide read vs remember |
