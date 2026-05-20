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


def emit_tool_event(event_type: str, data: dict[str, Any]) -> None:
    """Emit one structured telemetry event from a tool. Best-effort.

    The event type follows the ``tool.<verb>`` convention documented
    in ``durin/telemetry/schema.py`` — register a TypedDict there for
    new events so the meta-test in ``tests/telemetry/`` keeps the
    catalog in sync with the emit sites.
    """
    logger_obj = current_telemetry()
    if logger_obj is None:
        return
    with suppress(Exception):
        logger_obj.log(event_type, data)
