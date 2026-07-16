"""Batch-size bounding and process isolation for FastembedProvider.

The ONNX CPU arena sizes itself to the peak of the largest embed run and
never returns memory to the OS, so an unbounded batch (fastembed default:
256) ratchets the gateway to multi-GB RSS. These tests pin the two
containment layers: an explicit batch_size on every model call, and a
recyclable worker subprocess.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from durin.config.schema import MemoryEmbeddingConfig
from durin.memory.embedding import FastembedProvider
from tests.memory.test_embedding import _inject_fake_fastembed


class FakeModel:
    """Stands in for fastembed.TextEmbedding; records embed() kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def embed(self, texts, batch_size=256, **kwargs):
        self.calls.append({"n": len(list(texts)), "batch_size": batch_size})
        for _ in range(self.calls[-1]["n"]):
            yield [0.0, 1.0]


def test_schema_defaults():
    cfg = MemoryEmbeddingConfig()
    assert cfg.batch_size == 32
    assert cfg.isolation == "process"
    assert cfg.worker_recycle_batches == 64


def test_schema_bounds():
    with pytest.raises(ValidationError):
        MemoryEmbeddingConfig(batch_size=0)
    with pytest.raises(ValidationError):
        MemoryEmbeddingConfig(isolation="thread")


def test_inline_embed_forwards_bounded_batch_size():
    # Fake fastembed so this runs in CI, which installs no [memory] extra
    # (FastembedProvider.__init__ validates against the model catalog).
    with _inject_fake_fastembed():
        provider = FastembedProvider(batch_size=8, isolation="inline")
        fake = FakeModel()
        provider._model = fake  # bypass the lazy real-model load
        out = provider.embed(["a", "b", "c"])
    assert len(out) == 3
    assert fake.calls == [{"n": 3, "batch_size": 8}]


def test_inline_default_batch_size_is_32_not_library_256():
    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="inline")
        fake = FakeModel()
        provider._model = fake
        provider.embed(["x"])
    assert fake.calls[0]["batch_size"] == 32


def test_process_isolation_parity_with_inline():
    """Same model, same texts → same vectors, whether embedded in-process
    or in the worker subprocess. Downloads/loads the real model twice —
    skipped wherever the [memory] extra is absent (CI)."""
    pytest.importorskip("fastembed")
    texts = ["hello world", "durin memory", "embedding parity"]
    inline = FastembedProvider(isolation="inline")
    proc = FastembedProvider(isolation="process", recycle_batches=64)
    v_inline = inline.embed(texts)
    v_proc = proc.embed(texts)
    assert len(v_inline) == len(v_proc) == 3
    for a, b in zip(v_inline, v_proc):
        assert a == pytest.approx(b, abs=1e-6)


def test_worker_recycles_after_max_batches():
    """recycle_batches=1 → every embed call lands in a fresh child, which
    is what bounds the arena ratchet."""
    pytest.importorskip("fastembed")
    from durin.memory import embedding_worker

    provider = FastembedProvider(isolation="process", recycle_batches=1)
    pool = provider._ensure_pool()
    pid_a = pool.submit(embedding_worker.worker_pid).result()
    pid_b = pool.submit(embedding_worker.worker_pid).result()
    assert pid_a != pid_b


def test_process_mode_falls_back_inline_on_pool_failure(monkeypatch, caplog):
    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    fake = FakeModel()
    provider._model = fake

    def boom():
        raise OSError("spawn refused")

    monkeypatch.setattr(provider, "_ensure_pool", boom)
    out = provider.embed(["a", "b"])
    assert len(out) == 2
    assert fake.calls and fake.calls[0]["batch_size"] == 32
    assert provider._isolation == "inline"  # permanent for this process
