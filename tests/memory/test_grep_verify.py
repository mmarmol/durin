"""Grep-verify boost — literal confirmation of vector-sourced hits.

RRF only credits lexical evidence when a doc lands in the lexical
top-50. A doc that vector ranks high and that literally contains the
query terms — but sits just past the lexical cutoff — got no lexical
contribution at all, so semantically-near distractors (vector's known
failure mode) could outrank a literally-confirmed hit.

The boost re-verifies vector-sourced hits that the lexical list missed:
a per-uri FTS MATCH (same tokenizers as the lexical tier, so it stays
language-neutral — unicode61 / trigram / LIKE routed like the query
itself). A confirmed hit gains a lexical-grade contribution at its
VECTOR rank: ``W_LEXICAL / (k + rank_in_vector)`` — literal presence is
exactly the lexical evidence the cutoff dropped, and the vector rank is
the confidence we have for it.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.query_router import decide_lexical_route
from durin.memory.rrf_fusion import (
    DEFAULT_K,
    DEFAULT_W_LEXICAL,
    FusedHit,
)


def _index_doc(workspace: Path, uri: str, text: str) -> None:
    with FTSIndex.open(workspace) as idx:
        idx.upsert(
            uri=uri, path=f"{uri}.md", type_="episodic",
            entity_type=None, text=text, mtime=1.0,
        )


def _vector_hit(uri: str, rank: int, score: float = 0.016) -> FusedHit:
    return FusedHit(
        uri=uri, score=score, sources=("vector",),
        ranks={"vector": rank},
    )


# ---------------------------------------------------------------------------
# function-level contract
# ---------------------------------------------------------------------------


def test_verified_hit_gains_lexical_grade_boost(tmp_path: Path) -> None:
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "the needle is here")
    decision = decide_lexical_route("needle")
    hits = [_vector_hit("memory/episodic/a", rank=2)]
    out = _grep_verify_boost(tmp_path, decision, hits)
    expected = 0.016 + DEFAULT_W_LEXICAL / (DEFAULT_K + 2)
    assert abs(out[0].score - expected) < 1e-9


def test_unverified_hit_is_unchanged(tmp_path: Path) -> None:
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "nothing relevant here")
    decision = decide_lexical_route("needle")
    hits = [_vector_hit("memory/episodic/a", rank=1)]
    out = _grep_verify_boost(tmp_path, decision, hits)
    assert out[0].score == hits[0].score


def test_hit_already_in_lexical_is_skipped(tmp_path: Path) -> None:
    """Lexical already credited it — verifying again would double-count."""
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "the needle is here")
    decision = decide_lexical_route("needle")
    hits = [FusedHit(
        uri="memory/episodic/a", score=0.03,
        sources=("lexical", "vector"),
        ranks={"vector": 1, "lexical": 7},
    )]
    out = _grep_verify_boost(tmp_path, decision, hits)
    assert out[0].score == 0.03


def test_non_vector_hit_is_skipped(tmp_path: Path) -> None:
    """Only vector-sourced hits are candidates — grep/lexical rows
    already carry their literal evidence."""
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "the needle is here")
    decision = decide_lexical_route("needle")
    hits = [FusedHit(
        uri="memory/episodic/a", score=0.005, sources=("grep",),
        ranks={"grep": 1},
    )]
    out = _grep_verify_boost(tmp_path, decision, hits)
    assert out[0].score == 0.005


def test_boost_resorts_hits(tmp_path: Path) -> None:
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "the needle is here")
    decision = decide_lexical_route("needle")
    hits = [
        _vector_hit("memory/episodic/b", rank=1, score=0.017),
        _vector_hit("memory/episodic/a", rank=2, score=0.016),
    ]
    out = _grep_verify_boost(tmp_path, decision, hits)
    assert [h.uri for h in out] == [
        "memory/episodic/a", "memory/episodic/b",
    ]


def test_missing_fts_row_is_unchanged(tmp_path: Path) -> None:
    """No row for the uri (stale index): unverifiable, no crash."""
    from durin.memory.search_pipeline import _grep_verify_boost

    decision = decide_lexical_route("needle")
    hits = [_vector_hit("memory/episodic/ghost", rank=1)]
    out = _grep_verify_boost(tmp_path, decision, hits)
    assert out[0].score == hits[0].score


def test_cjk_trigram_route_verifies(tmp_path: Path) -> None:
    """The verification follows the query's lexical route so CJK
    queries verify through the trigram table, not unicode61."""
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "詳細は記憶装置を参照")
    decision = decide_lexical_route("記憶装置")
    hits = [_vector_hit("memory/episodic/a", rank=1)]
    out = _grep_verify_boost(tmp_path, decision, hits)
    expected = 0.016 + DEFAULT_W_LEXICAL / (DEFAULT_K + 1)
    assert abs(out[0].score - expected) < 1e-9


def test_keywords_override_verification_target(tmp_path: Path) -> None:
    """When the agent supplied `keywords`, THAT literal string is what
    must appear — it's the stronger statement of literal intent."""
    from durin.memory.search_pipeline import _grep_verify_boost

    _index_doc(tmp_path, "memory/episodic/a", "ticket ABC-99 closed")
    decision = decide_lexical_route("deployment ticket")
    hits = [_vector_hit("memory/episodic/a", rank=1)]
    out = _grep_verify_boost(tmp_path, decision, hits, keywords="ABC-99")
    assert out[0].score > hits[0].score


