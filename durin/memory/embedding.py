"""Embedding provider abstraction for the memory subsystem.

Phase 2.1 ships :class:`FastembedProvider`, a load-once-keep-loaded
adapter over fastembed (ONNX runtime, in-process, no Ollama required).

**Default model**: ``intfloat/multilingual-e5-small`` (~450 MB fp32 /
~115 MB int8, 384-dim, 100+ languages, MIT). Registered as a custom
fastembed model (see :func:`_register_custom_models`) because the
fastembed default catalog doesn't include it. For heavy workloads
needing top-tier multilingual recall, ``intfloat/multilingual-e5-large``
(2.24 GB, 1024-dim) is the override; it ships in fastembed's catalog
natively. See ``docs/architecture/memory/02_indexing.md`` and the wizard in
``durin/cli/onboard_wizard.py``.

**E5 prefix convention**: E5-family models were trained with asymmetric
prompts — documents must be prefixed with ``passage: `` and queries
with ``query: ``. Skipping the prefix degrades recall measurably (the
fastembed library does NOT auto-apply it). :meth:`embed_passages` and
:meth:`embed_query` apply the prefix when the model is E5-family;
:meth:`embed` is left as the prefix-less escape hatch for callers that
need raw embedding (tests, ad-hoc tools).

Model identifiers are validated against fastembed's live catalog at
construction time. ``ValueError`` with an actionable message lists the
supported models if an unknown identifier is supplied — surfacing
catalog drift (fastembed retires or renames models between versions)
at the config boundary instead of at first ``embed()`` call.

Telemetry: every load and every embed call emits a structured event
(``memory.embedding.load`` / ``memory.embedding.embed`` with
``duration_ms``) so we can decide on idle-eviction empirically rather
than guessing.
"""

from __future__ import annotations

import threading
import time
import warnings
from abc import ABC, abstractmethod
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.extras import ensure_or_note

__all__ = [
    "EmbeddingProvider",
    "FastembedProvider",
    "list_supported_models",
    "model_dimensions",
]


# Custom models registered with fastembed via `add_custom_model` because
# they are not part of fastembed's default catalog but the durin wizard
# offers them. Each entry maps a model id to the kwargs passed to
# `TextEmbedding.add_custom_model`.
#
# Why register vs. PR upstream: PR'ing each model to fastembed's catalog
# is the "right" path long-term (helps other users too) but the in-
# process registration here is cheap (~10 lines), avoids waiting for
# Qdrant to merge, and keeps durin's model choice independent of
# upstream cadence. We can still PR upstream later — the registration
# becomes a no-op once the model lands in the default catalog (it would
# already be present and `_register_custom_models` skips it).
_CUSTOM_MODELS: dict[str, dict[str, Any]] = {
    "intfloat/multilingual-e5-small": {
        "pooling": "MEAN",
        "normalization": True,
        "hf_source": "intfloat/multilingual-e5-small",
        "dim": 384,
        "model_file": "onnx/model.onnx",
        "description": (
            "Multilingual E5 small — 117M params, 384-dim, 100+ "
            "languages, MIT. Fine-tuned from microsoft/Multilingual-"
            "MiniLM-L12-H384 with contrastive retrieval objective. "
            "Requires `query: ` / `passage: ` prefix (applied by "
            "FastembedProvider.embed_query / embed_passages)."
        ),
        "license": "mit",
        "size_in_gb": 0.45,
    },
}

# Process-lifetime guard for `_register_custom_models`: fastembed's
# `add_custom_model` raises ValueError on re-registration, so we only
# call it once per model per process.
_REGISTERED_CUSTOM: set[str] = set()

# Serialises the registration / catalog-cache mutations below. The embedding
# provider is shared between the agent loop and the Dream daemon thread (B4),
# so two threads can hit the lazy first-use path at once; without this lock
# both pass the `model_id in _REGISTERED_CUSTOM` check and the second
# `add_custom_model` raises ValueError (B5). Reentrant because
# `list_supported_models` holds it across its call to `_register_custom_models`.
_REGISTRATION_LOCK = threading.RLock()


