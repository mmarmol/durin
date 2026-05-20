"""Tests for the embedding provider abstraction.

fastembed is an optional install (``pip install durin[memory]``) and
the default model is 2.2 GB — pulling it on every CI run is wasteful.
The tests inject a fake ``fastembed`` module into ``sys.modules`` so
the lazy import inside :meth:`FastembedProvider._load` resolves to a
controlled stub. This exercises the load/embed paths plus the
telemetry contracts without any network or disk activity.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from durin.memory.embedding import EmbeddingProvider, FastembedProvider


class _FakeTextEmbedding:
    """Minimal stand-in for ``fastembed.TextEmbedding``."""

    last_init_kwargs: dict | None = None

    def __init__(self, model_name: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        type(self).last_init_kwargs = {"model_name": model_name, **kwargs}

    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        # Deterministic embeddings: dim follows the model name's expected dim.
        dim = 1024 if "bge-m3" in (self.model_name or "") else 384
        for i, _ in enumerate(texts):
            yield [float(i) / 10.0] * dim


@contextmanager
def _inject_fake_fastembed():
    """Insert a stub ``fastembed`` module into sys.modules for the duration."""
    _FakeTextEmbedding.last_init_kwargs = None
    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake_module
    try:
        yield fake_module
    finally:
        sys.modules.pop("fastembed", None)


# ---------------------------------------------------------------------------
# Interface + dimensions
# ---------------------------------------------------------------------------


def test_provider_is_subclass_of_abstract() -> None:
    assert issubclass(FastembedProvider, EmbeddingProvider)


def test_default_model_is_bge_m3() -> None:
    provider = FastembedProvider()
    assert provider.model_name == "BAAI/bge-m3"


def test_known_dimensions() -> None:
    assert FastembedProvider("BAAI/bge-m3").dimensions == 1024
    assert FastembedProvider("intfloat/multilingual-e5-small").dimensions == 384
    assert FastembedProvider("intfloat/multilingual-e5-base").dimensions == 768


def test_unknown_model_raises_on_dimensions() -> None:
    provider = FastembedProvider("vendor/unknown-model")
    with pytest.raises(ValueError, match="unknown fastembed model"):
        _ = provider.dimensions


# ---------------------------------------------------------------------------
# Lazy load + embed
# ---------------------------------------------------------------------------


def test_embed_empty_input_skips_load() -> None:
    """Empty input must not trigger the model load."""
    provider = FastembedProvider("BAAI/bge-m3")
    # Don't inject the fake — if load is attempted, the real (missing)
    # fastembed import would raise.
    assert provider.embed([]) == []


def test_embed_lazy_loads_then_returns_embeddings() -> None:
    with _inject_fake_fastembed():
        provider = FastembedProvider("BAAI/bge-m3")
        out = provider.embed(["hello", "world"])

    assert len(out) == 2
    assert len(out[0]) == 1024
    assert _FakeTextEmbedding.last_init_kwargs == {"model_name": "BAAI/bge-m3"}


def test_embed_loads_only_once() -> None:
    with _inject_fake_fastembed():
        provider = FastembedProvider("BAAI/bge-m3")
        provider.embed(["one"])
        first_model = provider._model
        provider.embed(["two"])
        second_model = provider._model
    assert first_model is second_model


def test_missing_fastembed_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)
    provider = FastembedProvider("BAAI/bge-m3")
    with pytest.raises(RuntimeError, match="fastembed is required"):
        provider.embed(["x"])


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_emits_load_event_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict]] = []

    def capture(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    monkeypatch.setattr(
        "durin.memory.embedding.emit_tool_event",
        capture,
    )

    with _inject_fake_fastembed():
        provider = FastembedProvider("BAAI/bge-m3")
        provider.embed(["hello"])

    load_events = [e for e in events if e[0] == "memory.embedding.load"]
    assert len(load_events) == 1
    payload = load_events[0][1]
    assert payload["model"] == "BAAI/bge-m3"
    assert payload["duration_ms"] >= 0


def test_emits_embed_event_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.embedding.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )

    with _inject_fake_fastembed():
        provider = FastembedProvider("BAAI/bge-m3")
        provider.embed(["a", "b", "c"])
        provider.embed(["d"])

    embed_events = [e for e in events if e[0] == "memory.embedding.embed"]
    assert len(embed_events) == 2
    assert embed_events[0][1]["batch_size"] == 3
    assert embed_events[1][1]["batch_size"] == 1
    assert all(e[1]["model"] == "BAAI/bge-m3" for e in embed_events)
    assert all(e[1]["duration_ms"] >= 0 for e in embed_events)


def test_load_event_fires_once_across_multiple_embed_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.embedding.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )

    with _inject_fake_fastembed():
        provider = FastembedProvider("BAAI/bge-m3")
        provider.embed(["a"])
        provider.embed(["b"])
        provider.embed(["c"])

    load_events = [e for e in events if e[0] == "memory.embedding.load"]
    assert len(load_events) == 1


# ---------------------------------------------------------------------------
# Config schema integration
# ---------------------------------------------------------------------------


def test_config_default_uses_bge_m3() -> None:
    from durin.config.schema import MemoryEmbeddingConfig

    cfg = MemoryEmbeddingConfig()
    assert cfg.provider == "fastembed"
    assert cfg.model == "BAAI/bge-m3"
    assert cfg.base_url is None
    assert cfg.api_key is None
    assert cfg.lazy_eviction is False


def test_config_camel_case_aliases() -> None:
    from durin.config.schema import MemoryEmbeddingConfig

    cfg = MemoryEmbeddingConfig.model_validate(
        {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "sk-test",
            "lazyEviction": True,
        }
    )
    assert cfg.provider == "openai"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == "sk-test"
    assert cfg.lazy_eviction is True


def test_config_memory_section_exposed_at_root() -> None:
    from durin.config.schema import Config

    cfg = Config()
    assert cfg.memory.embedding.model == "BAAI/bge-m3"
