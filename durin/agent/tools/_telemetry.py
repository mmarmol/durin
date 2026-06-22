"""Shared helper for emitting structured telemetry from tool code.

Tool subclasses of ``_FsTool`` (filesystem.py) inherit an ``_emit``
method; tools that DON'T extend ``_FsTool`` (the web / todo / list_dir
families) duplicated the boilerplate. This module collapses that into
one free function so every tool has the same shape:

    from durin.agent.tools._telemetry import emit_tool_event
    emit_tool_event("tool.web_search", {"provider": "brave", ...})

Failures are silently swallowed — telemetry must NEVER break a tool
call. A missing session logger (``current_telemetry()`` returning None
outside a bound task context) is treated the same as the bound logger
raising mid-write: the event is dropped, the tool proceeds.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from durin.telemetry.logger import current_telemetry

# Privacy bound for free-text fields. The full query is
# never persisted to telemetry — only the first N chars, enough to
# debug + bucket without exposing user content. Applied to any field
# named ``query`` / ``text`` / ``snippet`` / ``content``.
_MAX_FREETEXT_CHARS = 200

_TRUNCATED_FIELDS = frozenset(
    {"query", "text", "snippet", "content", "needle"}
)


def emit_tool_event(event_type: str, data: dict[str, Any]) -> None:
    """Emit one structured telemetry event from a tool. Best-effort.

    The event type follows the ``tool.<verb>`` convention documented
    in ``durin/telemetry/schema.py`` — register a TypedDict there for
    new events so the meta-test in ``tests/telemetry/`` keeps the
    catalog in sync with the emit sites.

    Privacy: free-text fields (``query`` / ``text`` / ``snippet`` /
    ``content`` / ``needle``) are truncated to 200 characters before
    persistence. The truncation is non-destructive — it does NOT mutate
    the caller's dict.

    The bound :class:`durin.telemetry.logger.TelemetryLogger` carries
    the active ``session_key`` and per-turn ``iteration``; both are
    auto-injected into the payload when the caller hasn't already
    populated them. Dashboards joining `memory.recall` to other events
    on `(session_key, iteration)` now have data to join on.
    """
    logger_obj = current_telemetry()
    if logger_obj is None:
        return
    safe_data = _truncate_freetext(data)
    # Auto-inject identity fields if absent. Caller-supplied values always
    # win so subagents / replay tools can stamp a different identity when
    # they need to. `getattr` defaults keep ad-hoc test loggers working —
    # they just won't carry the identity.
    if "session_key" not in safe_data:
        sk = getattr(logger_obj, "session_key", None)
        if sk:
            safe_data["session_key"] = sk
    if "iteration" not in safe_data:
        it = getattr(logger_obj, "iteration", None)
        if it is not None:
            safe_data["iteration"] = it
    with suppress(Exception):
        logger_obj.log(event_type, safe_data)


def _truncate_freetext(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *data* with free-text fields trimmed.

    Only top-level keys are inspected — nested dicts pass through
    untouched. This is intentional: nested structure usually carries
    structured metadata (counts, identifiers) that doesn't need
    trimming, and recursively traversing would surprise callers that
    pass typed dataclasses.
    """
    if not isinstance(data, dict):
        return data  # type: ignore[unreachable]
    out: dict[str, Any] = {}
    for key, value in data.items():
        if (
            key in _TRUNCATED_FIELDS
            and isinstance(value, str)
            and len(value) > _MAX_FREETEXT_CHARS
        ):
            out[key] = value[:_MAX_FREETEXT_CHARS] + "…"
        else:
            out[key] = value
    return out