def _register_custom_models() -> None:
    """Idempotently register :data:`_CUSTOM_MODELS` with fastembed.

    Safe to call multiple times: the in-process ``_REGISTERED_CUSTOM``
    set short-circuits repeat work, and a check against
    ``TextEmbedding.list_supported_models()`` skips models that have
    landed in fastembed's catalog upstream since this code was written.
    """
    if not _CUSTOM_MODELS:
        return
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
        from fastembed.common.model_description import (  # type: ignore[import-not-found]
            ModelSource,
            PoolingType,
        )
    except ImportError:
        # Defer: callers of `list_supported_models()` will raise the
        # actionable install hint themselves.
        return

    catalog_names = {m["model"] for m in TextEmbedding.list_supported_models()}
    with _REGISTRATION_LOCK:
        for model_id, kwargs in _CUSTOM_MODELS.items():
            if model_id in _REGISTERED_CUSTOM or model_id in catalog_names:
                continue
            TextEmbedding.add_custom_model(
                model=model_id,
                pooling=getattr(PoolingType, kwargs["pooling"]),
                normalization=kwargs["normalization"],
                sources=ModelSource(hf=kwargs["hf_source"]),
                dim=kwargs["dim"],
                model_file=kwargs.get("model_file", "onnx/model.onnx"),
                description=kwargs.get("description", ""),
                license=kwargs.get("license", ""),
                size_in_gb=kwargs.get("size_in_gb", 0.0),
            )
            _REGISTERED_CUSTOM.add(model_id)


# Cached catalog from fastembed.TextEmbedding.list_supported_models().
# Populated lazily on first lookup and never invalidated for the process
# lifetime — fastembed's catalog is a build-time constant.
_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None


