"""Shared TTL-based inbound message deduplication.

Chat transports re-deliver: Slack Socket Mode replays events after
reconnects, WeCom/DingTalk stream connections re-push on slow acks, Bot
Framework retries webhooks, and sync-cursor protocols (Matrix, Feishu)
replay the last window after a crash. Every channel needs the same "have
I seen this id recently?" check; this helper centralizes it.

Not thread-safe: call it from the channel's event loop only. Optional
persistence exists for transports that re-deliver across process
restarts; everything else should stay in-memory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class MessageDeduplicator:
    def __init__(
        self,
        max_size: int = 2000,
        ttl_seconds: float = 300.0,
        persist_path: Path | None = None,
    ) -> None:
        self._seen: dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._persist_path = persist_path
        if persist_path is not None:
            self._load()

    def is_duplicate(self, key: str) -> bool:
        """Record *key* and report whether it was already seen within the TTL."""
        if not key:
            return False
        now = time.time()
        stamp = self._seen.get(key)
        if stamp is not None and now - stamp < self._ttl:
            return True
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            self._prune(now)
        if self._persist_path is not None:
            self._save()
        return False

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if len(self._seen) > self._max_size:
            # All entries still fresh: keep the newest to enforce the cap.
            newest = sorted(self._seen.items(), key=lambda kv: kv[1])
            self._seen = dict(newest[-self._max_size :])

    def _load(self) -> None:
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            cutoff = time.time() - self._ttl
            self._seen = {
                str(k): float(v)
                for k, v in raw.items()
                if isinstance(v, (int, float)) and float(v) > cutoff
            }
        except (OSError, ValueError, AttributeError):
            self._seen = {}

    def _save(self) -> None:
        try:
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._seen), encoding="utf-8")
            os.replace(tmp, self._persist_path)
        except OSError:
            pass  # dedup persistence is best-effort; never break inbound flow
