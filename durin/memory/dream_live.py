"""Live tee: forward dream activity to a publish callback as it happens.

A :class:`DreamProgressSink` is registered as an extra sink on the cron dream's
``TelemetryLogger`` (see ``durin.telemetry.logger``). For every dream/absorb
*activity* event the dream emits, it maps the event with the shared digest
mapping and hands the resulting item(s) to an injected ``publish`` callback —
the same items the after-the-fact digest would show, only in real time.

The sink is deliberately transport-agnostic: it knows nothing about websockets.
The caller injects a ``publish`` that delivers the payload (the cron handler
injects one that hops onto the event loop and fans out over the websocket).
Because the dream passes run in worker threads, ``publish`` is responsible for
any thread-safety it needs; the sink itself is pure mapping.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from durin.memory.dream_digest import DREAM_ACTIVITY_TYPES, map_dream_event


class DreamProgressSink:
    """Telemetry sink that maps activity events and forwards them live.

    ``publish`` receives ``{"kind": "activity", "item": <item dict>}`` for each
    mapped item (the key is ``item``, not ``event``, so it never collides with
    the websocket frame's own ``event`` discriminator). Run-boundary markers
    (start/end) are NOT forwarded here — the cron handler emits explicit
    ``run_started`` / ``run_finished`` frames so the UI's running indicator does
    not depend on the dream's internal markers.
    """

    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self._publish = publish

    def log(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type not in DREAM_ACTIVITY_TYPES:
            return
        at_ms = int(time.time() * 1000)
        for item in map_dream_event(event_type, data or {}, at_ms):
            self._publish({"kind": "activity", "item": item})