# ---------------------------------------------------------------------------
# pipeline integration — the boost survives the full pipeline
# ---------------------------------------------------------------------------


class _FakeVectorIndex:
    def __init__(self, uris: list[str]) -> None:
        self._uris = uris

    def search(self, query: str, top_k: int = 50) -> list[dict]:
        return [
            {"uri": u, "type": "episodic", "path": f"{u}.md"}
            for u in self._uris[:top_k]
        ]


def test_pipeline_boosts_vector_hit_past_lexical_cutoff(
    tmp_path: Path,
) -> None:
    """52 strong competitors fill the lexical top-50; the target doc
    (term appears once, longer doc → weakest BM25) misses the cutoff.
    Vector ranks it #1. Its final score must exceed the maximum it
    could reach WITHOUT the verify boost (vector rank1 + best-case
    grep rank1), proving the boost was applied in the real pipeline."""
    from durin.memory.indexer import rebuild_fts_index
    from durin.memory.schema import MemoryEntry
    from durin.memory.search_pipeline import run_search_pipeline
    from durin.memory.storage import save_entry

    epi_dir = tmp_path / "memory" / "episodic"
    epi_dir.mkdir(parents=True, exist_ok=True)
    for i in range(52):
        save_entry(
            MemoryEntry(id=f"c{i:02d}",
                        headline=f"quopazine quopazine note {i}",
                        body="strong match"),
            epi_dir / f"c{i:02d}.md",
        )
    save_entry(
        MemoryEntry(
            id="target",
            headline="background pharmacology discussion",
            body=("a long body that mentions quopazine exactly once "
                  "between many other tokens " + "filler " * 40),
        ),
        epi_dir / "target.md",
    )
    rebuild_fts_index(tmp_path)
    vi = _FakeVectorIndex(["memory/episodic/target"])
    result = run_search_pipeline(
        tmp_path, "quopazine", vector_index=vi, limit=20,
    )
    target = next(
        (h for h in result.hits if h.uri == "memory/episodic/target"),
        None,
    )
    assert target is not None, "target hit vanished from results"
    max_without_verify = (1.0 / 61) + (0.3 / 61)
    assert target.score > max_without_verify + 1e-4, (
        f"score {target.score:.6f} fits within vector+grep alone — "
        "verify boost not applied"
    )
