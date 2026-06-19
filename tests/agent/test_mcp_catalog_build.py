"""TDD tests for mcp_catalog_build.build_catalog.

All network seams are injected — no real HTTP calls here.
"""
from __future__ import annotations

from durin.agent.mcp_catalog_build import build_catalog
from durin.agent.mcp_github import GithubMeta

_NOW = "2026-06-19T06:00:00Z"

# ---------------------------------------------------------------------------
# Fake registry pages
# ---------------------------------------------------------------------------

_SERVER_WITH_REPO = {
    "name": "io.github.github/github-mcp-server",
    "description": "GitHub MCP Server",
    "packages": [{"registryType": "npm", "identifier": "@github/mcp"}],
    "remotes": [],
    "repository": {"url": "https://github.com/github/github-mcp-server"},
}

_SERVER_WITH_REMOTE = {
    "name": "io.modelcontextprotocol/everything",
    "description": "Everything MCP server",
    "packages": [],
    "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com"}],
    "repository": {"url": ""},
}

_SERVER_BOTH = {
    "name": "com.stripe/agent-toolkit",
    "description": "Stripe MCP",
    "packages": [{"registryType": "npm", "identifier": "@stripe/mcp"}],
    "remotes": [{"type": "sse", "url": "https://stripe.example.com"}],
    "repository": {"url": "https://github.com/stripe/agent-toolkit"},
}

_SERVER_NO_REPO = {
    "name": "io.github.norepo/norepo-mcp",
    "description": "No repo",
    "packages": [{"registryType": "npm", "identifier": "@norepo/mcp"}],
    "remotes": [],
    "repository": {"url": ""},
}

# Page 1: first two servers; page 2: last two servers
_PAGE_1 = ([_SERVER_WITH_REPO, _SERVER_WITH_REMOTE], "cursor2")
_PAGE_2 = ([_SERVER_BOTH, _SERVER_NO_REPO], None)


def _fake_fetch_page(*, cursor=None, updated_since=None):
    if cursor is None:
        return _PAGE_1
    return _PAGE_2


# ---------------------------------------------------------------------------
# Fake GitHub meta
# ---------------------------------------------------------------------------

_GH_META = {
    ("github", "github-mcp-server"): GithubMeta(
        stars=30810,
        owner_login="github",
        owner_type="Organization",
        owner_url="https://github.com/github",
        owner_avatar="https://avatars.githubusercontent.com/u/9919",
        topics=["mcp", "github"],
        language="Go",
        license="MIT",
        about="GitHub MCP server.",
    ),
    ("stripe", "agent-toolkit"): GithubMeta(
        stars=5000,
        owner_login="stripe",
        owner_type="Organization",
        owner_url="https://github.com/stripe",
        owner_avatar="https://avatars.githubusercontent.com/stripe",
        topics=["payments"],
        language="TypeScript",
        license="MIT",
        about="Stripe agent toolkit.",
    ),
}


def _fake_fetch_repo_meta(repo_keys, *, token, post=None, batch=80):
    return {k: _GH_META.get(k, GithubMeta(stars=None)) for k in repo_keys}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_schema_version_and_generated_at():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    assert result["schema_version"] == 1
    assert result["generated_at"] == _NOW


def test_server_count():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    assert len(result["servers"]) == 4


def test_server_with_github_repo_enriched():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    servers_by_name = {s["name"]: s for s in result["servers"]}
    s = servers_by_name["io.github.github/github-mcp-server"]

    assert s["stars"] == 30810
    assert s["owner_login"] == "github"
    assert s["owner_url"] == "https://github.com/github"
    assert s["owner_avatar"] == "https://avatars.githubusercontent.com/u/9919"
    assert s["topics"] == ["mcp", "github"]
    assert s["language"] == "Go"
    assert s["license"] == "MIT"
    assert s["repo_url"] == "https://github.com/github/github-mcp-server"
    assert s["official"] is True  # Org + >1000 stars
    assert s["kind"] == "local"   # only packages
    assert s["name"] == "io.github.github/github-mcp-server"
    assert s["description"] == "GitHub MCP Server"


