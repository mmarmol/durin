"""GitHub MCP server rides the shared credential — leak-safe env injection."""

import durin.agent.tools.mcp_connection as mc


def test_fills_declared_but_empty_github_token(monkeypatch):
    monkeypatch.setattr("durin.security.github_auth.resolve_github_token", lambda: "gho_SHARED")
    out = mc._inject_shared_github_token({"GITHUB_PERSONAL_ACCESS_TOKEN": "", "OTHER": "x"})
    assert out["GITHUB_PERSONAL_ACCESS_TOKEN"] == "gho_SHARED"
    assert out["OTHER"] == "x"


def test_never_adds_token_to_a_server_that_did_not_declare_one(monkeypatch):
    # no leak: a server without a GitHub-token env var must never receive one
    monkeypatch.setattr("durin.security.github_auth.resolve_github_token", lambda: "gho_SHARED")
    env = {"SOME_OTHER_TOKEN": "", "URL": "x"}
    assert mc._inject_shared_github_token(env) == env
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in mc._inject_shared_github_token(env)


def test_user_provided_token_is_not_overwritten(monkeypatch):
    monkeypatch.setattr("durin.security.github_auth.resolve_github_token", lambda: "gho_SHARED")
    out = mc._inject_shared_github_token({"GITHUB_TOKEN": "user-own"})
    assert out["GITHUB_TOKEN"] == "user-own"


def test_noop_when_shared_credential_is_anonymous(monkeypatch):
    monkeypatch.setattr("durin.security.github_auth.resolve_github_token", lambda: "")
    out = mc._inject_shared_github_token({"GITHUB_PERSONAL_ACCESS_TOKEN": ""})
    assert out["GITHUB_PERSONAL_ACCESS_TOKEN"] == ""


def test_noop_on_empty_env():
    assert mc._inject_shared_github_token(None) is None
    assert mc._inject_shared_github_token({}) == {}
