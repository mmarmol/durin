"""Tests for forget_reference — forget an ingested Library document.

The Library counterpart to forget_entry: an ingested ``reference:<slug>`` is
archived (+ its chunk sidecar) and tombstoned, and its search-index rows are
removed — the whole-doc FTS row (``reference:<slug>``) and every per-chunk
vector row (``reference:<slug>#<idx>``). Shared behind the webui delete button,
the ``DELETE /api/v1/memory/documents/{slug}`` route, and the ``memory_forget``
agent tool.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from durin.memory.deletion import is_deleted
from durin.memory.forget import forget_reference
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import reindex_one_file
from durin.memory.reference import ingest_reference, load_reference

_DOC = "# Intro\n\nStationarity matters.\n\n# ARIMA\n\nauto.arima picks orders.\n"


def _ingest(ws: Path, title: str = "Time Series with R") -> tuple[str, str]:
    """Ingest a reference and FTS-index it (as memory_ingest does). Returns
    ``(ref, slug)``."""
    res = ingest_reference(ws, title, _DOC, source="/x.pdf")
    slug = res.ref.split(":", 1)[1]
    reindex_one_file(ws, ws / "memory" / "references" / f"{slug}.md", trigger="test")
    return res.ref, slug


def _fts_uris(ws: Path) -> set[str]:
    with FTSIndex.open(ws) as idx:
        return {uri for uri, _ in idx.known_uris()}


# ---------------------------------------------------------------------------
# archive + tombstone
# ---------------------------------------------------------------------------


def test_forget_reference_archives_and_tombstones(tmp_path: Path) -> None:
    ref, slug = _ingest(tmp_path)
    ref_md = tmp_path / "memory" / "references" / f"{slug}.md"
    chunks = tmp_path / "memory" / "references" / f"{slug}.chunks.jsonl"
    assert ref_md.exists() and chunks.exists()  # precondition

    dest = forget_reference(tmp_path, ref)

    # The document + its chunk sidecar move out of the live pool into archive/.
    assert dest is not None
    assert dest.parts[-3:] == ("archive", "references", f"{slug}.md")
    assert not ref_md.exists()
    assert not chunks.exists()
    assert (tmp_path / "memory" / "archive" / "references" / f"{slug}.md").exists()
    # Tombstoned (dream won't re-distill) and no longer loadable.
    assert is_deleted(tmp_path, ref)
    assert load_reference(tmp_path, ref) is None


def test_forget_reference_accepts_bare_slug(tmp_path: Path) -> None:
    _ref, slug = _ingest(tmp_path)
    # Callers may pass the bare slug (path param) rather than reference:<slug>.
    dest = forget_reference(tmp_path, slug)
    assert dest is not None
    assert not (tmp_path / "memory" / "references" / f"{slug}.md").exists()


def test_forget_reference_missing_returns_none(tmp_path: Path) -> None:
    assert forget_reference(tmp_path, "reference:does-not-exist") is None


# ---------------------------------------------------------------------------
# index cleanup: FTS row (real) + vector ids (captured)
# ---------------------------------------------------------------------------


def test_forget_reference_removes_fts_row(tmp_path: Path) -> None:
    ref, _slug = _ingest(tmp_path)
    assert ref in _fts_uris(tmp_path)  # whole-doc FTS row keyed reference:<slug>

    forget_reference(tmp_path, ref)

    assert ref not in _fts_uris(tmp_path)


def test_forget_reference_drops_all_chunk_vector_ids(tmp_path: Path, monkeypatch) -> None:
    ref, _slug = _ingest(tmp_path)
    n_chunks = len(
        (tmp_path / "memory" / "references" / f"{_slug}.chunks.jsonl")
        .read_text(encoding="utf-8").splitlines()
    )
    assert n_chunks >= 1

    captured: list[list[str]] = []
    import durin.memory.vector_index as vi

    monkeypatch.setattr(vi, "delete_ids", lambda ws, ids: captured.append(list(ids)))

    forget_reference(tmp_path, ref)

    # Every chunk's vector row is targeted: reference:<slug>#0 .. #N-1.
    assert captured == [[f"{ref}#{i}" for i in range(n_chunks)]]


# ---------------------------------------------------------------------------
# graph_api wrapper (webui / route facing)
# ---------------------------------------------------------------------------


def test_graph_api_forget_reference_results(tmp_path: Path) -> None:
    from durin.memory.graph_api import forget_reference as gforget

    ref, _slug = _ingest(tmp_path)
    assert gforget(tmp_path, ref) == {"result": "archived"}
    # Idempotent: forgetting again is a clean not_found, never a crash.
    assert gforget(tmp_path, ref) == {"result": "not_found"}
    assert gforget(tmp_path, "reference:missing") == {"result": "not_found"}


# ---------------------------------------------------------------------------
# memory_forget agent tool routes reference:<slug>
# ---------------------------------------------------------------------------


def test_memory_forget_tool_routes_reference(tmp_path: Path) -> None:
    from durin.agent.tools.memory_forget import MemoryForgetTool

    ref, slug = _ingest(tmp_path)
    tool = MemoryForgetTool(workspace=tmp_path)

    out = asyncio.run(tool.execute(uri=ref, reason="test"))
    assert out["status"] == "forgotten"
    assert out["archived_to"].endswith(f"archive/references/{slug}.md")
    assert not (tmp_path / "memory" / "references" / f"{slug}.md").exists()

    missing = asyncio.run(tool.execute(uri="reference:missing"))
    assert "error" in missing
