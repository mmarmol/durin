"""Cross-encoder reranker.

Opt-in step 5 of the search pipeline. Default model
`jinaai/jina-reranker-v2-base-multilingual`; tests stub the scorer
so the heavy dep isn't required for CI.
"""

from __future__ import annotations

from durin.memory.cross_encoder import (
    DEFAULT_MODEL,
    CrossEncoderReranker,
)


class _FakeScorer:
    """Stub: returns score = length of doc text mod 100."""

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, str]]] = []

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.batches.append(list(pairs))
        return [float(len(d)) / 100.0 for _, d in pairs]


def test_default_model_constant() -> None:
    # H30 (2026-05-30): switched from jina-reranker-v2 to bge-reranker-
    # base. See `durin/memory/cross_encoder.py` for full rationale
    # (license, transformers 5.x compat, footprint).
    assert DEFAULT_MODEL == "BAAI/bge-reranker-base"


def test_score_returns_floats_per_doc() -> None:
    reranker = CrossEncoderReranker(scorer=_FakeScorer())
    scores = reranker.score("query", ["doc one", "doc two and three"])
    assert isinstance(scores, list)
    assert len(scores) == 2
    assert all(isinstance(s, float) for s in scores)


def test_empty_docs_returns_empty_list() -> None:
    reranker = CrossEncoderReranker(scorer=_FakeScorer())
    assert reranker.score("query", []) == []


def test_batching_default_32() -> None:
    """Inputs > 32 docs are batched. Single call here = 64 docs →
    two batches of 32."""
    fake = _FakeScorer()
    reranker = CrossEncoderReranker(scorer=fake, batch_size=32)
    reranker.score("q", [f"doc {i}" for i in range(64)])
    assert len(fake.batches) == 2
    assert len(fake.batches[0]) == 32
    assert len(fake.batches[1]) == 32


def test_batch_size_configurable() -> None:
    fake = _FakeScorer()
    reranker = CrossEncoderReranker(scorer=fake, batch_size=8)
    reranker.score("q", [f"d{i}" for i in range(20)])
    # 20 / 8 = 3 batches (8 + 8 + 4)
    assert len(fake.batches) == 3
    assert [len(b) for b in fake.batches] == [8, 8, 4]


def test_scorer_failure_returns_none() -> None:
    """Model load failure / inference crash → return None so the pipeline
    can skip the rerank step gracefully."""
    class _Broken:
        def score(self, pairs):
            raise RuntimeError("model OOM")

    reranker = CrossEncoderReranker(scorer=_Broken())
    result = reranker.score("q", ["one", "two"])
    assert result is None


def test_rerank_combines_with_pipeline() -> None:
    """The companion `rerank_hits` helper takes a list of (uri, doc_text)
    tuples + the query and returns the URIs reordered by cross-encoder
    score, dropping anything below position N."""
    from durin.memory.cross_encoder import rerank_hits

    fake = _FakeScorer()
    reranker = CrossEncoderReranker(scorer=fake)
    hits = [
        ("a", "short"),
        ("b", "this is a much longer document"),
        ("c", "medium length"),
    ]
    out = rerank_hits(reranker, query="q", hits=hits, top_n=2)
    # Fake scorer ranks by length desc → b > c > a; top_n=2 keeps b, c.
    assert out == ["b", "c"]


def test_rerank_gracefully_skips_on_failure() -> None:
    """When the reranker returns None (scorer crashed), the helper
    returns the URIs in input order — no error propagated."""
    from durin.memory.cross_encoder import rerank_hits

    class _Broken:
        def score(self, pairs):
            raise RuntimeError("nope")

    reranker = CrossEncoderReranker(scorer=_Broken())
    hits = [("a", "x"), ("b", "y")]
    out = rerank_hits(reranker, query="q", hits=hits, top_n=10)
    assert out == ["a", "b"]


def test_lazy_load_when_no_scorer_provided() -> None:
    """When `scorer=None`, the reranker tries to load the default
    model on first call. In CI without the dep, the load fails →
    subsequent `score()` calls return None."""
    reranker = CrossEncoderReranker(scorer=None)
    result = reranker.score("q", ["doc"])
    # We don't assert hard on None — the test box might have the dep.
    # The contract is: never raise, return list[float] | None.
    assert result is None or isinstance(result, list)


# ---------------------------------------------------------------------------
# P11 Fix C (2026-05-30): time-based reload retry + reset()
# ---------------------------------------------------------------------------


def test_reset_clears_load_state() -> None:
    """`reset()` forces the next score() call to re-attempt the load.

    Used by the HealthChecker probe to recover from transient load
    failures (network blip, HF cache issue) without a process restart.
    """
    from durin.memory.cross_encoder import CrossEncoderReranker

    r = CrossEncoderReranker(scorer=None)
    # First call attempts load; in CI without the dep, fails.
    r.score("q", ["d"])
    assert r._load_attempted is True
    r.reset()
    assert r._load_attempted is False
    assert r._scorer is None
    assert r._last_failed_load_at == 0.0


def test_failed_load_retries_after_retry_window() -> None:
    """After a failed load, score() retries on the next call ONLY
    after the retry window has elapsed. Inside the window, score()
    returns None without re-attempting the load."""
    from unittest.mock import patch

    from durin.memory.cross_encoder import CrossEncoderReranker

    load_attempts = []

    def _fake_load(model: str):
        load_attempts.append(model)
        return None  # simulate persistent load failure

    with patch(
        "durin.memory.cross_encoder._load_default_scorer", _fake_load,
    ):
        r = CrossEncoderReranker(scorer=None, model="fake/model")
        # First call → load attempted
        assert r.score("q", ["d"]) is None
        assert len(load_attempts) == 1
        # Immediately after: should NOT retry (still within the
        # retry window). _should_retry_load returns False.
        assert r.score("q", ["d"]) is None
        assert len(load_attempts) == 1
        # Simulate enough time elapsed to retry
        from durin.memory import cross_encoder as ce_mod
        r._last_failed_load_at -= ce_mod._RELOAD_RETRY_SECONDS + 1
        # Now next call should retry
        assert r.score("q", ["d"]) is None
        assert len(load_attempts) == 2


def test_successful_load_does_not_retry() -> None:
    """Once load succeeds, _should_retry_load returns False
    indefinitely — we keep the loaded scorer."""

    class _Fake:
        def score(self, pairs): return [0.5] * len(pairs)

    from unittest.mock import patch

    from durin.memory.cross_encoder import CrossEncoderReranker

    load_attempts = []

    def _fake_load(model: str):
        load_attempts.append(model)
        return _Fake()

    with patch(
        "durin.memory.cross_encoder._load_default_scorer", _fake_load,
    ):
        r = CrossEncoderReranker(scorer=None, model="fake/model")
        # Three calls: load fires only once.
        r.score("q", ["d"])
        r.score("q", ["d"])
        r.score("q", ["d"])
        assert len(load_attempts) == 1
