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


def _fake_fetch_repo_meta(repo_keys):
    # The real seam closes over its own token/HTTP client; build_catalog calls it with
    # ONLY the keys (it must NOT pass a token — doing so once disabled all enrichment).
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

    def tracking_fetch(repo_keys):
        calls.append(list(repo_keys))
        return _fake_fetch_repo_meta(repo_keys)

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
    def fetch_meta_lowercased(repo_keys):
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


# ---------------------------------------------------------------------------
# Retry + fail-loud tests
# ---------------------------------------------------------------------------

def test_pagination_retries_transient():
    """A single ReadTimeout on fetch_page is retried; build_catalog completes."""
    import httpx

    call_count = 0

    def flaky_fetch_page(*, cursor=None, updated_since=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadTimeout("transient timeout")
        # Second call succeeds with a single-page result
        return ([_SERVER_WITH_REPO], None)

    result = build_catalog(
        fetch_page=flaky_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
        sleep=lambda _: None,
    )
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == _SERVER_WITH_REPO["name"]
    assert call_count == 2  # one failure + one success


def test_pagination_gives_up_after_attempts():
    """fetch_page always raising exhausts all retry attempts and re-raises."""
    import httpx

    call_count = 0

    def always_fail(*, cursor=None, updated_since=None):
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("always fails")

    try:
        build_catalog(
            fetch_page=always_fail,
            fetch_repo_meta=_fake_fetch_repo_meta,
            now=_NOW,
            sleep=lambda _: None,
        )
        assert False, "Expected an exception"
    except httpx.ReadTimeout:
        pass

    assert call_count == 4, f"Expected 4 attempts (default), got {call_count}"


# ---------------------------------------------------------------------------
# HTTP-error retry (rate limits / 5xx) — the GraphQL enrichment path
# ---------------------------------------------------------------------------

def _http_error(status: int, headers: dict | None = None):
    import httpx

    req = httpx.Request("POST", "https://api.github.com/graphql")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def test_with_retry_retries_5xx_then_succeeds():
    """A transient HTTP 502 is retried; the call eventually succeeds."""
    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(502)
        return {"ok": True}

    slept: list[float] = []
    out = _with_retry(fn, sleep=slept.append)
    assert out == {"ok": True}
    assert calls["n"] == 3
    assert len(slept) == 2  # backoff before each retry


def test_with_retry_honors_retry_after_on_429():
    """HTTP 429 with Retry-After is retried after exactly that many seconds."""
    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "7"})
        return "ok"

    slept: list[float] = []
    out = _with_retry(fn, sleep=slept.append)
    assert out == "ok"
    assert slept == [7.0]


def test_with_retry_retries_secondary_rate_limit_403():
    """A secondary-rate-limit 403 (x-ratelimit-remaining: 0) is retried."""
    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(403, {"x-ratelimit-remaining": "0", "Retry-After": "3"})
        return "ok"

    slept: list[float] = []
    out = _with_retry(fn, sleep=slept.append)
    assert out == "ok"
    assert calls["n"] == 2
    assert slept == [3.0]


def test_with_retry_does_not_retry_auth_403():
    """A plain 403 (no rate-limit signal) is a hard auth error — not retried."""
    import pytest

    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_error(403)  # no Retry-After / x-ratelimit-remaining

    with pytest.raises(Exception):  # noqa: B017 — httpx.HTTPStatusError
        _with_retry(fn, sleep=lambda _: None)
    assert calls["n"] == 1


def test_with_retry_does_not_retry_404():
    """A non-retryable 4xx (404) re-raises immediately."""
    import pytest

    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_error(404)

    with pytest.raises(Exception):  # noqa: B017 — httpx.HTTPStatusError
        _with_retry(fn, sleep=lambda _: None)
    assert calls["n"] == 1


def test_with_retry_gives_up_on_persistent_5xx():
    """Persistent 503 exhausts all attempts and re-raises."""
    import pytest

    from durin.agent.mcp_catalog_build import _with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_error(503)

    with pytest.raises(Exception):  # noqa: B017 — httpx.HTTPStatusError
        _with_retry(fn, sleep=lambda _: None)
    assert calls["n"] == 4  # default attempts


def test_min_resolved_fraction_guard():
    """build_catalog raises ValueError when stars resolution is below the threshold."""
    # fetch_repo_meta returns stars=None for all repos — simulates a failed GraphQL batch
    def no_stars_meta(repo_keys):
        return {k: GithubMeta(stars=None) for k in repo_keys}

    # Two servers that both have repos → 0% resolved → should raise
    def two_repo_page(*, cursor=None, updated_since=None):
        return ([_SERVER_WITH_REPO, _SERVER_BOTH], None)

    import pytest
    with pytest.raises(ValueError, match="resolution"):
        build_catalog(
            fetch_page=two_repo_page,
            fetch_repo_meta=no_stars_meta,
            now=_NOW,
            min_resolved_fraction=0.8,
            sleep=lambda _: None,
        )

    # With real stars it should NOT raise
    result = build_catalog(
        fetch_page=two_repo_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
        min_resolved_fraction=0.8,
        sleep=lambda _: None,
    )
    assert len(result["servers"]) == 2


