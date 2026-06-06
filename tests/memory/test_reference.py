from durin.memory.reference import (
    chunk_by_tokens,
    ingest_reference,
    load_reference,
    reference_chunks,
    reference_marker,
)
from durin.utils.helpers import estimate_text_tokens

_PARA = ("mxHERO is an email-to-cloud company. Its flagship product Mail2Cloud "
         "routes email attachments into cloud storage like Box, Google Drive and "
         "SharePoint. The platform also offers an AI Supervisor approval flow. ")


def test_chunk_by_tokens_respects_budget():
    text = "\n\n".join(_PARA * 4 for _ in range(8))     # well over 512 tokens
    chunks = chunk_by_tokens(text, max_tokens=512)
    assert len(chunks) >= 2
    assert all(estimate_text_tokens(c) <= 512 for c in chunks)


def test_short_doc_single_chunk():
    assert len(chunk_by_tokens("A short note about mxHERO.")) == 1


def test_ingest_preserves_whole_doc(tmp_path):
    content = "# mxHERO\n\n" + (_PARA * 6)
    res = ingest_reference(tmp_path, "mxHERO Profile", content, source="https://mxhero.com")
    assert res.ref == "reference:mxhero-profile"
    whole = load_reference(tmp_path, res.ref)
    # the WHOLE doc is preserved verbatim (not lost to chunking)
    assert "# mxHERO" in whole
    assert _PARA.strip() in whole
    assert "type: reference" in whole
    assert "source: https://mxhero.com" in whole


def test_chunks_have_parent_pointer(tmp_path):
    content = "\n\n".join(_PARA * 4 for _ in range(8))
    res = ingest_reference(tmp_path, "Big Doc", content)
    chunks = reference_chunks(tmp_path, res.ref)
    assert len(chunks) == res.chunk_count >= 2
    assert all(c["parent"] == "reference:big-doc" for c in chunks)
    assert all(c["tokens"] <= 512 for c in chunks)


def test_reference_indexed_and_searchable(tmp_path):
    # G2: reference pages are indexable (not rejected) + found by search.
    from durin.memory.indexer import _payload_for
    from durin.memory.search import search_memory
    ingest_reference(tmp_path, "SMTP Setup",
                     "Set the relay host to smtp.example.com port 587 with STARTTLS.")
    payload = _payload_for(tmp_path, tmp_path / "memory/references/smtp-setup.md")
    assert payload is not None
    assert payload["type_"] == "reference"
    assert payload["uri"] == "reference:smtp-setup"
    hits = [r for r in search_memory(tmp_path, "STARTTLS") if r.class_name == "reference"]
    assert hits and hits[0].uri == "memory/reference/smtp-setup"


def test_reference_marker():
    assert reference_marker("reference:mxhero", title="mxHERO Profile") == \
        "=== REFERENCE: reference:mxhero (mxHERO Profile) ==="


import pytest

from durin.memory.vector_index import vector_index_available

# These two tests exercise the REAL vector index (fastembed + lancedb); the CI
# image skips the `memory` extra, so they self-skip there (grep/FTS tests stay).
_needs_vector = pytest.mark.skipif(
    not vector_index_available(),
    reason="vector deps (memory extra: fastembed/lancedb) absent",
)


@_needs_vector
@pytest.mark.asyncio
async def test_memory_ingest_makes_reference_searchable_grep_fts_vector(tmp_path):
    """A2 end-to-end: memory_ingest stores the doc as a REFERENCE (whole) and
    indexes it so it is findable via ALL THREE retrieval mechanisms — grep
    (warm), FTS (whole doc), and vector/embeddings (the token chunks) — not the
    old chunked `corpus/` model. This is the test that was missing when
    references shipped unwired."""
    from durin.agent.tools.memory_ingest import MemoryIngestTool
    from durin.memory.fts_index import FTSIndex
    from durin.memory.search import search_memory
    from durin.memory.vector_index import VectorIndex
    from durin.memory.embedding import FastembedProvider

    body = ("Set the SMTP relay host to smtp.example.com on port 587 with "
            "STARTTLS. The AI Supervisor approval flow routes attachments "
            "into Box and SharePoint.\n\n")
    doc = tmp_path / "spec.md"
    doc.write_text("# SMTP Relay Setup\n\n" + body * 8, encoding="utf-8")

    tool = MemoryIngestTool(workspace=str(tmp_path),
                            embedding_model="intfloat/multilingual-e5-small")
    await tool.execute(path=str(doc))

    # (1) stored as a REFERENCE (whole doc) + chunk sidecar, NOT a corpus entry
    refs = list((tmp_path / "memory" / "references").glob("*.md"))
    assert refs, "memory_ingest did not create a reference page"
    whole = refs[0].read_text(encoding="utf-8")
    assert "type: reference" in whole and "STARTTLS" in whole
    slug = refs[0].stem
    assert (tmp_path / "memory" / "references" / f"{slug}.chunks.jsonl").exists()
    corpus = tmp_path / "memory" / "corpus"
    assert not (corpus.exists() and list(corpus.glob("*.md"))), "old corpus path still used"

    # (2) GREP (warm) finds the whole reference
    assert any(r.class_name == "reference" for r in search_memory(tmp_path, "STARTTLS")), "grep miss"

    # (3) FTS finds the whole reference doc
    with FTSIndex.open(tmp_path) as idx:
        assert any(slug in str(getattr(h, "uri", h)) for h in idx.search("STARTTLS", limit=20)), "FTS miss"

    # (4) VECTOR (embeddings): the chunks are embedded + a semantic query pulls a
    #     chunk that resolves to the parent reference
    vi = VectorIndex(tmp_path, FastembedProvider("intfloat/multilingual-e5-small"))
    hits = vi.search("how do I configure the outbound mail relay port", top_k=10)
    assert any("reference" in str(h.get("class_name", "")) and slug in str(h.get("id", ""))
               for h in hits), f"vector miss on reference chunk; got {[h.get('id') for h in hits]}"


@_needs_vector
def test_rebuild_from_workspace_indexes_reference_chunks(tmp_path):
    """A full vector rebuild must restore reference chunks (e2e finding 2026-06-06):
    rebuild_from_workspace previously walked entries + entities + skills but NOT
    references, so `durin memory reindex` / the N5 model-change rebuild silently
    dropped reference semantic search."""
    from durin.memory.embedding import FastembedProvider
    from durin.memory.vector_index import VectorIndex
    ingest_reference(tmp_path, "relay-doc",
                     "Set relay.port to configure the outbound mail relay. Default 587.",
                     source="docs/relay.md")
    vi = VectorIndex(tmp_path, FastembedProvider("intfloat/multilingual-e5-small"))
    n = vi.rebuild_from_workspace()
    assert n >= 1
    hits = vi.search("how do I set the outbound mail relay port", top_k=10)
    assert any("reference" in str(h.get("class_name", "")) and "relay-doc" in str(h.get("id", ""))
               for h in hits), f"reference chunk missing from rebuild; got {[h.get('id') for h in hits]}"
