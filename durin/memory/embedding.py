"""Embedding provider abstraction for the memory subsystem.

Phase 2.1 ships :class:`FastembedProvider`, a load-once-keep-loaded
adapter over fastembed (ONNX runtime, in-process, no Ollama required).
Default model is ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
(220 MB, 384-dim, multilingual). For CJK-heavy users,
``intfloat/multilingual-e5-large`` (2.24 GB, 1024-dim) is the
recommended override; for English-only workloads,
``sentence-transformers/all-MiniLM-L6-v2`` (90 MB, 384-dim) is the
lightest viable option. See ``docs/08_memory_phase2_proposal.md`` §0d.2.

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

import time
from abc import ABC, abstractmethod
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event

__all__ = [
    "EmbeddingProvider",
    "FastembedProvider",
    "list_supported_models",
    "model_dimensions",
]


# Cached catalog from fastembed.TextEmbedding.list_supported_models().
# Populated lazily on first lookup and never invalidated for the process
# lifetime — fastembed's catalog is a build-time constant.
_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None


def list_supported_models() -> dict[str, dict[str, Any]]:
    """Return fastembed's supported models keyed by model name.

    Raises ``RuntimeError`` if fastembed is not installed (the
    ``[memory]`` extra is missing). The cache is process-lifetime; the
    catalog never changes inside one fastembed version.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "fastembed is required for vector retrieval. "
            "Install the memory extra: pip install durin-agent[memory]"
        ) from exc
    _CATALOG_CACHE = {m["model"]: m for m in TextEmbedding.list_supported_models()}
    return _CATALOG_CACHE


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
    """Minimal embedding provider contract."""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastembedProvider(EmbeddingProvider):
    """In-process ONNX embedding via fastembed.

    The model identifier is validated against fastembed's live catalog
    at construction time so config errors surface immediately, not on
    the first ``embed()`` call. The model itself loads lazily on first
    :meth:`embed` and stays resident for the life of the process. No
    idle eviction in V1 — telemetry (``memory.embedding.load``,
    ``memory.embedding.embed``) gives data to revisit the decision.
    """

    DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or self.DEFAULT_MODEL
        # Validate at construction time — surface unknown models at the
        # config boundary instead of at the first embed() call.
        catalog = list_supported_models()
        if self._model_name not in catalog:
            raise ValueError(_unknown_model_error(self._model_name, catalog))
        self._model: Any = None

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
        if not texts:
            return []
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

    def _load(self) -> None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is required for vector retrieval. "
                "Install the memory extra: pip install durin-agent[memory]"
            ) from exc
        t0 = time.monotonic()
        self._model = TextEmbedding(model_name=self._model_name)
        emit_tool_event(
            "memory.embedding.load",
            {
                "model": self._model_name,
                "duration_ms": (time.monotonic() - t0) * 1000.0,
            },
        )
