"""Daily models.dev refresh → user-cache overlay for the per-provider catalog.

The vendored ``provider_models.json`` is the offline floor; this writes a
fresher ``provider_models_cache.json`` under the data dir that
``provider_catalog`` overlays on top. Any fetch/parse failure is swallowed so a
network blip never breaks the picker (the prior cache / vendored floor stays).
"""

from __future__ import annotations

import json
import threading
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


def refresh_provider_models_cache(data_dir: Path) -> bool:
    """Fetch models.dev → write ``provider_models_cache.json``. Returns False
    (keeping the prior cache / vendored floor) on any fetch/parse failure."""
    from durin.config.schema import ProvidersConfig

    try:
        req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "durin"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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
    thread refreshes once on start, then waits ``interval_hours`` between runs.
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

    def _run(self) -> None:
        # Wait first, THEN refresh: the vendored floor is the day-1 data, so this
        # keeps process (and test) startup free of any network call. ``wait``
        # returns True the instant ``stop()`` fires, so shutdown is immediate.
        while not self._stop.wait(self._interval):
            try:
                refresh_provider_models_cache(self._data_dir)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        self._thread = None
