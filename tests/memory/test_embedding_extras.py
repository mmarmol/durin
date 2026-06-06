import builtins
import types

import pytest

import durin.memory.embedding as emb


def test_list_models_calls_ensure_when_fastembed_missing(monkeypatch):
    """A missing `memory` extra triggers ensure_or_note before the RuntimeError."""
    monkeypatch.setattr(emb, "_CATALOG_CACHE", None)
    real_import = builtins.__import__

    def block(name, *a, **k):
        if name == "fastembed":
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block)
    calls = []
    monkeypatch.setattr(
        emb,
        "ensure_or_note",
        lambda feature, *, config: calls.append(feature)
        or types.SimpleNamespace(status="failed", needs_restart=True, message="x"),
    )
    with pytest.raises(RuntimeError):
        emb.list_supported_models()
    assert calls == ["memory_vector"]
