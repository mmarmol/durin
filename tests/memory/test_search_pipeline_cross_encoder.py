"""Cross-encoder rerank step in `search_pipeline.run_search_pipeline`
(P4.2 / doc 03 §9).

The reranker is opt-in: callers pass a `cross_encoder` instance.
When None, the pipeline behaves exactly as before — no rerank step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.cross_encoder import CrossEncoderReranker
from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
from durin.memory.schema import MemoryEntry
from durin.memory.search_pipeline import run_search_pipeline
from durin.memory.storage import save_entry


class _PrefersFirstUri:
    """Stub: prefers documents whose first chunk after the space
    starts with the letter 'A'. Lets the test choose ordering."""

    def score(self, pairs):
        scores: list[float] = []
        for q, doc in pairs:
            scores.append(1.0 if doc.startswith("A") else 0.1)
        return scores


def _seed(workspace: Path) -> None:
    epi_dir = workspace / "memory" / "episodic"
    epi_dir.mkdir(parents=True, exist_ok=True)
    for i, txt in enumerate(["A apple match", "Z zebra match", "M mango match"]):
        save_entry(
            MemoryEntry(id=f"e{i}", headline=txt, body=txt),
            epi_dir / f"e{i}.md",
        )
    rebuild_fts_index(workspace)


def test_no_cross_encoder_runs_pipeline_unchanged(tmp_path: Path) -> None:
    _seed(tmp_path)
    out = run_search_pipeline(tmp_path, "match")
    assert out.hits  # something surfaced; order is RRF-only


def test_cross_encoder_reorders_results(tmp_path: Path) -> None:
    """The fake reranker prefers docs starting with 'A'. The 'A
    apple match' entry should land at index 0 after rerank."""
    _seed(tmp_path)
    reranker = CrossEncoderReranker(scorer=_PrefersFirstUri())
    out = run_search_pipeline(
        tmp_path, "match",
        cross_encoder=reranker,
        cross_encoder_top_n=10,
    )
    assert out.hits
    # The hit whose snippet/headline begins with 'A' should be at top.
    top = out.hits[0]
    assert top.snippet.startswith("A") or top.uri.endswith("e0")


def test_cross_encoder_failure_falls_back_to_rrf_order(
    tmp_path: Path,
) -> None:
    """When the scorer crashes, `rerank_hits` returns input order;
    pipeline output equals what we'd get without a reranker."""
    _seed(tmp_path)

    class _Broken:
        def score(self, pairs):
            raise RuntimeError("model OOM")

    broken_reranker = CrossEncoderReranker(scorer=_Broken())

    out_no_rerank = run_search_pipeline(tmp_path, "match")
    out_broken = run_search_pipeline(
        tmp_path, "match",
        cross_encoder=broken_reranker,
    )
    # Same result count + same top URI (order preserved on failure).
    assert len(out_no_rerank.hits) == len(out_broken.hits)
    assert out_no_rerank.hits[0].uri == out_broken.hits[0].uri


def test_cross_encoder_top_n_caps(tmp_path: Path) -> None:
    """With top_n=1, only one hit makes it past the rerank step
    into the position-0 slot; lower-ranked hits still surface via
    the cap-aware tail logic but the rerank's choice wins position 0."""
    _seed(tmp_path)
    reranker = CrossEncoderReranker(scorer=_PrefersFirstUri())
    out = run_search_pipeline(
        tmp_path, "match",
        cross_encoder=reranker,
        cross_encoder_top_n=1,
    )
    # At least one hit; position 0 reflects rerank's preference.
    assert out.hits
    assert (
        out.hits[0].snippet.startswith("A")
        or out.hits[0].uri.endswith("e0")
    )
