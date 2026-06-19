import pytest
from durin.agent.mcp_github import parse_repo_url


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
