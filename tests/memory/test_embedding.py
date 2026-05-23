"""Tests for the embedding provider abstraction.

fastembed is an optional install (``pip install durin-agent[memory]``)
and the smallest valid model is 90 MB — pulling it on every CI run is
wasteful. The tests inject a fake ``fastembed`` module into ``sys.modules``
that exposes both ``TextEmbedding`` (for the load/embed path) and
``TextEmbedding.list_supported_models`` (for the catalog validation that
runs at ``FastembedProvider.__init__``).
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Iterator

import pytest

import durin.memory.embedding as embedding_module
from durin.memory.embedding import EmbeddingProvider, FastembedProvider


# Catalog snapshot used across tests; matches the real fastembed 0.8.0
# catalog for the models the wizard offers.
_FAKE_CATALOG = [
    {
        "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "size_in_GB": 0.22,
    },
    {
        "model": "intfloat/multilingual-e5-large",
        "dim": 1024,
        "size_in_GB": 2.24,
    },
    {
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "dim": 384,
        "size_in_GB": 0.09,
    },
]


class _FakeTextEmbedding:
    """Minimal stand-in for ``fastembed.TextEmbedding``."""

    last_init_kwargs: dict | None = None

    @staticmethod
    def list_supported_models() -> list[dict]:
        return list(_FAKE_CATALOG)

    def __init__(self, model_name: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        type(self).last_init_kwargs = {"model_name": model_name, **kwargs}

    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        # Deterministic embeddings: dim derived from the fake catalog so
        # the load/embed path stays consistent with what list_supported_models
        # would have advertised.
        catalog = {m["model"]: m["dim"] for m in _FAKE_CATALOG}
        dim = catalog.get(self.model_name or "", 384)
        for i, _ in enumerate(texts):
            yield [float(i) / 10.0] * dim


@contextmanager
def _inject_fake_fastembed():
    """Insert a stub ``fastembed`` module into sys.modules for the duration."""
    _FakeTextEmbedding.last_init_kwargs = None
    # Reset the module-level catalog cache so the fake is consulted.
    embedding_module._CATALOG_CACHE = None
    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake_module
    try:
        yield fake_module
    finally:
        sys.modules.pop("fastembed", None)
        embedding_module._CATALOG_CACHE = None


# ---------------------------------------------------------------------------
# Interface + catalog validation
# ---------------------------------------------------------------------------


def test_provider_is_subclass_of_abstract() -> None:
    assert issubclass(FastembedProvider, EmbeddingProvider)


def test_default_model_is_minilm_l12_multilingual() -> None:
    """Polite default: 220 MB multilingual; e5-large opt-in for CJK heavy."""
    with _inject_fake_fastembed():
        provider = FastembedProvider()
    assert provider.model_name == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_known_dimensions() -> None:
    with _inject_fake_fastembed():
        assert FastembedProvider("intfloat/multilingual-e5-large").dimensions == 1024
        assert (
            FastembedProvider("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2").dimensions
            == 384
        )
        assert (
            FastembedProvider("sentence-transformers/all-MiniLM-L6-v2").dimensions == 384
        )


def test_unknown_model_raises_at_construction() -> None:
    """Unknown identifier surfaces at __init__, not at first embed."""
    with _inject_fake_fastembed():
        with pytest.raises(ValueError, match="is not in fastembed's catalog"):
            FastembedProvider("vendor/unknown-model")


def test_unknown_model_error_lists_available_models() -> None:
    """The error message must guide the user — list real options."""
    with _inject_fake_fastembed():
        with pytest.raises(ValueError) as exc_info:
            FastembedProvider("vendor/unknown-model")
    msg = str(exc_info.value)
    assert "Available models" in msg
    assert "paraphrase-multilingual-MiniLM-L12-v2" in msg
    assert "memory.embedding.model" in msg


def test_retired_model_names_now_raise() -> None:
    """Catalog drift: models the old config defaulted to no longer exist."""
    with _inject_fake_fastembed():
        for retired in (
            "intfloat/multilingual-e5-small",
            "BAAI/bge-m3",
            "intfloat/multilingual-e5-base",
        ):
            with pytest.raises(ValueError, match="not in fastembed's catalog"):
                FastembedProvider(retired)


# ---------------------------------------------------------------------------
# Lazy load + embed
# ---------------------------------------------------------------------------


def test_embed_empty_input_skips_load() -> None:
    """Empty input must not trigger the model load."""
    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-large")
        assert provider.embed([]) == []


def test_embed_lazy_loads_then_returns_embeddings() -> None:
    with _inject_fake_fastembed():
        provider = FastembedProvider()  # default
        out = provider.embed(["hello", "world"])

    assert len(out) == 2
    assert len(out[0]) == 384  # MiniLM-L12 dim
    assert _FakeTextEmbedding.last_init_kwargs == {
        "model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    }


def test_embed_loads_only_once() -> None:
    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-large")
        provider.embed(["one"])
        first_model = provider._model
        provider.embed(["two"])
        second_model = provider._model
    assert first_model is second_model


def test_warmup_loads_and_returns_duration() -> None:
    """`warmup()` is what the wizard and AgentLoop boot call so the
    first user-facing tool call doesn't pay the ~18s download."""
    with _inject_fake_fastembed():
        duration_ms = FastembedProvider.warmup()
    assert duration_ms >= 0


def test_missing_fastembed_raises_clear_error_at_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If fastembed isn't installed, construction fails with a fixable message."""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    embedding_module._CATALOG_CACHE = None
    with pytest.raises(RuntimeError, match="fastembed is required"):
        FastembedProvider("sentence-transformers/all-MiniLM-L6-v2")


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_emits_load_event_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "durin.memory.embedding.emit_tool_event",
        lambda event_type, data: events.append((event_type, data)),
    )

    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-large")
        provider.embed(["hello"])

    load_events = [e for e in events if e[0] == "memory.embedding.load"]
    assert len(load_events) == 1
    payload = load_events[0][1]
    assert payload["model"] == "intfloat/multilingual-e5-large"
    assert payload["duration_ms"] >= 0


def test_emits_embed_event_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.embedding.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )

    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-large")
        provider.embed(["a", "b", "c"])
        provider.embed(["d"])

    embed_events = [e for e in events if e[0] == "memory.embedding.embed"]
    assert len(embed_events) == 2
    assert embed_events[0][1]["batch_size"] == 3
    assert embed_events[1][1]["batch_size"] == 1
    assert all(e[1]["model"] == "intfloat/multilingual-e5-large" for e in embed_events)
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
        provider = FastembedProvider("intfloat/multilingual-e5-large")
        provider.embed(["a"])
        provider.embed(["b"])
        provider.embed(["c"])

    load_events = [e for e in events if e[0] == "memory.embedding.load"]
    assert len(load_events) == 1


# ---------------------------------------------------------------------------
# Config schema integration
# ---------------------------------------------------------------------------


def test_config_default_uses_minilm_l12_multilingual() -> None:
    from durin.config.schema import MemoryEmbeddingConfig

    cfg = MemoryEmbeddingConfig()
    assert cfg.provider == "fastembed"
    assert cfg.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
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
    assert (
        cfg.memory.embedding.model
        == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
