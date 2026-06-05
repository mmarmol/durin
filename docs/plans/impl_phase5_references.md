# Phase 5 — References

> Builds on Phases 1-4. Branch `memory-redesign-phase1`.

**Goal:** Coherent ingested documents are kept WHOLE as REFERENCE pages (never
synthesized/chunked-away by dream), with a token-aware chunk index for
retrieval that points back to the whole doc (design §2.3/§2.8). Closes the
Phase 3 "references as input to extract" deferral.

**Built:** `durin/memory/reference.py`
- `ingest_reference(workspace, title, content, *, source)` — writes
  `memory/references/<slug>.md` (the whole doc + `type: reference` frontmatter:
  title/source/ingested_at/chunk_count) and a `<slug>.chunks.jsonl` sidecar.
- `chunk_by_tokens(text, max_tokens=512)` — greedy token-aware chunking
  (`estimate_text_tokens`/tiktoken): pack paragraphs ≤512 tok (the e5-small
  embedder's max_seq); split an oversize paragraph by sentence, then char.
- Each chunk record carries a `parent` pointer back to the reference, so a
  fragment hit can pull the whole document.
- `load_reference` / `reference_chunks` / `reference_marker` (the REFERENCE
  structural marker, distinct from FRAGMENT).

**Verified:**
- 5 unit tests: chunk budget (all chunks ≤512 tok); whole-doc preservation
  (verbatim, with frontmatter); chunk parent pointers; short-doc single chunk;
  marker format.
- **LIVE (glm-5.1):** ingested a real Globex profile → kept whole as
  `reference:globex-profile` + chunked; the extract dream read the reference as
  input and authored `company:globex` with **12 attributes**, every one
  `author=dream` with provenance `source_ref → reference:globex-profile`. The
  reference is preserved AND usable; experience→knowledge now works from
  references, not just sessions.

**Deferred (follow-on) — design §2.8/§6.2:**
- **Index wiring:** plug the whole doc into FTS and the chunks into the vector
  index so `memory_search` surfaces references; surface a chunk hit with the
  REFERENCE marker + parent so the LLM can pull the whole doc. (This module owns
  the storage model; the search-stack integration is Phase 6 territory.)
- **Tool wiring:** point `memory_ingest` at `ingest_reference` (today it chunks
  to `corpus` entries at ~1500 chars with no parent doc) + update its
  description (§6.2). FRAGMENT marker is superseded by REFERENCE+parent.
- **Git-commit** references via the existing GitStore (whole-file writes, not
  the memory_writer CAS path).
- **Agent→reference relations:** let the agent relate an entity to a reference
  it cited.
