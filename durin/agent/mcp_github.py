"""GitHub augmentation for MCP discovery: resolve repos to stars/owner/topics,
classify first-party servers, and cache results. All network access is injectable
so unit tests run offline. GraphQL requires a token; without one, enrichment is a
no-op and the quality gate is disabled (see mcp_catalog_cache / search).
"""
from __future__ import annotations

import re
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
