"""Wire optional telemetry sinks onto a session ``TelemetryLogger``.

Audit A8 (2026-05-28): when ``cfg.telemetry.push.enabled`` is true,
construct a :class:`durin.telemetry.push.PushSink` from the config +
the bearer token resolved through the secret store, and attach it to
the logger. The local JSONL persistence runs UNCHANGED — push is an
additive sink, never a replacement.

Privacy: the bearer token MUST live in the secret store (never in
``config.json`` plaintext). The config carries only the secret name;
this module looks the actual value up at wire-time. Missing secret →
push disabled with a warning (graceful degradation; local JSONL
keeps working).
"""

from __future__ import annotations

import logging
from typing import Any

from durin.telemetry.logger import TelemetryLogger
from durin.telemetry.push import PushSink

logger = logging.getLogger(__name__)

__all__ = ["wire_push_sink"]


def wire_push_sink(
    session_logger: TelemetryLogger,
    push_config: Any,
) -> PushSink | None:
    """Attach a :class:`PushSink` to *session_logger* when configured.

    Returns the attached sink (so callers that own the shutdown path
    can call ``flush()`` on it) or ``None`` when push is disabled,
    misconfigured, or the secret is missing.

    All failure modes leave the local JSONL logger fully functional —
    push is opt-in, never required.
    """
    if push_config is None:
        return None
    if not getattr(push_config, "enabled", False):
        return None

    url = getattr(push_config, "url", None) or ""
    secret_name = getattr(push_config, "token_secret_name", None) or ""
    if not url or not secret_name:
        logger.warning(
            "telemetry.push.enabled=true but url or token_secret_name "
            "is empty; push disabled. Set both or disable push."
        )
        return None

    # Lookup the bearer token in the secret store. We import inside the
    # function so test paths that never enable push don't pay for the
    # import.
    try:
        from durin.security.secrets import get_secret_store
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "telemetry.push: secret store import failed (%s); push disabled",
            exc,
        )
        return None

    try:
        entry = get_secret_store().get(secret_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "telemetry.push: secret lookup for %r failed (%s); push disabled",
            secret_name, exc,
        )
        return None

    if entry is None or not getattr(entry, "value", ""):
        logger.warning(
            "telemetry.push: secret %r not found in store; push disabled. "
            "Add it via `durin secrets set %s <token>`.",
            secret_name, secret_name,
        )
        return None

    batch_size = int(getattr(push_config, "batch_size", 10) or 10)
    sink = PushSink(url=url, token=entry.value, batch_size=batch_size)
    session_logger.add_sink(sink)
    logger.info(
        "telemetry.push: enabled — events fan out to %s "
        "(batch=%d)",
        url, batch_size,
    )
    return sink
