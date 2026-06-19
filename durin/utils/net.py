"""Small networking helpers."""

from __future__ import annotations

import socket

from loguru import logger


def pick_free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if it can be bound on ``host``; else an OS-picked free port.

    Lets a second durin instance start without hand-editing ports: the configured
    port stays the preference, and a free fallback is chosen and logged when the
    preferred one is already taken (e.g. by the daily daemon).
    """
    # Probe with bind + listen and NO SO_REUSEADDR so we mirror what uvicorn
    # actually does: on macOS a bind-only probe with SO_REUSEADDR succeeds even
    # against an actively-listening socket, but the subsequent listen() fails —
    # which would let us return a taken port and crash the server.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, preferred))
            probe.listen(1)
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as alloc:
        alloc.bind((host, 0))
        chosen = alloc.getsockname()[1]
    logger.warning(
        "Port {} on {} is taken; using free port {} instead.", preferred, host, chosen
    )
    return chosen
