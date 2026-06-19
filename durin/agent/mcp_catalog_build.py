"""Build the durin-owned MCP catalog (runs in CI weekly, not at client runtime).

``build_catalog`` is the testable core — all network seams are injected so tests
run offline. ``main()`` wires in the real registry + a pooled httpx client and
writes ``durin/agent/data/mcp_catalog.json``.

The catalog schema mirrors the server rows used by mcp_catalog_cache / _hit_from_server,
so the store + search layers can consume it without transformation.
"""
from __future__ import annotations

import json
from pathlib import Path

from durin.agent.mcp_github import GithubMeta, classify_official, parse_repo_url


def _kind(server: dict) -> str:
    has_pkg = bool(server.get("packages"))
    has_remote = bool(server.get("remotes"))
    if has_pkg and has_remote:
        return "both"
    return "local" if has_pkg else "remote"


def _repo_url(server: dict) -> str:
    return (server.get("repository") or {}).get("url", "") or ""


def build_catalog(*, fetch_page, fetch_repo_meta, now: str) -> dict:
    """Paginate the registry, enrich with GitHub metadata, and return the catalog dict.

    Args:
        fetch_page: callable(*, cursor, updated_since) -> (list[server_dict], next_cursor|None).
                    Mirrors OfficialMcpRegistry.fetch_page (sync wrapper expected for CI script).
        fetch_repo_meta: callable(repo_keys, *, token, post, batch) -> dict[tuple, GithubMeta].
                         Called ONCE with all unique GitHub repo keys.
        now: ISO-8601 timestamp string injected by the caller (tests supply a fixed value).

    Returns:
        {"schema_version": 1, "generated_at": now, "servers": [...]}
    """
    # --- Paginate all servers ---
    all_servers: list[dict] = []
    cursor = None
    while True:
        servers, cursor = fetch_page(cursor=cursor, updated_since=None)
        all_servers.extend(servers)
        if not cursor:
            break

    # --- Collect unique GitHub repo keys ---
    server_repo_keys: list[tuple[str, str] | None] = []
    unique_keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in all_servers:
        key = parse_repo_url(_repo_url(s))
        server_repo_keys.append(key)
        if key and key not in seen:
            seen.add(key)
            unique_keys.append(key)

    # --- Enrich: one call over all unique keys ---
    meta_by_key: dict[tuple[str, str], GithubMeta] = {}
    if unique_keys:
        meta_by_key = fetch_repo_meta(unique_keys, token="")

    # --- Build output rows ---
    rows: list[dict] = []
    for s, repo_key in zip(all_servers, server_repo_keys):
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
            "repo_url": _repo_url(s),
        })

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
    from durin.agent.mcp_registry import OfficialMcpRegistry

    token = resolve_token()
    if not token:
        print("No GitHub token found — enrichment will be empty.", file=sys.stderr)

    registry = OfficialMcpRegistry()

    def sync_fetch_page(*, cursor=None, updated_since=None):
        return asyncio.run(registry.fetch_page(cursor=cursor, updated_since=updated_since))

    from durin.agent.mcp_github import fetch_repo_meta as _fetch_repo_meta

    # Pooled httpx client: one connection reused across all GraphQL batches.
    with httpx.Client(timeout=40.0) as pooled_client:

        def pooled_post(query: str, tok: str) -> dict:
            resp = pooled_client.post(
                _GQL,
                json={"query": query},
                headers={"Authorization": f"bearer {tok}", "User-Agent": "durin-mcp"},
            )
            resp.raise_for_status()
            return resp.json()

        def enriching_fetch(repo_keys, *, token, post=None, batch=80):
            if not token:
                return {k: GithubMeta(stars=None) for k in repo_keys}
            return _fetch_repo_meta(repo_keys, token=token, post=pooled_post, batch=batch)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        catalog = build_catalog(
            fetch_page=sync_fetch_page,
            fetch_repo_meta=lambda keys, *, token=token, **kw: enriching_fetch(keys, token=token, **kw),
            now=now,
        )

    out_path = Path(__file__).parent / "data" / "mcp_catalog.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"Wrote {len(catalog['servers'])} servers to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
