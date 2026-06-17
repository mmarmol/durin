"""Refresh YARA rules from a pinned, integrity-checked feed (spec §4.f).

durin consumes a maintained feed; it does NOT author signatures. A failed or
unsafe update (fetch error, checksum mismatch, non-compiling rules) never
replaces the active rule set — the previous rules stay live.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(feed_url: str, feed_pin: str) -> bytes:
    """Download the pinned rule bundle via durin's SSRF-safe client. Raises on error."""
    import asyncio

    from durin.security.network import ssrf_safe_async_client

    url = feed_url if not feed_pin else f"{feed_url}@{feed_pin}"

    async def _go() -> bytes:
        async with ssrf_safe_async_client() as client:
            r = await client.get(url, timeout=20.0)
            r.raise_for_status()
            return r.content

    return asyncio.run(_go())


def _compiles(text: str) -> bool:
    try:
        import yara
        yara.compile(source=text)
        return True
    except Exception as exc:  # noqa: BLE001 — any compile failure rejects the update
        logger.warning("fetched YARA rules do not compile: %s", exc)
        return False


def refresh_rules(dest: Path, feed_url: str, feed_pin: str, sha256: str | None) -> bool:
    """Fetch, verify, compile-check, and atomically swap in a new rule bundle.

    Returns True on success; on ANY failure leaves the existing rules untouched
    and returns False.
    """
    dest = Path(dest)
    try:
        blob = _fetch(feed_url, feed_pin)
    except Exception as exc:  # noqa: BLE001 — network failure: keep current rules
        logger.warning("YARA feed fetch failed (keeping current rules): %s", exc)
        return False
    if sha256 and _sha256(blob) != sha256:
        logger.warning("YARA feed checksum mismatch (keeping current rules)")
        return False
    text = blob.decode("utf-8", errors="replace")
    if not _compiles(text):
        return False
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / ".incoming.yar"
    staging.write_text(text, encoding="utf-8")
    staging.replace(dest / "feed.yar")  # atomic swap of the feed file
    (dest / ".updated_at").write_text(str(int(time.time())), encoding="utf-8")
    return True


def is_stale(dest: Path, max_age_hours: int) -> bool:
    """True when the rule set is older than *max_age_hours* (0 disables refresh)."""
    if max_age_hours <= 0:
        return False
    marker = Path(dest) / ".updated_at"
    if not marker.is_file():
        return True
    try:
        age = time.time() - int(marker.read_text().strip())
    except (ValueError, OSError):
        return True
    return age > max_age_hours * 3600
