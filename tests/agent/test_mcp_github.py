import pytest
from durin.agent.mcp_github import parse_repo_url, resolve_token


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/github/github-mcp-server", ("github", "github-mcp-server")),
    ("https://github.com/stripe/agent-toolkit.git", ("stripe", "agent-toolkit")),
    ("https://github.com/upstash/context7/", ("upstash", "context7")),
    ("git@github.com:microsoft/playwright-mcp.git", ("microsoft", "playwright-mcp")),
    ("https://gitlab.com/foo/bar", None),
    ("", None),
    ("not a url", None),
])
def test_parse_repo_url(url, expected):
    assert parse_repo_url(url) == expected


def test_resolve_token_prefers_gh():
    tok = resolve_token(env={"GITHUB_TOKEN": "env"}, gh_runner=lambda: "gh")
    assert tok == "gh"


def test_resolve_token_env_fallback():
    tok = resolve_token(env={"GITHUB_TOKEN": "env"}, gh_runner=lambda: None)
    assert tok == "env"


def test_resolve_token_durin_env_then_secret():
    assert resolve_token(env={"DURIN_GITHUB_TOKEN": "d"}, gh_runner=lambda: None) == "d"
    got = resolve_token(env={}, gh_runner=lambda: None,
                        secret_getter=lambda n: "sek" if n == "gh_tok" else None,
                        secret_name="gh_tok")
    assert got == "sek"


def test_resolve_token_none():
    assert resolve_token(env={}, gh_runner=lambda: None) is None
