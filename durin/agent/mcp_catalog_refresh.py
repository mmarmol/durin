"""Weekly MCP catalog refresh → user-cache overlay.

The vendored ``mcp_catalog.json`` is the offline floor — a trimmed quality
tier of the full catalog, so day-1 searches work without any network. This
module writes the FULL ``mcp_catalog_cache.json`` under the data dir that
``mcp_catalog_store`` overlays on top. Any fetch/parse failure is swallowed
so a network blip never breaks discovery (the prior cache / vendored floor
stays).

The overlay file's mtime records the last successful check, so the schedule
survives process restarts: the scheduler's first wait is the time REMAINING
until the overlay is due (zero when it is missing or overdue), not a fresh
full interval.

Mirrors ``durin/providers/catalog_refresh.py`` — structure and idioms are
intentionally identical; only names and the newer-than guard differ.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

# The durin-owned catalog, published weekly as a release asset (see
# .github/workflows/mcp-catalog.yml). Mirrors McpCatalogRefreshConfig.url — callers
# normally pass cfg.url; this default exists only so a bare call still targets the
# right artifact (NOT the upstream registry, whose schema lacks stars/official).
_DEFAULT_URL = "https://github.com/mmarmol/durin/releases/download/catalog/mcp_catalog.json"


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "durin"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _current_generated_at(data_dir: Path) -> str:
    """Return the best available local generated_at for comparison.

    Checks the overlay first; falls back to the vendored floor.
    Returns "" if neither can be read.
    """
    overlay = data_dir / "mcp_catalog_cache.json"
    if overlay.exists():
        try:
            raw = json.loads(overlay.read_text(encoding="utf-8"))
            ts = raw.get("generated_at", "")
            if ts:
                return ts
        except Exception:  # noqa: BLE001
            pass

    # Fall back to vendored floor
    try:
        from durin.agent.mcp_catalog_store import _FLOOR

        raw = json.loads(_FLOOR.read_text(encoding="utf-8"))
        return raw.get("generated_at", "")
    except Exception:  # noqa: BLE001
        return ""


def refresh_catalog(data_dir: Path, *, url: str = _DEFAULT_URL, fetch=None) -> bool:
    """Fetch the remote MCP catalog → write ``mcp_catalog_cache.json``.

    Writes the overlay **only** when the remote ``generated_at`` is strictly
    newer than the current local copy (lexicographic ISO-Z string compare).
    Returns False (keeping the prior cache / vendored floor) on any
    fetch/parse/IO failure — mirrors catalog_refresh.py swallow pattern.

    Parameters
    ----------
    data_dir:
        Directory where the overlay ``mcp_catalog_cache.json`` is written.
    url:
        Remote catalog JSON URL.
    fetch:
        Injectable ``fetch(url) -> bytes | str`` callable. Defaults to a
        ``urllib.request.urlopen`` call with a 30-second timeout and a
        ``User-Agent: durin`` header.
    """
    if fetch is None:
        fetch = _default_fetch

    try:
        raw = fetch(url)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — network/decode/parse: keep prior data
        return False

    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        return False

    remote_ts: str = data.get("generated_at", "")
    local_ts: str = _current_generated_at(data_dir)
    cache_path = data_dir / "mcp_catalog_cache.json"
    overlay_exists = cache_path.exists()

    # Newer-than guard. When the overlay exists, only a strictly newer remote
    # is worth a write — but a successful check still bumps the mtime so the
    # scheduler's due time (derived from it) does not refetch on every
    # restart. When the overlay is MISSING, an equal timestamp must still
    # write: the vendored floor is a trimmed quality tier, so equal
    # generated_at does not mean equal content.
    if not remote_ts or remote_ts < local_ts or (overlay_exists and remote_ts == local_ts):
        if overlay_exists and remote_ts and remote_ts >= local_ts:
            try:
                os.utime(cache_path)
            except OSError:
                pass
        return False

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(cache_path):
            atomic_write_text(cache_path, json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — IO failure: keep prior data
        return False

    from durin.agent import mcp_catalog_store

    mcp_catalog_store.cache_clear()
    return True


class McpCatalogRefreshScheduler:
    """Weekly background refresh of the MCP server catalog.

    Mirrors ``CatalogRefreshScheduler`` from ``durin/providers/catalog_refresh.py``:
    a single daemon thread refreshes when the overlay is due, then waits
    ``interval_hours`` between runs, until ``stop()`` is called.

    The due time is derived from the overlay file's mtime, so it SURVIVES
    process restarts: a missing or overdue overlay is fetched immediately (in
    the background thread — startup itself never blocks on network), a fresh
    one waits out only the remaining time. A wait-first design would restart
    the full interval on every boot, and a deployment restarted more often
    than the interval would never refresh at all.
    """

    def __init__(
        self,
        data_dir: Path,
        url: str = _DEFAULT_URL,
        interval_hours: int = 168,
        fetch=None,
    ) -> None:
        self._data_dir = data_dir
        self._url = url
        self._interval = max(1, interval_hours) * 3600
        self._fetch = fetch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="mcp-catalog-refresh", daemon=True
        )
        self._thread.start()

    def _initial_wait(self) -> float:
        """Seconds until the overlay is due — 0.0 when missing or overdue."""
        try:
            mtime = (self._data_dir / "mcp_catalog_cache.json").stat().st_mtime
        except OSError:
            return 0.0
        return max(0.0, mtime + self._interval - time.time())

    def _run(self) -> None:
        # First wait = time remaining until due (0 → fetch right away); after
        # each attempt, a full interval. ``wait`` returns True the instant
        # ``stop()`` fires → immediate shutdown.
        wait = self._initial_wait()
        while not self._stop.wait(wait):
            try:
                refresh_catalog(self._data_dir, url=self._url, fetch=self._fetch)
            except Exception:  # noqa: BLE001
                pass
            wait = self._interval

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        self._thread = None
