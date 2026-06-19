"""Build the durin-owned MCP catalog (runs in CI weekly, not at client runtime).

``build_catalog`` is the testable core — all network seams are injected so tests
run offline. ``main()`` wires in the real registry + a pooled httpx client and
writes ``durin/agent/data/mcp_catalog.json``.

The catalog schema is the flat server row consumed by ``mcp_catalog_store`` (its
``_SIGNAL_KEYS`` select the enrichment into each ``McpServerHit.signals``), so the
store + search layers can consume it without transformation.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from durin.agent.mcp_github import GithubMeta, classify_official, parse_repo_url

_RETRY_ATTEMPTS = 4


def _with_retry(fn, *, attempts: int = _RETRY_ATTEMPTS, sleep=time.sleep):
    """Call fn(); on transient error retry with exponential backoff.

    Retries on httpx.TimeoutException, httpx.TransportError, and OSError.
    Raises the last exception after exhausting all attempts.
    """
    import httpx  # local import — not a new dep

    transient = (httpx.TimeoutException, httpx.TransportError, OSError)
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except transient as exc:
            last_exc = exc
            if attempt < attempts - 1:
                sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def _kind(server: dict) -> str:
    has_pkg = bool(server.get("packages"))
    has_remote = bool(server.get("remotes"))
    if has_pkg and has_remote:
        return "both"
    return "local" if has_pkg else "remote"


def _repo_url(server: dict) -> str:
    return (server.get("repository") or {}).get("url", "") or ""


def build_catalog(
    *,
    fetch_page,
    fetch_repo_meta,
    now: str,
    min_resolved_fraction: float = 0.0,
    sleep=time.sleep,
    fetch_verified=None,
) -> dict:
    """Paginate the registry, enrich with GitHub metadata, and return the catalog dict.

    Args:
        fetch_page: callable(*, cursor, updated_since) -> (list[server_dict], next_cursor|None).
                    Mirrors OfficialMcpRegistry.fetch_page (sync wrapper expected for CI script).
        fetch_repo_meta: callable(repo_keys) -> dict[tuple, GithubMeta]. Called ONCE with
                         all unique GitHub repo keys. The callable owns its own GitHub
                         token/HTTP client (closed over by main()) — build_catalog does NOT
                         pass a token (passing token="" here once silently disabled all star
                         enrichment; a token-ignoring test fake hid it).
        now: ISO-8601 timestamp string injected by the caller (tests supply a fixed value).
        min_resolved_fraction: If > 0.0, raise ValueError when the fraction of servers
                               (that have a github repo) whose stars is not None is below
                               this threshold. main() passes 0.8.
        sleep: injectable sleep callable for retry backoff (tests pass lambda _: None).

    Returns:
        {"schema_version": 1, "generated_at": now, "servers": [...]}
    """
    # --- Paginate all servers (with retry on transient errors) ---
    all_servers: list[dict] = []
    cursor = None
    while True:
        servers, cursor = _with_retry(
            lambda c=cursor: fetch_page(cursor=c, updated_since=None),
            sleep=sleep,
        )
        all_servers.extend(servers)
        if not cursor:
            break

    # --- GitHub-curated "verified" set: names + any servers only GitHub lists ---
    verified_servers: list[dict] = list(fetch_verified()) if fetch_verified else []
    verified_names = {s.get("name", "") for s in verified_servers if s.get("name")}
    official_names = {s.get("name", "") for s in all_servers}
    extra = [
        s for s in verified_servers
        if s.get("name") and s["name"] not in official_names
    ]
    combined = all_servers + extra

    # --- Collect unique GitHub repo keys (over official + verified-only) ---
    server_repo_keys: list[tuple[str, str] | None] = []
    unique_keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in combined:
        raw = parse_repo_url(_repo_url(s))
        key = (raw[0].lower(), raw[1].lower()) if raw else None
        server_repo_keys.append(key)
        if key and key not in seen:
            seen.add(key)
            unique_keys.append(key)

    # --- Enrich: one call over all unique keys ---
    meta_by_key: dict[tuple[str, str], GithubMeta] = {}
    if unique_keys:
        meta_by_key = fetch_repo_meta(unique_keys)

    # --- Build output rows ---
    rows: list[dict] = []
    for s, repo_key in zip(combined, server_repo_keys):
        meta: GithubMeta = meta_by_key.get(repo_key) if repo_key else None
        if meta is None:
            meta = GithubMeta(stars=None)

        official = classify_official(
            s.get("name", ""),
            owner_type=meta.owner_type,
            stars=meta.stars,
        )

        rows.append({
            "name": s.get("name", ""),
            "ref": s.get("name", ""),
            "description": s.get("description", ""),
            "kind": _kind(s),
            "stars": meta.stars,
            "owner_login": meta.owner_login,
            "owner_url": meta.owner_url,
            "owner_avatar": meta.owner_avatar,
            "topics": meta.topics,
            "language": meta.language,
            "license": meta.license,
            "official": official,
            "verified": s.get("name", "") in verified_names,
            "repo_url": _repo_url(s),
        })

    # --- Fail-loud guard: check resolution fraction for servers with a repo ---
    if min_resolved_fraction > 0.0:
        with_repo = [r for r, key in zip(rows, server_repo_keys) if key is not None]
        if with_repo:
            resolved = sum(1 for r in with_repo if r["stars"] is not None)
            fraction = resolved / len(with_repo)
            if fraction < min_resolved_fraction:
                raise ValueError(
                    f"Star resolution too low: {resolved}/{len(with_repo)} "
                    f"({fraction:.1%}) < required {min_resolved_fraction:.1%}. "
                    "Catalog not written — re-run after GitHub API recovers."
                )

    return {
        "schema_version": 1,
        "generated_at": now,
        "servers": rows,
    }


def main() -> None:
    """Entry point for CI: paginates, enriches pooled, writes mcp_catalog.json."""
    import asyncio
    import sys
    from datetime import datetime, timezone

    import httpx

    from durin.agent.mcp_github import _GQL, resolve_token
    from durin.agent.mcp_registry import GithubMcpRegistry, OfficialMcpRegistry

    token = resolve_token()
    if not token:
        print("No GitHub token found — enrichment will be empty.", file=sys.stderr)

    registry = OfficialMcpRegistry()
    gh_registry = GithubMcpRegistry()

    def sync_fetch_page(*, cursor=None, updated_since=None):
        return asyncio.run(registry.fetch_page(cursor=cursor, updated_since=updated_since))

    def sync_fetch_verified():
        """Crawl GitHub's curated registry → normalized server dicts (the verified tier)."""
        out: list = []
        cursor = None
        while True:
            servers, cursor = asyncio.run(gh_registry.fetch_page(cursor=cursor))
            out.extend(servers)
            if not cursor:
                break
        return out

    from durin.agent.mcp_github import fetch_repo_meta as _fetch_repo_meta

    # Pooled httpx client: one connection reused across all GraphQL batches.
    with httpx.Client(timeout=40.0) as pooled_client:

        def _raw_post(query: str, tok: str) -> dict:
            resp = pooled_client.post(
                _GQL,
                json={"query": query},
                headers={"Authorization": f"bearer {tok}", "User-Agent": "durin-mcp"},
            )
            resp.raise_for_status()
            return resp.json()

        def pooled_post(query: str, tok: str) -> dict:
            return _with_retry(lambda: _raw_post(query, tok))

        def enriching_fetch(repo_keys, *, token, post=None, batch=80):
            if not token:
                return {k: GithubMeta(stars=None) for k in repo_keys}
            return _fetch_repo_meta(repo_keys, token=token, post=pooled_post, batch=batch)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        catalog = build_catalog(
            fetch_page=sync_fetch_page,
            fetch_repo_meta=lambda keys: enriching_fetch(keys, token=token),
            now=now,
            min_resolved_fraction=0.8,
            fetch_verified=sync_fetch_verified,
        )

    out_path = Path(__file__).parent / "data" / "mcp_catalog.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"Wrote {len(catalog['servers'])} servers to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
