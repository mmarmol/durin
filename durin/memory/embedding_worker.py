"""Embedding worker — the child-process side of isolation="process".

Runs inside a single-worker ``ProcessPoolExecutor``. The parent never
loads the ONNX model in this mode; the child loads it once in
``init_worker`` and is recycled by the pool (``max_tasks_per_child``)
so the ONNX CPU arena — which grows to the peak batch and never returns
memory to the OS — is periodically reclaimed with the process.

Module-level functions (not methods) so they are picklable by the
``spawn`` start method on every supported platform.
"""
from __future__ import annotations

import os

_MODEL = None


def init_worker(model_name: str) -> None:
    """Pool initializer: register durin's custom models, load once."""
    global _MODEL
    from fastembed import TextEmbedding  # type: ignore[import-not-found]

    from durin.memory.embedding import _register_custom_models

    _register_custom_models()
    _MODEL = TextEmbedding(model_name=model_name)


def embed_batch(texts: list[str], batch_size: int) -> list[list[float]]:
    """Embed raw texts (prefixing already applied by the parent)."""
    assert _MODEL is not None, "worker used before init_worker ran"
    return [
        list(map(float, vec))
        for vec in _MODEL.embed(texts, batch_size=batch_size)
    ]


def worker_pid() -> int:
    """Test/diagnostic hook: identify the current child process."""
    return os.getpid()
