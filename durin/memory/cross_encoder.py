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
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "CrossEncoderReranker",
    "DEFAULT_MODEL",
    "rerank_hits",
    "test_model",
]


# Default cross-encoder reranker. H30 (2026-05-30): switched from
# `jinaai/jina-reranker-v2-base-multilingual` to `BAAI/bge-reranker-
# base` because:
#   1. jina-v2 needs `trust_remote_code=True` + custom code that
#      depends on private transformers APIs removed in 5.x
#      (`create_position_ids_from_input_ids`). Breaks on a fresh
#      install with current transformers.
#   2. jina is CC-BY-NC-4.0 (non-commercial); BAAI is MIT.
#   3. BAAI/bge-reranker-base is ~100M params (vs 278M for jina-v2),
#      so faster + less RAM, while still strongly multilingual.
DEFAULT_MODEL: str = "BAAI/bge-reranker-base"
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


_RERANK_FALLBACK_LOGGED = False


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

    Audit H25 (2026-05-30): when the reranker returns ``None``
    (model never loaded, scoring failed, etc.) we log ONCE per
    process at WARNING level so the operator knows the rerank step
    was a no-op for the rest of the session. The per-call fallback
    stays silent after the first log to avoid spamming on every
    search.
    """
    if not hits:
        return []
    docs = [d for _, d in hits]
    scores = reranker.score(query, docs)
    if scores is None or len(scores) != len(hits):
        global _RERANK_FALLBACK_LOGGED
        if not _RERANK_FALLBACK_LOGGED:
            _RERANK_FALLBACK_LOGGED = True
            logger.warning(
                "cross_encoder: rerank returned None — every search "
                "this process serves will fall through to the "
                "RRF-only order. See earlier ERROR log for the load "
                "failure root cause. Subsequent fallbacks suppressed."
            )
        return [u for u, _ in hits][:top_n]
    paired = sorted(
        zip(hits, scores), key=lambda x: x[1], reverse=True,
    )
    return [u for (u, _), _ in paired[:top_n]]


def test_model(
    model_id: str,
    *,
    loader: Optional[Any] = None,
) -> dict:
    """Probe a cross-encoder model id: load + score a trivial pair.

    Audit B12 (2026-05-28). Replaces the previously-considered
    hard-coded enum of "supported models" — any id that
    ``sentence_transformers.CrossEncoder`` (or a custom loader) can
    resolve is accepted. The validation happens dynamically when an
    operator clicks "Test" in the webui or runs ``durin doctor``.

    Returns a dict with:
    - ``status``: ``"ok"`` | ``"fail"``.
    - ``message``: human-readable summary.
    - ``model_id``: the input id (echoed for the webui to confirm).
    - ``duration_ms``: wall-clock of load + score.

    ``loader`` is the factory that resolves the id to a :class:`Scorer`.
    Defaults to :func:`_load_default_scorer` (production). Tests inject
    a stub that returns a known scorer or simulates load failure.
    Keeping the loader injectable means we can unit-test the result
    shape without pulling sentence_transformers into the test path.
    """
    import time

    if not model_id or not isinstance(model_id, str):
        return {
            "status": "fail",
            "message": "model id is empty",
            "model_id": model_id or "",
            "duration_ms": 0.0,
        }

    factory = loader or _load_default_scorer
    t0 = time.perf_counter()
    try:
        scorer = factory(model_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "message": f"loader raised: {type(exc).__name__}: {exc}",
            "model_id": model_id,
            "duration_ms": (time.perf_counter() - t0) * 1000.0,
        }
    if scorer is None:
        return {
            "status": "fail",
            "message": (
                "loader returned None — either sentence_transformers "
                "is missing (install durin[cross-encoder]) or the "
                "model id failed to download or load. Check logs for "
                "the underlying error."
            ),
            "model_id": model_id,
            "duration_ms": (time.perf_counter() - t0) * 1000.0,
        }
    # Trivial score to confirm the loaded model is actually callable.
    try:
        scores = scorer.score([("test query", "test document")])
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "message": (
                f"loaded but score() raised: {type(exc).__name__}: {exc}"
            ),
            "model_id": model_id,
            "duration_ms": (time.perf_counter() - t0) * 1000.0,
        }
    if not isinstance(scores, list) or not scores:
        return {
            "status": "fail",
            "message": (
                f"loaded but score() returned an unusable result: "
                f"{type(scores).__name__}({scores!r})"
            ),
            "model_id": model_id,
            "duration_ms": (time.perf_counter() - t0) * 1000.0,
        }
    return {
        "status": "ok",
        "message": (
            f"loaded {model_id} and produced a score "
            f"(value={float(scores[0]):.3f})"
        ),
        "model_id": model_id,
        "duration_ms": (time.perf_counter() - t0) * 1000.0,
    }


def _load_default_scorer(model: str) -> Optional[Scorer]:
    """Try to lazy-load ``sentence_transformers.CrossEncoder``.

    Returns a :class:`Scorer` shim or ``None`` on failure (missing
    dep, model download failure, OOM, etc.).

    Audit H25 (2026-05-30): failures escalate to ERROR-level logs
    (was INFO / WARNING) so an operator who enabled the feature in
    config sees a loud signal that the rerank step is disabled.
    Search continues without rerank — pure RRF fusion still ships
    results — but the operator's intent (enable rerank) silently
    became no-op pre-H25, which masked the dead-code bug discovered
    while investigating bench-100-prop chicken retrieval failures.
    """
    try:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
    except ImportError:
        logger.error(
            "cross_encoder: sentence_transformers is NOT installed — "
            "rerank step DISABLED, falling back to pure RRF fusion. "
            "Search still works but ranking quality is reduced. "
            "Install with `pip install durin-agent[cross-encoder]` "
            "(or `pip install sentence-transformers`) to enable. "
            "Set `cfg.memory.search.cross_encoder.enabled = false` "
            "to silence this message if rerank is not desired."
        )
        return None
    try:
        # H30 (2026-05-30): pass trust_remote_code=True. Several
        # production-grade rerankers (jina-reranker-v2, jina-reranker-v3,
        # qwen-reranker) ship custom architecture code that lives in the
        # HuggingFace repo, not in sentence-transformers — without this
        # flag the load raises with "The repository contains custom code
        # which must be executed to correctly load the model".
        #
        # Security note: trust_remote_code lets the model definition
        # execute arbitrary code from the HF repo at load time. This is
        # the standard pattern in the embedding / rerank ecosystem
        # (jina, qwen, alibaba-gte all require it). Models we recommend
        # in the wizard are from vetted publishers (jinaai, BAAI,
        # mixedbread, intfloat); operators who configure custom models
        # should be aware of this contract.
        model_obj = CrossEncoder(model, trust_remote_code=True)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cross_encoder: failed to load model %r — rerank step "
            "DISABLED, falling back to pure RRF fusion. Underlying "
            "error: %s. Search still works but ranking quality is "
            "reduced. Verify the model id is reachable from this "
            "host (network / HuggingFace cache), or switch to a "
            "different model via `cfg.memory.search.cross_encoder.model`.",
            model, exc,
        )
        return None

    class _STScorer:
        def __init__(self, m) -> None:
            self._m = m

        def score(self, pairs):
            return [float(s) for s in self._m.predict(pairs).tolist()]

    return _STScorer(model_obj)
