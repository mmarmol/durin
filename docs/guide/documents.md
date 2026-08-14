# Documents & your knowledge

durin can take in your documents — PDFs, Word/PowerPoint/Excel files, EPUBs, web
pages, notebooks, plain notes — and either answer about them on the spot or
**remember** them so you can come back to them later. This page is the user's
view of how that works; the mechanics live in
[Memory internals](../internals/memory/00_overview.md).

## Two things you can do with a document

There is a deliberate split, and durin picks based on what you ask:

- **Read it now.** "What does this contract say?", "Summarise this PDF." durin
  converts the file to clean text into the current turn, answers, and saves
  **nothing**. Good for one-off questions.
- **Remember it.** "Keep this," "learn this book," "remember this report." durin
  stores the document in your **Library**: the original is kept, and overnight it
  is understood (see below). Good for anything you will refer back to.

When it is ambiguous, durin reads it now and offers to keep it.

You give durin a document by **attaching it in chat** (the composer's attach
button in the web dashboard accepts documents, not just images), by pointing at a
**path on disk**, or just by asking it to read or remember a file you name.

**Supported formats:** PDF, Word (`.docx`), PowerPoint (`.pptx`), Excel
(`.xls`/`.xlsx`), EPUB, HTML, CSV, JSON, XML, Jupyter notebooks (`.ipynb`), and
ZIP archives of these. Scanned/image-only PDFs (which need OCR) and a few office
formats like ODT/RTF are not covered yet.

## The Library — kept apart from your everyday memory

Remembered documents go into a **Library** that is deliberately **separate** from
durin's day-to-day memory. A book's worth of raw text would otherwise drown out
the handful of facts you actually told durin about yourself and your work. So the
raw document text stays out of normal recall — but durin still **knows the
document exists** and can reach it when it is relevant.

Two things bridge a Library document back into everyday use:

- **What the document is about** surfaces in normal search. Overnight, durin pulls
  the key subjects, people, concepts, and cases a document covers into its regular
  knowledge — each carrying a pointer back to the source. So a normal question can
  turn up "durin learned *X* from this document," and pull the document from there.
- **A short catalog** of what you have ingested rides in durin's context, so it
  proactively knows the Library's contents without carrying their text.

## What "understanding" a document means

The night after you remember a document, durin's background **dream** does a few
things to it — no work happens while you wait:

- **Outlines it** — a whole-document summary plus a line per section, so durin can
  scan what the document covers without re-reading it.
- **Pulls out its key things** — the subjects, people, concepts, and cases it is
  about, linked back to the document as their source.
- **Files it under a topic** — the Library keeps a clean, maintained map of the
  subjects it covers (e.g. *"Covers: canine uroabdomen, paraprostatic cysts,
  vaccine reactions"*), so durin has a tidy sense of your whole library even as it
  grows.

## Finding and using a remembered document

Mostly, **just ask.** durin recognises when a question is about something you gave
it, searches the Library, shows you the relevant passage, and can pull the full
document when you need more. You do not have to remember which file it was in.

To **browse** everything, open the web dashboard's **Memory** page and switch to
the **Documents** tab: a searchable shelf of everything you have ingested, and
per document its outline, the things it taught durin, and its content.

## Forgetting a document

Ingested the wrong file, or done with one? Remove it either way:

- **Just ask** — "forget that document", "remove the handbook from your library".
- **In the dashboard** — open the document on the **Documents** tab and click the
  trash icon in its header.

Either way the document is archived (not hard-deleted) and dropped from search,
so durin stops surfacing it. Ingest it again anytime to bring it back. Note this
forgets the *source document*; a fact durin already distilled from it into your
memory is removed separately.

## A note on trust

Knowledge durin distils from a document is marked as coming *from* that document,
and it always yields to **you**: if something you stated and something a document
claims disagree, durin trusts you and says so.