def list_supported_models() -> dict[str, dict[str, Any]]:
    """Return fastembed's supported models keyed by model name.

    Registers durin's custom models (:data:`_CUSTOM_MODELS`) with
    fastembed before reading the catalog, so the returned dict
    includes them transparently — wizard, validators, and tests see
    the same view.

    Raises ``RuntimeError`` if fastembed is not installed (the
    ``[memory]`` extra is missing). The cache is process-lifetime; the
    catalog never changes inside one fastembed version.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
    except ImportError:
        res = ensure_or_note("memory_vector", config=None)
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is required for vector retrieval. "
                + (res.message or "Install the memory extra: pip install durin-agent[memory]")
            ) from exc
    # Double-checked under the lock: a concurrent caller may have populated
    # the cache (and registered) while we were importing (B5).
    with _REGISTRATION_LOCK:
        if _CATALOG_CACHE is not None:
            return _CATALOG_CACHE
        _register_custom_models()
        _CATALOG_CACHE = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        return _CATALOG_CACHE


# E5-family model prefixes (per `intfloat/multilingual-e5-*` model
# cards). Documents must be prefixed with `passage: ` and queries with
# `query: ` to match the training distribution. Without the prefix,
# recall degrades by ~2-5pp on MTEB retrieval tasks (intfloat reports
# this in the paper appendix).
_E5_PASSAGE_PREFIX = "passage: "
_E5_QUERY_PREFIX = "query: "


def _is_e5_family(model_name: str) -> bool:
    """Heuristic: does this model need the E5 query/passage prefix?

    Currently matches both ``intfloat/e5-*`` and ``intfloat/
    multilingual-e5-*`` model ids. The check is by-string rather than
    by-catalog-flag because fastembed's catalog doesn't expose model
    family metadata; if a future non-E5 model happens to contain the
    substring, we add a more specific match here.
    """
    lower = model_name.lower()
    return "e5-" in lower or lower.endswith("/e5") or "/e5-" in lower


def model_dimensions(model_name: str) -> int:
    """Return the embedding output dim for ``model_name`` per fastembed.

    Raises ``ValueError`` if the model is not in the catalog.
    """
    catalog = list_supported_models()
    entry = catalog.get(model_name)
    if entry is None:
        raise ValueError(_unknown_model_error(model_name, catalog))
    dim = entry.get("dim")
    if not isinstance(dim, int) or dim <= 0:
        raise ValueError(
            f"fastembed catalog entry for {model_name!r} lacks a usable 'dim' "
            f"field (got {dim!r})."
        )
    return dim


def _unknown_model_error(model_name: str, catalog: dict[str, dict[str, Any]]) -> str:
    """Compose the actionable error message for an unknown model id."""
    available = sorted(catalog.keys())
    sample = "\n".join(f"  - {n}" for n in available[:10])
    more = "" if len(available) <= 10 else f"\n  ... ({len(available) - 10} more)"
    return (
        f"Embedding model {model_name!r} is not in fastembed's catalog. "
        f"Either fastembed was upgraded and retired this model, or the "
        f"identifier has a typo. Available models in this fastembed "
        f"version:\n{sample}{more}\n"
        f"Update memory.embedding.model in your config to one of the above."
    )


class EmbeddingProvider(ABC):
    """Minimal embedding provider contract.

    ``embed_passages`` and ``embed_query`` exist as semantic surfaces
    for callers that need to express "this is for storage" vs "this is
    for search". Concrete providers may apply model-family-specific
    wrapping (E5 prefix, instruction prefixes, etc.); the default
    implementations fall through to :meth:`embed` so callers can rely
    on the surface even when the provider doesn't differentiate.
    """

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for storage. Default: same as :meth:`embed`."""
        return self.embed(texts)

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query for retrieval. Default: same as
        :meth:`embed` on a one-item list."""
        if not query:
            return []
        return self.embed([query])[0]


class FastembedProvider(EmbeddingProvider):
    """In-process ONNX embedding via fastembed.

    The model identifier is validated against fastembed's live catalog
    at construction time so config errors surface immediately, not on
    the first ``embed()`` call. The model itself loads lazily on first
    :meth:`embed` and stays resident for the life of the process. No
    idle eviction in V1 — telemetry (``memory.embedding.load``,
    ``memory.embedding.embed``) gives data to revisit the decision.
    """

    DEFAULT_MODEL = "intfloat/multilingual-e5-small"

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or self.DEFAULT_MODEL
        # Validate at construction time — surface unknown models at the
        # config boundary instead of at the first embed() call.
        catalog = list_supported_models()
        if self._model_name not in catalog:
            raise ValueError(_unknown_model_error(self._model_name, catalog))
        self._model: Any = None
        # The loop and the Dream daemon thread share one provider (B4); guard
        # the lazy first load so the heavy ONNX model is constructed once.
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return model_dimensions(self._model_name)

    @classmethod
    def warmup(cls, model: str | None = None) -> float:
        """Download (if missing) and load the model; return load duration ms.

        Intended for the onboard wizard and for `AgentLoop` boot when
        the user has just enabled memory: paying the ~18 s first-time
        download here, while the user is actively waiting, is better
        than paying it on the first tool call mid-conversation.

        The loaded model is discarded — this is a side-effect call to
        populate the on-disk model cache. The next FastembedProvider
        instance will reload from disk in ~230 ms.
        """
        provider = cls(model=model)
        t0 = time.monotonic()
        provider._load()
        return (time.monotonic() - t0) * 1000.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Raw embed — no prefix wrapping. Prefer :meth:`embed_passages`
        and :meth:`embed_query` for production memory writes/reads;
        ``embed`` is the escape hatch for tests + ad-hoc tools that
        need to call the model identically regardless of family.
        """
        if not texts:
            return []
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._load()
        assert self._model is not None
        t0 = time.monotonic()
        # fastembed.embed() returns an iterator of numpy arrays.
        out = [list(map(float, vec)) for vec in self._model.embed(texts)]
        emit_tool_event(
            "memory.embedding.embed",
            {
                "model": self._model_name,
                "batch_size": len(texts),
                "duration_ms": (time.monotonic() - t0) * 1000.0,
            },
        )
        return out

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for storage. Applies ``passage: `` prefix
        when the configured model is E5-family.

        E5 models were trained with asymmetric prompts — `passage: `
        for documents, `query: ` for queries. Skipping the prefix
        measurably degrades recall (paper appendix reports ~2-5pp on
        MTEB retrieval). For non-E5 models the input is passed through
        unchanged so this method is a safe default for storage writes
        regardless of the current model.
        """
        if not texts:
            return []
        if _is_e5_family(self._model_name):
            texts = [f"{_E5_PASSAGE_PREFIX}{t}" for t in texts]
        return self.embed(texts)

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query for vector search. Applies ``query: ``
        prefix when the configured model is E5-family. See
        :meth:`embed_passages` for the asymmetric-prompt rationale.
        """
        if _is_e5_family(self._model_name):
            query = f"{_E5_QUERY_PREFIX}{query}"
        return self.embed([query])[0]

    def _load(self) -> None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is required for vector retrieval. "
                "Install the memory extra: pip install durin-agent[memory]"
            ) from exc
        # Ensure custom models (e.g., e5-small) are registered before
        # fastembed tries to resolve the model id. Idempotent.
        _register_custom_models()
        t0 = time.monotonic()
        # fastembed >=0.6 switched catalog E5 models from CLS to mean
        # pooling and warns about the behaviour change on every load.
        # Mean pooling IS the correct E5 behaviour (the E5 paper uses
        # average pooling; our own e5-small registration above declares
        # MEAN explicitly), and any model switch triggers a full vector
        # rebuild (index_meta.embedding_model_id), so index and queries
        # are always pooled consistently. Suppress just that warning —
        # it reads as a problem to operators (onboard/doctor) when it
        # describes the desired behaviour.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*uses mean pooling instead of CLS embedding.*",
                category=UserWarning,
            )
            self._model = TextEmbedding(model_name=self._model_name)
        emit_tool_event(
            "memory.embedding.load",
            {
                "model": self._model_name,
                "duration_ms": (time.monotonic() - t0) * 1000.0,
            },
        )
