import builtins
import types

import pytest

import durin.providers.local_llama_provider as llp


def test_load_llama_calls_ensure_when_missing(monkeypatch):
    """A missing `local` extra triggers ensure_or_note before the ImportError."""
    real_import = builtins.__import__

    def block(name, *a, **k):
        if name == "llama_cpp":
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block)
    calls = []
    monkeypatch.setattr(
        llp,
        "ensure_or_note",
        lambda feature, *, config: calls.append(feature)
        or types.SimpleNamespace(status="failed", needs_restart=True, message="x"),
    )
    prov = llp.LocalLlamaProvider.__new__(llp.LocalLlamaProvider)
    prov._app_config = None
    with pytest.raises(ImportError):
        prov._load_llama("/tmp/m.gguf", "m")
    assert calls == ["local_models"]
