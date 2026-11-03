"""Per-thread state the Slack channel keeps across gateway restarts.

Two facts about a thread outlive the process that learned them:

- ``context_known`` — the thread's history already reached its session (it
  was fetched once, or the session started with the thread's first
  message). Forgetting it made the next message after a restart fetch and
  prepend the whole thread again, repeating turns the session held.
- ``followed`` — the bot was mentioned in the thread, so replies there are
  answered without a new mention. Forgetting it dropped those replies until
  someone mentioned the bot again.

One JSON file, one writer (the channel inside the gateway process), atomic
writes. Bounded: past ``max_entries`` the least recently marked threads are
forgotten, never the whole store at once. Losing the file only brings back
the old behaviour for the threads it held.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from loguru import logger

from durin.utils.atomic_write import atomic_write_text


class SlackThreadMemory:
    def __init__(self, path: Path | Callable[[], Path], *, max_entries: int) -> None:
        # A callable defers resolving the instance's data directory until the
        # store is first used, so building a channel creates no folders.
        self._path_source = path
        self._max_entries = max_entries
        self._threads: dict[str, dict] | None = None

    def _path(self) -> Path:
        src = self._path_source
        return src() if callable(src) else src

    def _load(self) -> dict[str, dict]:
        if self._threads is None:
            self._threads = {}
            try:
                path = self._path()
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._threads = {
                            k: v for k, v in data.items()
                            if isinstance(v, dict) and isinstance(v.get("flags"), list)
                        }
            except Exception as exc:  # noqa: BLE001 — a bad file starts empty
                logger.warning("Slack thread state unreadable, starting empty: {}", exc)
        return self._threads

    def has(self, flag: str, key: str) -> bool:
        entry = self._load().get(key)
        return entry is not None and flag in entry["flags"]

    def add(self, flag: str, key: str) -> None:
        threads = self._load()
        entry = threads.pop(key, None) or {"flags": []}
        if flag not in entry["flags"]:
            entry["flags"].append(flag)
        entry["at"] = time.time()
        threads[key] = entry  # most recently marked last
        while len(threads) > self._max_entries:
            threads.pop(next(iter(threads)))
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(threads))
        except OSError as exc:
            logger.warning("Slack thread state not saved: {}", exc)