# ---------------------------------------------------------------------------
# Verified tier merge (GitHub-curated set)
# ---------------------------------------------------------------------------

def test_verified_flag_and_github_only_servers_merged():
    """fetch_verified flags matching official rows AND adds servers only GitHub lists."""
    def _verified():
        return [
            # already in the official fake page (github-mcp-server) → should be flagged
            {"name": "io.github.github/github-mcp-server", "description": "GitHub MCP Server",
             "packages": [{"registryType": "oci", "identifier": "ghcr.io/x:1"}], "remotes": [],
             "repository": {"url": "https://github.com/github/github-mcp-server"}},
            # NOT in the official page → should be appended as a new verified row
            {"name": "com.figma.mcp/mcp", "description": "Figma MCP", "packages": [],
             "remotes": [{"type": "streamable-http", "url": "https://figma.example/mcp"}],
             "repository": {"url": "https://github.com/figma/mcp"}},
        ]

    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=_fake_fetch_repo_meta,
        now=_NOW,
        fetch_verified=_verified,
    )
    by_name = {s["name"]: s for s in result["servers"]}
    assert by_name["io.github.github/github-mcp-server"]["verified"] is True
    assert "com.figma.mcp/mcp" in by_name           # github-only server added
    assert by_name["com.figma.mcp/mcp"]["verified"] is True
    # a server NOT in the verified set stays unverified
    assert by_name["com.stripe/agent-toolkit"]["verified"] is False


def test_no_fetch_verified_leaves_all_unverified():
    result = build_catalog(
        fetch_page=_fake_fetch_page, fetch_repo_meta=_fake_fetch_repo_meta, now=_NOW)
    assert all(s["verified"] is False for s in result["servers"])


def test_build_catalog_calls_fetch_repo_meta_with_keys_only():
    """Regression: build_catalog must call fetch_repo_meta(keys) and NOT inject a token —
    passing token="" once silently disabled all star enrichment (rows came back stars=None).
    A token-respecting seam (like main()'s real closure) proves stars actually land."""
    calls = {}

    def token_respecting_fetch(repo_keys):
        # Mimics main()'s closure: it has a (captured) token, so it returns real stars.
        # If build_catalog ever passes extra args, this 1-arg signature raises TypeError.
        calls["keys"] = list(repo_keys)
        return {k: GithubMeta(stars=4242) for k in repo_keys}

    result = build_catalog(
        fetch_page=_fake_fetch_page,
        fetch_repo_meta=token_respecting_fetch,
        now=_NOW,
    )
    enriched = [s for s in result["servers"] if s["repo_url"]]
    assert enriched, "fixture must include servers with a github repo"
    assert all(s["stars"] == 4242 for s in enriched)  # enrichment actually applied
    assert calls["keys"]  # fetch_repo_meta was invoked with the repo keys


# ---------------------------------------------------------------------------
# trim_to_quality_tier: the vendored-floor subset
# ---------------------------------------------------------------------------


def test_trim_to_quality_tier_keeps_default_gate_servers():
    """The floor keeps exactly the servers the default search gate can show
    (curated OR popular) and preserves the catalog envelope."""
    from durin.agent.mcp_catalog_build import trim_to_quality_tier

    catalog = {
        "schema_version": 1,
        "generated_at": "2026-07-13T08:43:19Z",
        "servers": [
            {"ref": "a/curated", "verified": True, "stars": 3},
            {"ref": "b/popular", "verified": False, "stars": 5000},
            {"ref": "c/obscure", "verified": False, "stars": 12},
            {"ref": "d/no-stars"},
        ],
    }

    trimmed = trim_to_quality_tier(catalog, min_stars=100)

    refs = [s["ref"] for s in trimmed["servers"]]
    assert refs == ["a/curated", "b/popular"]
    assert trimmed["generated_at"] == "2026-07-13T08:43:19Z"
    assert trimmed["schema_version"] == 1
    # The input catalog is not mutated.
    assert len(catalog["servers"]) == 4


def test_trim_to_quality_tier_matches_store_gate():
    """The trim predicate IS the store's default gate — a server kept by the
    floor must be shown by search's default quality filter and vice versa."""
    from durin.agent.mcp_catalog_build import trim_to_quality_tier
    from durin.agent.mcp_catalog_store import _curated_or_popular

    servers = [
        {"ref": "x/one", "verified": False, "stars": 100},   # boundary: NOT > 100
        {"ref": "x/two", "verified": False, "stars": 101},
        {"ref": "x/three", "verified": True, "stars": 0},
    ]
    trimmed = trim_to_quality_tier({"generated_at": "t", "servers": servers}, min_stars=100)
    kept = {s["ref"] for s in trimmed["servers"]}
    expected = {s["ref"] for s in servers if _curated_or_popular(s, 100)}
    assert kept == expected
