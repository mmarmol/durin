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
#
# `intfloat/multilingual-e5-small` is included even though fastembed
# does NOT ship it in the default catalog — durin registers it as a
# custom model via `_register_custom_models()`. The fake catalog
# simulates the post-registration state, and the fake's
# `add_custom_model` is a no-op so the call from durin's registration
# code doesn't crash.
_FAKE_CATALOG = [
    {
        "model": "intfloat/multilingual-e5-small",
        "dim": 384,
        "size_in_GB": 0.45,
    },
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
    embed_calls: list[list[str]] = []

    @staticmethod
    def list_supported_models() -> list[dict]:
        return list(_FAKE_CATALOG)

    @staticmethod
    def add_custom_model(**_kwargs) -> None:
        # No-op stub. Durin's `_register_custom_models()` calls this at
        # import time on the real fastembed; the fake catalog already
        # includes the custom models, so we skip the side-effect.
        pass

    def __init__(self, model_name: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        type(self).last_init_kwargs = {"model_name": model_name, **kwargs}

    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        # Deterministic embeddings: dim derived from the fake catalog so
        # the load/embed path stays consistent with what list_supported_models
        # would have advertised. Also record the input verbatim so tests
        # can assert that the E5 `query:` / `passage:` prefix made it
        # through `embed_passages` / `embed_query`.
        type(self).embed_calls.append(list(texts))
        catalog = {m["model"]: m["dim"] for m in _FAKE_CATALOG}
        dim = catalog.get(self.model_name or "", 384)
        for i, _ in enumerate(texts):
            yield [float(i) / 10.0] * dim


# Fake fastembed.common.model_description module — durin's
# `_register_custom_models()` imports PoolingType + ModelSource from
# this submodule. Both are stub classes; the no-op `add_custom_model`
# never reads the values.
class _FakePoolingType:
    MEAN = "MEAN"
    CLS = "CLS"


class _FakeModelSource:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@contextmanager
def _inject_fake_fastembed():
    """Insert a stub ``fastembed`` module into sys.modules for the duration."""
    _FakeTextEmbedding.last_init_kwargs = None
    _FakeTextEmbedding.embed_calls = []
    # Reset the module-level catalog cache + custom-model registration
    # so the fake is consulted from a clean state.
    embedding_module._CATALOG_CACHE = None
    embedding_module._REGISTERED_CUSTOM = set()
    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    fake_common = types.ModuleType("fastembed.common")
    fake_model_desc = types.ModuleType("fastembed.common.model_description")
    fake_model_desc.PoolingType = _FakePoolingType  # type: ignore[attr-defined]
    fake_model_desc.ModelSource = _FakeModelSource  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake_module
    sys.modules["fastembed.common"] = fake_common
    sys.modules["fastembed.common.model_description"] = fake_model_desc
    try:
        yield fake_module
    finally:
        sys.modules.pop("fastembed", None)
        sys.modules.pop("fastembed.common", None)
        sys.modules.pop("fastembed.common.model_description", None)
        embedding_module._CATALOG_CACHE = None
        embedding_module._REGISTERED_CUSTOM = set()


# ---------------------------------------------------------------------------
# Interface + catalog validation
# ---------------------------------------------------------------------------


def test_provider_is_subclass_of_abstract() -> None:
    assert issubclass(FastembedProvider, EmbeddingProvider)


def test_default_model_is_multilingual_e5_small() -> None:
    """Default since 2026-05-30: multilingual-e5-small (MIT, 117M
    params, retrieval-tuned). Replaced MiniLM-L12 — same backbone
    architecture but fine-tuned with contrastive retrieval objective.
    Replaced MiniLM-L12 — same backbone architecture but fine-tuned with
    contrastive retrieval objective."""
    with _inject_fake_fastembed():
        provider = FastembedProvider()
    assert provider.model_name == "intfloat/multilingual-e5-small"


def test_known_dimensions() -> None:
    with _inject_fake_fastembed():
        assert FastembedProvider("intfloat/multilingual-e5-small").dimensions == 384
        assert FastembedProvider("intfloat/multilingual-e5-large").dimensions == 1024
        assert (
            FastembedProvider("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2").dimensions
            == 384
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
    """Catalog drift: model ids that durin doesn't support must fail
    construction with a clear message. Removed
    `intfloat/multilingual-e5-small` from this list on 2026-05-30 when
    we promoted it to the default; left `bge-m3` and `e5-base` because
    we don't register them as custom models."""
    with _inject_fake_fastembed():
        for retired in (
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
    assert len(out[0]) == 384  # e5-small dim (same as old default)
    assert _FakeTextEmbedding.last_init_kwargs == {
        "model_name": "intfloat/multilingual-e5-small"
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


def test_config_default_uses_multilingual_e5_small() -> None:
    from durin.config.schema import MemoryEmbeddingConfig

    cfg = MemoryEmbeddingConfig()
    assert cfg.provider == "fastembed"
    assert cfg.model == "intfloat/multilingual-e5-small"
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
    assert cfg.memory.embedding.model == "intfloat/multilingual-e5-small"


# ---------------------------------------------------------------------------
# E5 prefix + custom-model registration (2026-05-30)
# ---------------------------------------------------------------------------


def test_e5_family_detection() -> None:
    """`_is_e5_family` recognises intfloat/e5-* and intfloat/
    multilingual-e5-* model ids and ignores everything else."""
    from durin.memory.embedding import _is_e5_family

    assert _is_e5_family("intfloat/multilingual-e5-small")
    assert _is_e5_family("intfloat/multilingual-e5-large")
    assert _is_e5_family("intfloat/e5-base-v2")
    assert not _is_e5_family(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert not _is_e5_family("BAAI/bge-small-en")


def test_embed_passages_applies_e5_prefix() -> None:
    """E5 models receive the `passage: ` prefix on storage writes."""
    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-small")
        provider.embed_passages(["hello", "world"])

    # The fake records the raw input it embedded — verify prefix applied.
    assert _FakeTextEmbedding.embed_calls == [
        ["passage: hello", "passage: world"]
    ]


def test_embed_query_applies_e5_prefix() -> None:
    """E5 models receive the `query: ` prefix on search."""
    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-small")
        provider.embed_query("what does Audrey eat")

    assert _FakeTextEmbedding.embed_calls == [
        ["query: what does Audrey eat"]
    ]


def test_non_e5_models_pass_through_without_prefix() -> None:
    """Non-E5 models (MiniLM, BGE, etc.) must NOT receive a prefix —
    the wrap is family-specific."""
    with _inject_fake_fastembed():
        provider = FastembedProvider(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
        provider.embed_passages(["hello"])
        provider.embed_query("a query")

    assert _FakeTextEmbedding.embed_calls == [
        ["hello"],
        ["a query"],
    ]


def test_embed_passages_empty_input_skips_load() -> None:
    """Empty list must not trigger model load (mirrors `embed`)."""
    with _inject_fake_fastembed():
        provider = FastembedProvider("intfloat/multilingual-e5-small")
        assert provider.embed_passages([]) == []
        assert provider._model is None


def test_embed_query_returns_single_vector() -> None:
    """`embed_query` returns one vector (not a list of one)."""
    with _inject_fake_fastembed():
        provider = FastembedProvider()
        vec = provider.embed_query("test")
    assert isinstance(vec, list)
    assert len(vec) == 384  # not wrapped in another list


def test_register_custom_models_idempotent() -> None:
    """`_register_custom_models` must not crash on repeated calls.

    Two safeguards: (1) `_REGISTERED_CUSTOM` set short-circuits within
    one process, and (2) the function skips models already present in
    fastembed's catalog (so PRs landing custom models upstream become
    free no-ops without code change here)."""
    from durin.memory.embedding import _register_custom_models

    with _inject_fake_fastembed():
        # Should not raise even though the fake catalog already has
        # multilingual-e5-small (the registration skip branch fires).
        _register_custom_models()
        _register_custom_models()
        _register_custom_models()


def test_list_supported_models_includes_custom_after_registration() -> None:
    """`list_supported_models()` calls `_register_custom_models()`
    internally, so the returned dict must include the custom models."""
    from durin.memory.embedding import _CUSTOM_MODELS, list_supported_models

    with _inject_fake_fastembed():
        catalog = list_supported_models()
    for model_id in _CUSTOM_MODELS:
        assert model_id in catalog, (
            f"Custom model {model_id!r} missing from list_supported_models()"
        )
