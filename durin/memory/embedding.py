"""Embedding provider abstraction for the memory subsystem.

Phase 2.1 ships :class:`FastembedProvider`, a load-once-keep-loaded
adapter over fastembed (ONNX runtime, in-process, no Ollama required).
Default model is ``BAAI/bge-m3`` — see ``docs/08_memory_phase2_proposal.md``
§0d.2 for the model decision and the lighter ``multilingual-e5-small``
alternative.

The interface is intentionally minimal so future providers (HTTP-based
OpenAI / Ollama / Voyage) can plug in without churn. The vector index
and ``memory_search`` paths in later sub-tasks of Phase 2 depend only
on :class:`EmbeddingProvider`.

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

__all__ = ["EmbeddingProvider", "FastembedProvider"]


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


# Known fastembed model output dimensions. Used so callers can size the
# LanceDB vector column without instantiating the model. Update when
# adding a model the project intends to support out of the box.
_FASTEMBED_DIMS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
    "intfloat/multilingual-e5-small": 384,
    "intfloat/multilingual-e5-base": 768,
    "intfloat/multilingual-e5-large": 1024,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}


class FastembedProvider(EmbeddingProvider):
    """In-process ONNX embedding via fastembed.

    Lazy load: the model is constructed on the first :meth:`embed` call
    and kept resident for the life of the process. No idle eviction in
    V1 — telemetry (``memory.embedding.load``,
    ``memory.embedding.embed``) gives data to revisit the decision.
    """

    DEFAULT_MODEL = "BAAI/bge-m3"

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or self.DEFAULT_MODEL
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        d = _FASTEMBED_DIMS.get(self._model_name)
        if d is None:
            raise ValueError(
                f"unknown fastembed model {self._model_name!r}; "
                f"register its output dimensions in _FASTEMBED_DIMS or "
                f"pick one of {sorted(_FASTEMBED_DIMS)}"
            )
        return d

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
                "Install the memory extra: pip install durin[memory]"
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
