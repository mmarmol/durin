"""Shared OSV malware lookup (MAL-* advisories).

Used by both the MCP spawn-command preflight
(``durin/agent/tools/mcp_security.py``) and skill install-spec scanning
(``durin/security/skill_scan.py``). Transport errors propagate so callers can
fail-open; a clean query returns ``[]``.
"""
from __future__ import annotations

import json
import urllib.request

_OSV_ENDPOINT = "https://api.osv.dev/v1/query"
_OSV_TIMEOUT = 3  # seconds; tight — fail-open on slow infra


def _post_query(payload: dict, timeout: int) -> dict:
    """POST the query to OSV and return the parsed JSON. Raises on any error."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _OSV_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "durin-osv-preflight/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def query_malware(package: str, ecosystem: str, version: str | None = None) -> list[str]:
    """Return MAL-* advisory IDs for *package* in *ecosystem* (empty = clean).

    Raises on transport error so callers can decide their fail-open policy.
    """
    payload: dict = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    result = _post_query(payload, _OSV_TIMEOUT)
    vulns = result.get("vulns", []) or []
    return [v["id"] for v in vulns if str(v.get("id", "")).startswith("MAL-")]
