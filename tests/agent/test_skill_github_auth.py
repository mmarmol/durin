"""A6 — GitHub token via durin secrets, attached ONLY to GitHub hosts."""
from types import SimpleNamespace

import durin.agent.skill_resolve as R


def test_is_github_url_guard():
    assert R._is_github_url("https://api.github.com/repos/o/r")
    assert R._is_github_url("https://raw.githubusercontent.com/o/r/main/SKILL.md")
    # must NOT match arbitrary or look-alike hosts (token would leak)
    assert not R._is_github_url("https://example.com/x/SKILL.md")
    assert not R._is_github_url("https://api.github.com.attacker.com/")
    assert not R._is_github_url("http://api.github.com/repos/o/r")  # plain http


def test_github_token_empty_when_unconfigured():
    # default config has github_token_secret="" → anonymous
    assert R._github_token() == ""


def _cfg(secret_name):
    return SimpleNamespace(
        skills=SimpleNamespace(security=SimpleNamespace(github_token_secret=secret_name)))


def test_github_token_resolves_named_secret(monkeypatch):
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: _cfg("ghtok"))
    monkeypatch.setattr("durin.security.secrets.resolve_secret",
                        lambda ref: "TOKEN123" if "ghtok" in str(ref) else None)
    assert R._github_token() == "TOKEN123"


def test_github_token_degrades_when_secret_missing(monkeypatch):
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: _cfg("missing"))

    def _raise(ref):
        raise RuntimeError("secret not found")

    monkeypatch.setattr("durin.security.secrets.resolve_secret", _raise)
    assert R._github_token() == ""  # never raises → anonymous fetch


def test_token_test_requires_secret():
    from durin.agent.skills_store import web_github_token_test
    status, _ = web_github_token_test("")
    assert status == 400


def test_token_test_missing_secret_no_network(monkeypatch):
    from durin.agent import skills_store

    def _raise(ref):
        raise RuntimeError("not found")

    monkeypatch.setattr("durin.security.secrets.resolve_secret", _raise)
    status, payload = skills_store.web_github_token_test("ghx")
    assert status == 200 and payload["ok"] is False


def test_token_test_empty_secret_no_network(monkeypatch):
    from durin.agent import skills_store

    monkeypatch.setattr("durin.security.secrets.resolve_secret", lambda ref: "")
    status, payload = skills_store.web_github_token_test("ghx")
    assert status == 200 and payload["ok"] is False
