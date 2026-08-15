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
ZIP archives of these. A few office formats like ODT/RTF are not covered yet.

## Scanned PDFs

A PDF that is really a photograph of a page — a scanned book, an old contract,
anything with no text underneath the image — reads like any other document
once **local OCR** is turned on. It runs entirely on your machine, and it is
**off by default**: turn it on from the web dashboard's **Settings →
Documents**, where the toggle also notes roughly what it costs you in disk
space, memory, and processing time. Turning it on installs the OCR engine the
first time, so that switch is what enables the feature — not the setting on
its own. If the engine is ever missing while the setting says on (an install
that skipped it, say), durin still gives you whatever text the document does
have, and says the engine is the reason the rest is blank.

**What the built-in engine can read.** The models installed with OCR read
Chinese, Japanese, and Latin-script languages — English, Spanish, French,
German, Portuguese, and several dozen more — entirely offline. Scripts
outside that set fail in two different ways, and durin is honest about both.
A Cyrillic or Greek page comes back as confident-looking garbage: the engine
reads the letterforms as Latin lookalikes, and nothing in its output flags
the result as wrong. A page in Arabic — or any script the models cannot
match at all — comes back with no text; durin then checks whether the page
holds printed text at all, and reports that the engine detected printed text
but could not read it, instead of calling the page blank. A genuinely empty
page is still reported as blank.

**Reading other scripts.** For a script outside the built-in set, pick a
**recognition language** next to the OCR toggle in **Settings → Documents**
(the config key is `documents.ocr.language`): Arabic, Cyrillic, Devanagari,
Greek, East Slavic (Russian, Ukrainian, Belarusian), Korean, Tamil, Telugu,
or Thai. The first time a language is used, durin downloads its recognition
model — about 8 MB, plus about 11 MB of shared detection models the first
time any language is added — from **modelscope.cn** into
`~/.durin/models/ocr`, once; it survives updates and reinstalls, and your
documents never leave the machine. One language is active at a time: while
one is selected, scans in that script are read with its model, and switching
back to the built-in pack restores the original behavior — which needs no
network at all.

The engine also scores its own confidence in every line it reads, and durin
logs those scores to help diagnose a bad transcription — a per-document
summary when it transcribes while you wait, and a line per page in the
background worker's log. It never uses them to accept or reject one:
measured against real scans, plausible-but-wrong readings score in the same
range as legitimate noisy-but-correct ones, so no threshold can tell them
apart.

A short scan is read on the spot, the same as any other document. Ask durin to
remember a longer one — a whole scanned book — and it does not make you wait:
the original is kept right away and its pages are transcribed in the
background. Watch its progress from the dashboard's work panel, or just ask
durin how it's going.

Until that finishes there is no text to work with, and durin says so rather
than guessing at what the book contains. When it does finish, the book joins
your Library like any other document — searchable, and understood overnight
along with everything else.

See [Jobs internals](../internals/jobs.md) for how the background
transcription queue works.

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
