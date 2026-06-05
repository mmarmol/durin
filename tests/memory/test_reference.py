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


def test_reference_marker():
    assert reference_marker("reference:mxhero", title="mxHERO Profile") == \
        "=== REFERENCE: reference:mxhero (mxHERO Profile) ==="
