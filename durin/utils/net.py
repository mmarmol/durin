"""Small networking helpers."""

from __future__ import annotations

import socket


def port_is_available(host: str, port: int) -> bool:
    """True if ``host:port`` can be bound for a new server.

    Mirrors what uvicorn/asyncio actually do (``SO_REUSEADDR`` + ``bind`` +
    ``listen``), so a socket merely in ``TIME_WAIT`` from a just-restarted server
    is reported available (uvicorn would reuse it) while a genuinely active
    listener — e.g. another durin instance on the same port — is reported taken.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            probe.listen(1)
            return True
        except OSError:
            return False
