import tomllib
import types
from pathlib import Path

import durin.extras as ex
from durin.extras import REGISTRY, FeatureExtra


def test_registry_extras_exist_in_pyproject():
    """Every registry entry maps to a real pyproject extra (catches typos)."""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    declared = set(pyproject["project"]["optional-dependencies"])
    for fe in REGISTRY.values():
        assert isinstance(fe, FeatureExtra)
        assert fe.extra in declared, f"{fe.feature}: extra '{fe.extra}' not in pyproject"


def test_phase1_features_present():
    assert REGISTRY["web_search"].module == "ddgs"
    assert REGISTRY["web_search"].needs_restart is False
    assert REGISTRY["cross_encoder"].module == "sentence_transformers"
    assert REGISTRY["cross_encoder"].needs_restart is True


class _Cfg:
    def __init__(self, auto=True):
        self.install = types.SimpleNamespace(auto_install_extras=auto)


def test_present_module_is_noop(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: True)
    called = []
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: called.append(specs) or None)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "present"
    assert called == []  # never tried to install


def test_gate_off_returns_disabled(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    r = ex.ensure_extra("web_search", config=_Cfg(auto=False))
    assert r.status == "disabled"
    assert "durin-agent[web]" in r.message


def test_install_success(monkeypatch):
    seen = {"present": False}
    monkeypatch.setattr(ex, "_module_present", lambda m: seen["present"])
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs>=1,<2"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["echo", *specs])

    def fake_run(cmd, **kw):
        seen["present"] = True  # install "worked"
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "installed"
    assert r.needs_restart is False


def test_install_failure_returns_message(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs>=1,<2"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["false", *specs])

    def fake_run(cmd, **kw):
        raise ex.subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "failed"
    assert "boom" in r.message


def test_no_installer_found(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: None)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "failed"
    assert "installer" in r.message.lower()


def test_installer_prefers_pip_then_uv(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: m == "pip")
    cmd = ex._installer_cmd(["pkg"])
    assert cmd[:3] == [ex.sys.executable, "-m", "pip"]
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    cmd = ex._installer_cmd(["pkg"])
    assert cmd[0] == "/usr/bin/uv" and "pip" in cmd and "--python" in cmd


def test_install_config_has_auto_install_default_true():
    from durin.config.schema import InstallConfig
    assert InstallConfig().auto_install_extras is True


def test_post_install_cross_encoder_clears_fallback_flag():
    import durin.memory.cross_encoder as ce
    ce._RERANK_FALLBACK_LOGGED = True
    ex._post_install("cross_encoder")
    assert ce._RERANK_FALLBACK_LOGGED is False


def test_post_install_unknown_is_noop():
    ex._post_install("web_search")  # must not raise


def test_none_config_treated_as_gate_on(monkeypatch):
    """The agent often has app_config=None; treat that as gate-on (default)."""
    seen = {"present": False}
    monkeypatch.setattr(ex, "_module_present", lambda m: seen["present"])
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["echo", *specs])

    def fake_run(cmd, **kw):
        seen["present"] = True
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    r = ex.ensure_extra("web_search", config=None)
    assert r.status == "installed"


def test_ensure_or_note_logs_and_returns(monkeypatch, caplog):
    monkeypatch.setattr(
        ex, "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    import logging
    with caplog.at_level(logging.INFO):
        r = ex.ensure_or_note("slack", config=None)
    assert r.status == "installed"
    assert r.needs_restart is True
    assert any("slack" in rec.message for rec in caplog.records)