def test_server_no_repo_gets_stars_none():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    servers_by_name = {s["name"]: s for s in result["servers"]}
    s = servers_by_name["io.github.norepo/norepo-mcp"]

    assert s["stars"] is None
    assert s["official"] is False
    assert s["owner_login"] == ""


def test_kind_derivation():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    servers_by_name = {s["name"]: s for s in result["servers"]}

    assert servers_by_name["io.github.github/github-mcp-server"]["kind"] == "local"
    assert servers_by_name["io.modelcontextprotocol/everything"]["kind"] == "remote"
    assert servers_by_name["com.stripe/agent-toolkit"]["kind"] == "both"


def test_official_reference_namespace_no_repo():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    servers_by_name = {s["name"]: s for s in result["servers"]}
    s = servers_by_name["io.modelcontextprotocol/everything"]
    assert s["official"] is True  # reference namespace
    assert s["stars"] is None     # no repo → no meta


def test_official_vendor_domain():
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    servers_by_name = {s["name"]: s for s in result["servers"]}
    s = servers_by_name["com.stripe/agent-toolkit"]
    assert s["official"] is True  # DNS-verified vendor domain


def test_all_required_fields_present():
    required = {
        "name", "ref", "description", "kind", "stars",
        "owner_login", "owner_url", "owner_avatar",
        "topics", "language", "license", "official", "repo_url",
    }
    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
    )
    for s in result["servers"]:
        missing = required - s.keys()
        assert not missing, f"Server {s['name']!r} missing fields: {missing}"


def test_fetch_repo_meta_called_once_with_unique_repos():
    """fetch_repo_meta is called once (not per-page) with deduplicated repo keys."""
    calls = []

    def tracking_fetch(repo_keys, *, token, post=None, batch=80):
        calls.append(list(repo_keys))
        return _fake_fetch_repo_meta(repo_keys, token=token, post=post, batch=batch)

    build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=tracking_fetch,
        now=_NOW,
    )
    assert len(calls) == 1
    # Only repos that have a parseable GitHub URL
    called_repos = set(calls[0])
    assert ("github", "github-mcp-server") in called_repos
    assert ("stripe", "agent-toolkit") in called_repos
    # No-repo servers should not be in the list
    all_names = [name for (_, name) in called_repos]
    assert "norepo-mcp" not in all_names


def test_mixed_case_repo_url_enriched():
    """Repos with mixed-case owner/name in the URL must be enriched correctly.

    fetch_repo_meta stores results under lowercased keys (matching GitHub's
    case-insensitive repo identity). build_catalog must normalise to lowercase
    before looking up, otherwise the lookup silently misses and stars==None.
    """
    _SERVER_MIXED_CASE = {
        "name": "io.github.ChromeDevTools/chrome-devtools-mcp",
        "description": "Chrome DevTools MCP",
        "packages": [{"registryType": "npm", "identifier": "@chrome/mcp"}],
        "remotes": [],
        "repository": {"url": "https://github.com/ChromeDevTools/chrome-devtools-mcp"},
    }

    def fetch_page_single(*, cursor=None, updated_since=None):
        return ([_SERVER_MIXED_CASE], None)

    # fetch_repo_meta returns lowercased keys — mirrors real implementation
    def fetch_meta_lowercased(repo_keys, *, token, post=None, batch=80):
        return {
            ("chromedevtools", "chrome-devtools-mcp"): GithubMeta(
                stars=43982,
                owner_login="ChromeDevTools",
                owner_type="Organization",
                owner_url="https://github.com/ChromeDevTools",
                owner_avatar="https://avatars.githubusercontent.com/chromedevtools",
                topics=["devtools"],
                language="TypeScript",
                license="Apache-2.0",
                about="Chrome DevTools MCP server.",
            )
        }

    result = build_catalog(
        fetch_page=fetch_page_single,
        fetch_repo_meta=fetch_meta_lowercased,
        now=_NOW,
    )
    s = result["servers"][0]
    assert s["stars"] == 43982, f"Expected stars=43982, got {s['stars']} (lookup missed due to case mismatch)"
    assert s["official"] is True  # Organization + >1000 stars
