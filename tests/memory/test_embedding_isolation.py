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
    # "service" = the gateway-supervised standing embedding server; providers
    # without a discovered server fall back to the per-process pool.
    assert cfg.isolation == "service"
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


def test_process_mode_falls_back_inline_on_pool_failure(monkeypatch):
    from loguru import logger

    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    fake = FakeModel()
    provider._model = fake

    def boom():
        raise OSError("spawn refused")

    monkeypatch.setattr(provider, "_ensure_pool", boom)
    errors: list[str] = []
    sink_id = logger.add(errors.append, level="ERROR", format="{message}")
    try:
        out = provider.embed(["a", "b"])
    finally:
        logger.remove(sink_id)
    assert len(out) == 2
    assert fake.calls and fake.calls[0]["batch_size"] == 32
    assert provider._isolation == "inline"  # permanent for this process
    assert any("falling back to inline" in m for m in errors)


class _StubPool:
    """Stands in for the ProcessPoolExecutor; submit().result() raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.shutdown_calls: list[dict] = []

    def submit(self, fn, *args):
        exc = self._exc

        class _Future:
            def result(self):
                raise exc

        return _Future()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls.append(
            {"wait": wait, "cancel_futures": cancel_futures}
        )


def test_infra_failure_falls_back_inline_and_releases_pool():
    """BrokenProcessPool from the worker = infrastructure failure →
    permanent inline fallback, pool explicitly shut down and dropped."""
    from concurrent.futures.process import BrokenProcessPool

    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    fake = FakeModel()
    provider._model = fake
    stub = _StubPool(BrokenProcessPool("child died"))
    provider._pool = stub

    out = provider.embed(["a", "b"])
    assert len(out) == 2
    assert provider._isolation == "inline"
    assert provider._pool is None
    assert stub.shutdown_calls == [{"wait": False, "cancel_futures": True}]


def test_task_level_error_propagates_without_flipping_isolation():
    """A task-level error inside embed_batch (e.g. malformed text) must
    reach the caller unchanged — one bad input must not tear down a
    healthy pool or silently defeat arena containment."""
    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    stub = _StubPool(ValueError("bad input"))
    provider._pool = stub

    with pytest.raises(ValueError, match="bad input"):
        provider.embed(["a", "b"])
    assert provider._isolation == "process"
    assert provider._pool is stub
    assert stub.shutdown_calls == []


def test_provider_from_config_reads_all_knobs():
    from durin.config.schema import Config
    from durin.memory.embedding import provider_from_config

    cfg = Config()
    cfg.memory.embedding.batch_size = 8
    cfg.memory.embedding.isolation = "inline"
    cfg.memory.embedding.worker_recycle_batches = 5
    with _inject_fake_fastembed():
        p = provider_from_config(cfg)
        assert p._batch_size == 8
        assert p._isolation == "inline"
        assert p._recycle_batches == 5
        # explicit model override wins over cfg
        p2 = provider_from_config(cfg, model=p.model_name)
        assert p2.model_name == p.model_name


def test_infra_fallback_emits_telemetry(monkeypatch):
    """Losing arena containment must be observable: the inline fallback
    emits a memory.embedding.pool_fallback event (the 2026-07-18 incident
    showed a silent fallback is invisible post-mortem)."""
    from concurrent.futures.process import BrokenProcessPool

    from durin.memory import embedding as embedding_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        embedding_mod, "emit_tool_event",
        lambda name, payload: events.append((name, payload)),
    )
    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    provider._model = FakeModel()
    provider._pool = _StubPool(BrokenProcessPool("child died"))

    provider.embed(["a"])
    names = [n for n, _ in events]
    assert "memory.embedding.pool_fallback" in names
    payload = dict(events[names.index("memory.embedding.pool_fallback")][1])
    assert payload.get("model")
