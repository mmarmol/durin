import pytest

from durin.agent.mcp_github import (
    GithubMeta,
    classify_official,
    fetch_repo_meta,
    parse_repo_url,
    resolve_token,
)


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


def _fake_post(query, token):
    # one repo present, one missing (null node)
    return {"data": {
        "r0": {"stargazerCount": 30810,
               "owner": {"__typename": "Organization", "login": "github",
                         "url": "https://github.com/github",
                         "avatarUrl": "https://avatars/github"},
               "repositoryTopics": {"nodes": [{"topic": {"name": "mcp"}},
                                               {"topic": {"name": "git"}}]},
               "primaryLanguage": {"name": "Go"},
               "licenseInfo": {"spdxId": "MIT"},
               "description": "GitHub MCP server."},
        "r1": None,
    }}


def test_fetch_repo_meta_parses_and_handles_missing():
    out = fetch_repo_meta([("github", "github-mcp-server"), ("ghost", "gone")],
                          token="t", post=_fake_post)
    g = out[("github", "github-mcp-server")]
    assert isinstance(g, GithubMeta)
    assert g.stars == 30810
    assert g.owner_type == "Organization"
    assert g.owner_login == "github"
    assert g.owner_url == "https://github.com/github"
    assert g.topics == ["mcp", "git"]
    assert g.language == "Go"
    assert g.license == "MIT"
    missing = out[("ghost", "gone")]
    assert missing.stars is None


def test_fetch_repo_meta_empty():
    assert fetch_repo_meta([], token="t", post=_fake_post) == {}


def test_fetch_repo_meta_paces_between_batches():
    """fetch_repo_meta sleeps `pace` seconds between batches (avoids secondary limits)."""
    keys = [("o", f"r{i}") for i in range(5)]
    slept: list[float] = []
    fetch_repo_meta(keys, token="t", post=lambda q, t: {"data": {}},
                    batch=2, pace=0.5, sleep=slept.append)
    # 5 keys / batch 2 = 3 batches -> 2 inter-batch sleeps (none before the first)
    assert slept == [0.5, 0.5]


def test_fetch_repo_meta_no_pacing_by_default():
    """No pace -> no sleeps; default behavior unchanged."""
    keys = [("o", f"r{i}") for i in range(5)]
    slept: list[float] = []
    fetch_repo_meta(keys, token="t", post=lambda q, t: {"data": {}},
                    batch=2, sleep=slept.append)
    assert slept == []


def test_official_vendor_domain():
    assert classify_official("com.stripe/mcp", owner_type="Organization", stars=5) is True


def test_official_reference_namespace():
    assert classify_official("io.modelcontextprotocol/everything",
                             owner_type="", stars=None) is True


def test_official_org_with_many_stars():
    assert classify_official("io.github.github/github-mcp-server",
                             owner_type="Organization", stars=30810) is True


def test_not_official_org_low_stars():
    # pipeworx-io: org, but low stars and io.github namespace
    assert classify_official("io.github.pipeworx-io/x",
                             owner_type="Organization", stars=2) is False


def test_not_official_rehoster_domain():
    assert classify_official("ai.smithery/smithery-notion",
                             owner_type="Organization", stars=5) is False
