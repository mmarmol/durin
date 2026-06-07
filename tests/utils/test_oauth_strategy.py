from durin.utils import oauth


def test_device_code_when_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert oauth.should_use_device_code() is True


def test_loopback_when_local_gui(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(oauth.sys, "platform", "darwin")
    assert oauth.should_use_device_code() is False


def test_kit_path_resolves_codex_even_without_copilot_constant():
    # Regression: the kit (0.1.x) exports OPENAI_CODEX_PROVIDER but not
    # GITHUB_COPILOT_PROVIDER. Importing them together raised ImportError and
    # dropped codex.json from the search, so any_token_present() reported a
    # logged-in Codex account as "not configured".
    import pytest

    pytest.importorskip("oauth_cli_kit")
    paths = [str(p) for p in oauth.token_storage_paths("openai_codex")]
    assert any(p.endswith("codex.json") for p in paths), paths
