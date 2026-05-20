"""Meta-test: every event emitted in the source tree appears in the
``durin.telemetry.schema.EVENTS`` catalog, and vice versa (no orphans
in either direction).

This is the audit follow-up P3.5 enforcement layer. The schema module
itself is purely declarative (TypedDict hints have no runtime effect);
this test is what guarantees the catalog stays in sync with reality.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from durin.telemetry.schema import EVENTS

_DURIN_ROOT = Path(__file__).resolve().parent.parent.parent / "durin"

# A literal first-arg string to a telemetry-emitting helper. Matches
# the patterns we use across the codebase:
#
#   - ``.log("...")`` — direct ``TelemetryLogger.log``.
#   - ``._emit("...")`` / ``._emit_mode_telemetry("...")`` — instance
#     method wrappers on `_FsTool`, command builtins, plan_mode tool, etc.
#   - ``emit_tool_event("...")`` — free-function wrapper in
#     ``durin/agent/tools/_telemetry.py``, used by tools that don't
#     subclass _FsTool (web_search, web_fetch, todo_write, list_dir).
#
# Captures the event_type string (first quoted arg) for catalog comparison.
_LOG_CALL_RE = re.compile(
    r"""(?:\.log|_emit\w*|emit_tool_event)\(\s*["']([a-z][a-z0-9_.]*)["']"""
)

# Sites we know are NOT telemetry-event emits (e.g. structlog-style helpers,
# log_rate_limit which calls .log internally with a baked event_type). Match
# is on the captured event_type string.
_NON_EVENT_LOG_STRINGS: set[str] = set()


def _walk_python_sources() -> list[Path]:
    return [
        p for p in _DURIN_ROOT.rglob("*.py")
        # Skip the schema file itself — its catalog mentions every event
        # name as a dict key, which would create false positives.
        if "schema.py" not in p.name
    ]


def _emitted_events() -> set[str]:
    """Scan every ``.log("event.type", ...)`` call site and return the
    set of distinct event_type strings found."""
    found: set[str] = set()
    for source_file in _walk_python_sources():
        try:
            text = source_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _LOG_CALL_RE.finditer(text):
            event_type = match.group(1)
            if event_type in _NON_EVENT_LOG_STRINGS:
                continue
            # Telemetry events follow ``namespace.action`` naming. A bare
            # word with no dot is likely a different kind of log call
            # (loguru, structlog) — skip to keep false positives down.
            if "." not in event_type:
                continue
            found.add(event_type)
    return found


# Helper convenience for use in convenience tests.
def _registered_events() -> set[str]:
    return set(EVENTS.keys())


def test_every_emitted_event_is_in_the_catalog():
    """Walk the source tree, find every ``.log("namespace.action", ...)``
    call, and confirm the catalog has an entry. Fails with a clear list
    of missing entries — the diagnostic is the test failure itself."""
    emitted = _emitted_events()
    catalogued = _registered_events()
    missing = emitted - catalogued
    assert not missing, (
        f"Telemetry events emitted in source but missing from "
        f"durin/telemetry/schema.py::EVENTS:\n  - " + "\n  - ".join(sorted(missing))
        + "\nAdd a TypedDict + catalog entry for each."
    )


def test_no_orphan_catalog_entries():
    """Every event registered in the catalog must have at least one
    emit site in the source. Catches dead entries left over after a
    feature was reverted."""
    emitted = _emitted_events()
    catalogued = _registered_events()
    orphan = catalogued - emitted
    assert not orphan, (
        f"Telemetry events registered in catalog but never emitted in "
        f"source:\n  - " + "\n  - ".join(sorted(orphan))
        + "\nRemove the catalog entry or wire up the emit site."
    )


def test_catalog_keys_are_lowercase_dot_namespaced():
    """Naming convention check — keeps the JSONL dashboard-friendly."""
    for event_type in EVENTS:
        assert "." in event_type, f"event {event_type!r} missing namespace separator"
        assert event_type == event_type.lower(), f"event {event_type!r} not lowercase"
        assert not event_type.startswith("."), f"event {event_type!r} starts with separator"
        assert not event_type.endswith("."), f"event {event_type!r} ends with separator"


def test_catalog_values_are_typeddicts():
    """The catalog value must be a class (the TypedDict). Tightens the
    contract: a typo like ``EVENTS["foo.bar"] = None`` would silently
    pass without this."""
    import typing
    for event_type, td in EVENTS.items():
        assert isinstance(td, type), (
            f"event {event_type!r}: catalog value must be a TypedDict class, "
            f"got {type(td).__name__}"
        )
        # TypedDict subclasses don't pass isinstance(td, typing.TypedDict) —
        # TypedDict itself isn't usable that way — but they expose
        # ``__total__`` and ``__annotations__``.
        assert hasattr(td, "__annotations__"), (
            f"event {event_type!r}: catalog value {td.__name__} has no "
            f"__annotations__ — is it a TypedDict?"
        )
