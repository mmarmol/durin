"""Cross-encoder reranker (P4.1 / doc 03 §9).

A cross-encoder model scores (query, doc) pairs together in a single
forward pass, producing relevance scores that are typically tighter
than the bi-encoder cosine the LanceDB layer uses. The trade-off:
~300-1500ms per query depending on model size, vs <10ms for the
bi-encoder + RRF path.

This module is **infrastructure-only**. The reranker is OFF by
default — :mod:`durin.memory.search_pipeline` decides whether to
invoke it based on workspace config. Doc 03 §9 + doc 10 P4 are the
spec.

Two surfaces:

- :class:`CrossEncoderReranker` — wraps a scorer with batching +
  graceful degradation. Callers can inject a custom scorer (tests
  do this) or let the class lazy-load
  :data:`DEFAULT_MODEL` via :mod:`sentence_transformers`.

- :func:`rerank_hits` — convenience helper that takes
  ``[(uri, doc_text)]`` plus a query and returns URIs sorted by
  the reranker's score, capped at ``top_n``.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "CrossEncoderReranker",
    "DEFAULT_MODEL",
    "rerank_hits",
]


DEFAULT_MODEL: str = "jinaai/jina-reranker-v2-base-multilingual"
_DEFAULT_BATCH_SIZE: int = 32


class Scorer(Protocol):
    """Anything that scores (query, doc) pairs. Implementations:

    - Real: sentence_transformers `CrossEncoder` wrapped via the
      lazy loader below.
    - Test: a fake that records the calls + returns canned scores.
    """

    def score(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class CrossEncoderReranker:
    """Cross-encoder reranker with batching + graceful degradation."""

    def __init__(
        self,
        *,
        scorer: Optional[Scorer] = None,
        model: str = DEFAULT_MODEL,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._scorer = scorer
        self._model = model
        self._batch_size = batch_size
        # Lazy-load tracking: don't try to import the heavy lib until
        # the first ``score`` call. Set to True after the attempt
        # regardless of outcome so we don't retry on every call.
        self._load_attempted = scorer is not None

    def score(
        self, query: str, docs: list[str],
    ) -> Optional[list[float]]:
        """Return a relevance score per document, OR ``None`` on
        model/load failure. Order matches ``docs``.
        """
        if not docs:
            return []
        if not self._load_attempted:
            self._scorer = _load_default_scorer(self._model)
            self._load_attempted = True
        if self._scorer is None:
            return None
        try:
            scores: list[float] = []
            for batch in self._batched(docs):
                pairs = [(query, doc) for doc in batch]
                batch_scores = self._scorer.score(pairs)
                scores.extend(batch_scores)
            return scores
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cross_encoder: scoring failed (%s); skipping rerank",
                exc,
            )
            return None

    def _batched(self, docs: list[str]):
        for i in range(0, len(docs), self._batch_size):
            yield docs[i:i + self._batch_size]


def rerank_hits(
    reranker: CrossEncoderReranker,
    *,
    query: str,
    hits: list[tuple[str, str]],
    top_n: int,
) -> list[str]:
    """Re-order ``[(uri, doc_text), ...]`` by cross-encoder score.

    Returns the top-``top_n`` URIs. On reranker failure (model
    unavailable / crash), returns the input URIs in their original
    order — never raises, so the search pipeline keeps shipping
    results.
    """
    if not hits:
        return []
    docs = [d for _, d in hits]
    scores = reranker.score(query, docs)
    if scores is None or len(scores) != len(hits):
        return [u for u, _ in hits][:top_n]
    paired = sorted(
        zip(hits, scores), key=lambda x: x[1], reverse=True,
    )
    return [u for (u, _), _ in paired[:top_n]]


def _load_default_scorer(model: str) -> Optional[Scorer]:
    """Try to lazy-load ``sentence_transformers.CrossEncoder``.

    Returns a :class:`Scorer` shim or ``None`` on failure (missing
    dep, model download failure, OOM, etc.).
    """
    try:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
    except ImportError:
        logger.info(
            "cross_encoder: sentence_transformers not installed; "
            "rerank step disabled. Install durin[cross-encoder] "
            "to enable."
        )
        return None
    try:
        model_obj = CrossEncoder(model)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cross_encoder: failed to load model %s: %s", model, exc,
        )
        return None

    class _STScorer:
        def __init__(self, m) -> None:
            self._m = m

        def score(self, pairs):
            return [float(s) for s in self._m.predict(pairs).tolist()]

    return _STScorer(model_obj)
