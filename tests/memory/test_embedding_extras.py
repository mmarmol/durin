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
    # The site loads a config for the auto-install gate; keep the test off
    # the real loader even though ensure_or_note itself is mocked below.
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: types.SimpleNamespace()
    )
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


def test_opted_out_config_blocks_install_when_fastembed_missing(monkeypatch):
    """The site has no config in hand, so it loads one for the gate:
    install.auto_install_extras=False must block the auto-install (no
    installer subprocess) and surface the manual install command."""
    import durin.extras as ex

    monkeypatch.setattr(emb, "_CATALOG_CACHE", None)
    real_import = builtins.__import__

    def block(name, *a, **k):
        if name == "fastembed":
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block)
    opted_out = types.SimpleNamespace(
        install=types.SimpleNamespace(auto_install_extras=False)
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: opted_out)
    # The __import__ block above only covers import statements; the gate's own
    # probe uses importlib, so fake the module as missing there explicitly.
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["fastembed"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["echo", *specs])
    run_calls = []
    monkeypatch.setattr(
        ex.subprocess, "run",
        lambda *a, **k: run_calls.append(a)
        or types.SimpleNamespace(returncode=0, stderr="", stdout=""),
    )
    with pytest.raises(RuntimeError, match=r"durin-agent\[memory\]"):
        emb.list_supported_models()
    assert run_calls == []  # opt-out honored: no installer subprocess


def test_check_path_asks_without_installing(memory_extra_missing, monkeypatch):
    """A caller that only checks (doctor, the onboarding probe) asks with
    install=False: the missing extra raises, and nothing is installed even
    with install.auto_install_extras on (the default config)."""
    noted = []
    monkeypatch.setattr(
        emb, "ensure_or_note", lambda feature, *, config: noted.append(feature)
    )
    with pytest.raises(RuntimeError, match=r"durin-agent\[memory\]"):
        emb.list_supported_models(install=False)
    assert memory_extra_missing.calls == []
    assert noted == []


@pytest.mark.parametrize(
    "use",
    [
        pytest.param(
            lambda: emb.FastembedProvider(model=emb.FastembedProvider.DEFAULT_MODEL,
                                          isolation="inline"),
            id="provider-construction",
        ),
        pytest.param(
            lambda: emb.model_dimensions(emb.FastembedProvider.DEFAULT_MODEL),
            id="model-dimensions",
        ),
    ],
)
def test_activation_path_still_installs(memory_extra_missing, use):
    """Using vector memory with the extra missing installs it (gate on by
    default) and then works on the installed package."""
    use()
    assert len(memory_extra_missing.calls) == 1
    assert any(spec.startswith("fastembed") for spec in memory_extra_missing.calls[0])
