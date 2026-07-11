"""Claims index: correlate incoming threads to loop runs via email digest keys.

Single JSON file at <workspace>/loops/claims.json, atomically rewritten under
cross_process_lock() so gateway, TUI, and cron processes can register and
release claims concurrently without colliding on same keys.

Policy: last claim on a key wins; a concurrent claimant on the same thread
silently clobbers the prior one, and that clobbering is logged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock


def claims_path(ws: str | Path) -> Path:
    """Return the path to the claims index file."""
    return Path(ws) / "loops" / "claims.json"


def _load_claims(ws: str | Path) -> dict:
    """Load claims from file, returning empty dict if missing or malformed.

    Tolerates: invalid JSON, non-UTF-8 bytes, top-level non-dict, and entries
    with non-dict values. Drops malformed entries and always returns a dict.
    """
    path = claims_path(ws)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Any read/parse error: skip and return empty dict
        return {}

    # Ensure top level is a dict
    if not isinstance(data, dict):
        return {}

    # Filter: keep only entries with dict values
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def lookup(ws: str | Path, key: str) -> dict | None:
    """Look up a claim by key (lock-free read).

    Returns dict with "loop", "run_id", "registered_at" or None if not found.
    """
    claims = _load_claims(ws)
    return claims.get(key)


def register(ws: str | Path, *, key: str, loop: str, run_id: str) -> None:
    """Register a claim with current timestamp.

    Overwrites existing claim if key already exists.
    """
    path = claims_path(ws)
    with cross_process_lock(path):
        claims = _load_claims(ws)
        existing = claims.get(key)
        if existing and (existing.get("loop") != loop or existing.get("run_id") != run_id):
            logger.warning(
                "loops: claim on key '{}' clobbered — {}/{} overwrites {}/{}",
                key, loop, run_id, existing.get("loop"), existing.get("run_id"),
            )
        claims[key] = {
            "loop": loop,
            "run_id": run_id,
            "registered_at": time.time(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(claims, indent=2))


def release(ws: str | Path, key: str) -> None:
    """Release (remove) a claim by key.

    Idempotent: does nothing if key doesn't exist.
    """
    path = claims_path(ws)
    with cross_process_lock(path):
        claims = _load_claims(ws)
        claims.pop(key, None)
        if claims:  # Only write if there are remaining claims
            atomic_write_text(path, json.dumps(claims, indent=2))
        elif path.exists():  # Remove file if empty
            path.unlink()


def release_run(ws: str | Path, loop: str, run_id: str) -> None:
    """Release all claims held by a run.

    Removes all claims where loop matches and run_id matches.
    Idempotent: does nothing if no matching claims exist.
    """
    path = claims_path(ws)
    with cross_process_lock(path):
        claims = _load_claims(ws)
        # Filter out claims for this loop+run_id
        claims = {
            k: v
            for k, v in claims.items()
            if not (v.get("loop") == loop and v.get("run_id") == run_id)
        }
        if claims:
            atomic_write_text(path, json.dumps(claims, indent=2))
        elif path.exists():
            path.unlink()


def prune(ws: str | Path, max_age_s: int) -> list[str]:
    """Expire stale claims older than max_age_s seconds.

    Returns list of released (expired) keys.
    """
    path = claims_path(ws)
    now = time.time()
    released: list[str] = []

    with cross_process_lock(path):
        claims = _load_claims(ws)
        for key, claim in list(claims.items()):
            registered_at = claim.get("registered_at", 0)
            age = now - registered_at
            if age > max_age_s:
                del claims[key]
                released.append(key)

        if claims:
            atomic_write_text(path, json.dumps(claims, indent=2))
        elif path.exists():
            path.unlink()

    return released
