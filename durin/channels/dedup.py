"""Shared TTL-based inbound message deduplication.

Chat transports re-deliver: Slack Socket Mode replays events after
reconnects and on slow acks, and a reconnecting gateway connection can
replay its recent window. Every channel needs the same "have I seen this
id recently?" check; this helper centralizes it.

Not thread-safe: call it from the channel's event loop only. The cache
lives in memory: these transports re-deliver within a live connection or a
slow-ack window, never across a gateway restart.
"""

from __future__ import annotations

import time


class MessageDeduplicator:
    def __init__(self, max_size: int = 2000, ttl_seconds: float = 300.0) -> None:
        self._seen: dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

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
        return False

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if len(self._seen) > self._max_size:
            # All entries still fresh: keep the newest to enforce the cap.
            newest = sorted(self._seen.items(), key=lambda kv: kv[1])
            self._seen = dict(newest[-self._max_size :])
