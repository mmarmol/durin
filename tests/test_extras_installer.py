"""Regression: in a pipx-installed gateway the app venv often has no pip/uv,
so the extras auto-installer must fall back to ``pipx inject <app>`` instead of
failing with 'No installer found' (which is what broke the webui [tts] install).
"""

from durin import extras


def test_installer_falls_back_to_pipx_inject(monkeypatch):
    monkeypatch.setattr(extras, "_module_present", lambda m: False)  # no importable pip
    monkeypatch.setattr(extras.shutil, "which", lambda n: None)  # no uv on PATH
    monkeypatch.setattr(extras, "_find_pipx", lambda: "/opt/homebrew/bin/pipx")
    monkeypatch.setattr(extras, "_pipx_app_name", lambda: "durin-agent")
    cmd = extras._installer_cmd(["supertonic>=1.3", "onnxruntime>=1.17,<2.0.0"])
    assert cmd == [
        "/opt/homebrew/bin/pipx",
        "inject",
        "durin-agent",
        "supertonic>=1.3",
        "onnxruntime>=1.17,<2.0.0",
    ]


def test_installer_prefers_pip_when_importable(monkeypatch):
    monkeypatch.setattr(extras, "_module_present", lambda m: m == "pip")
    cmd = extras._installer_cmd(["supertonic>=1.3"])
    assert cmd[:3] == [extras.sys.executable, "-m", "pip"]


def test_installer_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(extras, "_module_present", lambda m: False)
    monkeypatch.setattr(extras.shutil, "which", lambda n: None)
    monkeypatch.setattr(extras, "_find_pipx", lambda: None)
    monkeypatch.setattr(extras, "_pipx_app_name", lambda: None)
    assert extras._installer_cmd(["x"]) is None


def test_extra_specs_matches_single_quote_marker(monkeypatch):
    """Regression: hatchling emits ``extra == 'tts'`` (single quotes); the
    matcher previously only accepted double quotes, so specs came back EMPTY and
    ``_installer_cmd([])`` returned None → the [tts] install failed with
    'No auto-installer available' even though pip/uv/pipx were all available."""
    reqs = [
        "supertonic>=1.3; extra == 'tts'",
        "onnxruntime<2.0.0,>=1.17; extra == 'tts'",
        "other-pkg>=1.0; extra == 'memory'",
    ]
    monkeypatch.setattr(extras.importlib.metadata, "requires", lambda pkg: reqs)
    assert extras._extra_specs("tts") == ["supertonic>=1.3", "onnxruntime<2.0.0,>=1.17"]


def test_extra_specs_matches_double_quote_marker(monkeypatch):
    """setuptools-style double-quote markers must still match."""
    monkeypatch.setattr(
        extras.importlib.metadata, "requires",
        lambda pkg: ['supertonic>=1.3; extra == "tts"'],
    )
    assert extras._extra_specs("tts") == ["supertonic>=1.3"]
