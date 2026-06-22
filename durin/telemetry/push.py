"""Opt-in HTTPS push of telemetry events.

When `telemetry.push_url` + `telemetry.push_token` are configured,
the sink buffers events and POSTs them to the endpoint in batches.
Local JSONL persistence runs UNCHANGED — push is an additional
sink, not a replacement. Disabled-by-default per spec.

The sink is intentionally simple: synchronous HTTP, buffer-and-drain
on every Nth event. A future revision can add an asyncio.Queue + a
flusher task for better latency; for now, the cost of one POST per
batch happens on the thread that emitted the Nth event. That's
fine for low-volume telemetry.

Failures (network down, 4xx, 5xx) put events BACK into the buffer
so the next drain retries. Caller has no fallible surface — the
sink swallows exceptions and reports via the local logger.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_BATCH_SIZE", "PushSink"]


DEFAULT_BATCH_SIZE: int = 10


class PushSink:
    """One push destination, fed by `log(event_type, data)` calls.

    Construct with a URL + token; disabled when either is missing.
    Thread-safety: not designed for concurrent callers from many
    threads. The agent loop is single-threaded for telemetry by
    convention.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._url = url
        self._token = token
        self._batch_size = batch_size
        self._buffer: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return bool(self._url) and bool(self._token)

    def pending_count(self) -> int:
        return len(self._buffer)

    def log(self, event_type: str, data: dict[str, Any]) -> None:
        """Enqueue one event. Drains when the buffer hits `batch_size`."""
        if not self.enabled:
            return
        self._buffer.append({"type": event_type, "data": data})
        if len(self._buffer) >= self._batch_size:
            self._drain()

    def flush(self) -> None:
        """Force a drain even if the batch isn't full. Useful on
        process shutdown so partial batches don't get dropped."""
        if not self.enabled or not self._buffer:
            return
        self._drain()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _drain(self) -> None:
        """Send the current buffer; on failure, restore it for retry."""
        batch = self._buffer[:]
        self._buffer.clear()
        payload = {"events": batch}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._post(self._url, json=payload, headers=headers)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telemetry push: POST to %s failed (%s); keeping %d "
                "events buffered for retry",
                self._url, exc, len(batch),
            )
            # Restore — preserving order — so the next drain retries.
            self._buffer = batch + self._buffer

    def _post(self, url: str, *, json: dict, headers: dict):  # noqa: A002
        """Default HTTP transport. Override in tests + replace with
        an async client if needed for higher-throughput backends.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx required for push sink") from exc
        return httpx.post(url, json=json, headers=headers, timeout=5.0)
