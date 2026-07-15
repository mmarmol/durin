"""Daily models.dev refresh → user-cache overlay for the per-provider catalog.

The vendored ``provider_models.json`` is the offline floor; this writes a
fresher ``provider_models_cache.json`` under the data dir that
``provider_catalog`` overlays on top. Any fetch/parse failure is swallowed so a
network blip never breaks the picker (the prior cache / vendored floor stays).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

from durin.providers.models_dev import (
    MODELS_DEV_URL,
    apply_nvidia_live_ids,
    build_provider_models,
    fetch_nvidia_model_ids,
)
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "durin"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def refresh_provider_models_cache(data_dir: Path) -> bool:
    """Fetch models.dev → write ``provider_models_cache.json``. Returns False
    (keeping the prior cache / vendored floor) on any fetch/parse failure."""
    from durin.config.schema import ProvidersConfig

    try:
        data = json.loads(_default_fetch(MODELS_DEV_URL).decode("utf-8"))
    except Exception:  # noqa: BLE001 — network/parse: keep prior data
        return False
    index = build_provider_models(data, set(ProvidersConfig.model_fields))
    if not index:
        return False
    # NVIDIA: ids come from the provider's own public /v1/models (models.dev
    # drifts and re-spells them); models.dev only contributes capability
    # metadata. If NVIDIA is unreachable, drop the provider from this cache so
    # the overlay falls through to the vendored floor (already ground-truthed
    # at build time) instead of resurrecting models.dev's drifted list.
    live_ids = fetch_nvidia_model_ids()
    if live_ids:
        index["nvidia"] = apply_nvidia_live_ids(index.get("nvidia") or [], live_ids)
    else:
        index.pop("nvidia", None)
    cache_path = data_dir / "provider_models_cache.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(cache_path):
        atomic_write_text(
            cache_path,
            json.dumps({"schema_version": 1, "providers": index}, ensure_ascii=False),
        )
    from durin.providers import provider_catalog

    provider_catalog._load_index.cache_clear()
    return True


class CatalogRefreshScheduler:
    """Daily background refresh. Mirrors the daemon-thread start/stop pattern of
    ``durin/memory/health_check.py::HealthCheckScheduler``: a single daemon
    thread refreshes when the cache is due, then waits ``interval_hours``
    between runs.

    The due time is derived from the cache file's mtime, so it SURVIVES
    process restarts: a missing or overdue cache is refreshed immediately (in
    the background thread — startup itself never blocks on network), a fresh
    one waits out only the remaining time. A wait-first design would restart
    the full interval on every boot, and a deployment restarted more often
    than the interval would never refresh at all.
    """

    def __init__(self, data_dir: Path, interval_hours: int = 24) -> None:
        self._data_dir = data_dir
        self._interval = max(1, interval_hours) * 3600
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="catalog-refresh", daemon=True
        )
        self._thread.start()

    def _initial_wait(self) -> float:
        """Seconds until the cache is due — 0.0 when missing or overdue."""
        try:
            mtime = (self._data_dir / "provider_models_cache.json").stat().st_mtime
        except OSError:
            return 0.0
        return max(0.0, mtime + self._interval - time.time())

    def _run(self) -> None:
        # First wait = time remaining until due (0 → refresh right away); after
        # each attempt, a full interval. ``wait`` returns True the instant
        # ``stop()`` fires, so shutdown is immediate.
        wait = self._initial_wait()
        while not self._stop.wait(wait):
            try:
                refresh_provider_models_cache(self._data_dir)
            except Exception:  # noqa: BLE001
                pass
            wait = self._interval

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        self._thread = None
