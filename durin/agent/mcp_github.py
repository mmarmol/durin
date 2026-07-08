"""GitHub augmentation for MCP discovery: resolve repos to stars/owner/topics,
classify first-party servers, and cache results. All network access is injectable
so unit tests run offline. GraphQL requires a token; without one, enrichment is a
no-op and the quality gate is disabled (see mcp_catalog_store / search).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

_GH_RE = re.compile(r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/|$)")


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Return (owner, name) for a github.com URL, else None."""
    if not url:
        return None
    m = _GH_RE.search(url)
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if owner in (".", "..") or name in (".", ".."):
        return None
    return owner, name


def resolve_token(
    *, env: dict | None = None, gh_runner=None, secret_getter=None, secret_name: str = ""
) -> str | None:
    """Resolve a GitHub token via the shared resolver: gh CLI -> env -> the shared
    GITHUB_OAUTH secret -> the given per-feature secret. None if unavailable.

    Thin back-compat shim over ``durin.security.github_auth.resolve_github_token``
    so MCP discovery reads the same one credential as skills."""
    from durin.security.github_auth import resolve_github_token

    tok = resolve_github_token(
        env=env,
        gh_runner=gh_runner,
        secret_getter=secret_getter,
        legacy_secret_names=[secret_name] if secret_name else [],
    )
    return tok or None


_GQL = "https://api.github.com/graphql"


@dataclass
class GithubMeta:
    stars: int | None = None
    owner_login: str = ""
    owner_type: str = ""
    owner_url: str = ""
    owner_avatar: str = ""
    topics: list[str] = field(default_factory=list)
    language: str = ""
    license: str = ""
    about: str = ""


def _default_post(query: str, token: str) -> dict:
    import httpx

    with httpx.Client(timeout=40.0) as client:
        resp = client.post(
            _GQL,
            json={"query": query},
            headers={"Authorization": f"bearer {token}", "User-Agent": "durin-mcp"},
        )
        resp.raise_for_status()
        return resp.json()


def _repo_field(alias: str, owner: str, name: str) -> str:
    o = json.dumps(owner)
    n = json.dumps(name)
    return (
        f'{alias}: repository(owner: {o}, name: {n}) {{ stargazerCount '
        f'owner {{ __typename login url avatarUrl }} '
        f'repositoryTopics(first: 6) {{ nodes {{ topic {{ name }} }} }} '
        f'primaryLanguage {{ name }} licenseInfo {{ spdxId }} description }}'
    )


def _parse_node(node: dict) -> GithubMeta:
    owner = node.get("owner") or {}
    topics = [
        (t.get("topic") or {}).get("name", "")
        for t in ((node.get("repositoryTopics") or {}).get("nodes") or [])
    ]
    return GithubMeta(
        stars=node.get("stargazerCount", 0),
        owner_login=owner.get("login", ""),
        owner_type=owner.get("__typename", ""),
        owner_url=owner.get("url", ""),
        owner_avatar=owner.get("avatarUrl", ""),
        topics=[t for t in topics if t],
        language=(node.get("primaryLanguage") or {}).get("name", ""),
        license=(node.get("licenseInfo") or {}).get("spdxId", "") or "",
        about=node.get("description") or "",
    )


def fetch_repo_meta(
    repo_keys: list[tuple[str, str]], *, token: str, post=None, batch: int = 80,
    pace: float = 0.0, sleep=time.sleep,
) -> dict[tuple[str, str], GithubMeta]:
    """Resolve GitHub metadata for repos via batched GraphQL. Missing repos get
    GithubMeta(stars=None). Keys in the returned dict are lowercased.

    ``pace`` is the number of seconds to sleep *between* batches (never before the
    first) so a large crawl stays under GitHub's secondary rate limits, which
    trigger on bursts of unspaced requests. Defaults to 0.0 (no pacing)."""
    post = _default_post if post is None else post
    out: dict[tuple[str, str], GithubMeta] = {}
    for batch_idx, i in enumerate(range(0, len(repo_keys), batch)):
        if batch_idx and pace:
            sleep(pace)
        chunk = repo_keys[i : i + batch]
        query = "query {\n" + "\n".join(
            _repo_field(f"r{j}", o, n) for j, (o, n) in enumerate(chunk)
        ) + "\n}"
        try:
            data = (post(query, token) or {}).get("data") or {}
        except Exception:  # noqa: BLE001 — degrade: leave this batch unresolved
            data = {}
        for j, (o, n) in enumerate(chunk):
            node = data.get(f"r{j}")
            out[(o.lower(), n.lower())] = _parse_node(node) if node else GithubMeta(stars=None)
    return out


REFERENCE_NAMESPACE = "io.modelcontextprotocol"
REHOSTER_NAMESPACES = {"ai.smithery", "com.mcparmory", "eu.ansvar", "io.github.mcp-dir"}


def classify_official(
    name: str, *, owner_type: str, stars: int | None, denylist: set[str] | None = None
) -> bool:
    """First-party heuristic for the 'Official' badge / gate."""
    denylist = REHOSTER_NAMESPACES if denylist is None else denylist
    namespace = name.split("/", 1)[0]
    if namespace in denylist:
        return False
    if namespace == REFERENCE_NAMESPACE:
        return True
    if not namespace.startswith("io.github.") and "." in namespace:
        return True  # DNS-verified vendor domain (com.stripe, etc.)
    if owner_type == "Organization" and (stars or 0) > 1000:
        return True
    return False
