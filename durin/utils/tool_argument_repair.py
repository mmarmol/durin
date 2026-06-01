"""Tool-call argument repair (OpenClaw-inspired Tier 2 B1).

Durin already calls ``json_repair.loads`` on tool-call argument strings,
which fixes common JSON sins (trailing commas, unquoted keys, single
quotes). But two failure modes pass through ``json_repair`` unchanged:

1. **HTML-entity-encoded JSON**: some models emit
   ``{&quot;foo&quot;:&quot;bar&quot;}`` (most often when the model was
   trained on web-scraped tool-call examples and over-escaped). The
   parser sees ``&quot;`` as data, not a quote, and fails.

2. **Leading / trailing garbage**: models sometimes wrap the JSON in
   commentary — ``Here's the args: {"x":1}.`` — or trail it with a
   stray ``.`` or ``,``. ``json_repair`` can be confused by these.

This module is a thin pre-processor that runs BEFORE ``json_repair.loads``
and returns the repaired string plus a list of repair-token strings for
telemetry. Bounded by a 64 KB buffer (anything larger is suspicious —
no real tool-call argument is that long; either the provider already
truncated or the model emitted a hallucinated payload).

Mirrors OpenClaw's ``attempt.tool-call-argument-repair.ts`` constants
where possible so behaviour is comparable across the two stacks.
"""

from __future__ import annotations

import html
import re

# Mirrors OpenClaw run/attempt.tool-call-argument-repair.ts
MAX_REPAIR_BUFFER_CHARS = 64_000
MAX_LEADING_GARBAGE_CHARS = 96
MAX_TRAILING_GARBAGE_CHARS = 3

# Allowlist for what can appear before the JSON. A model commentary like
# "Here is the JSON: " matches; arbitrary code or quoted strings do not
# (the `"` is intentionally excluded — if there's a quote BEFORE the JSON,
# the string itself is malformed, not just prefixed with commentary).
_ALLOWED_LEADING_RE = re.compile(r"^[a-z0-9\s'`.:/_\\-]+$", re.IGNORECASE)
_ALLOWED_TRAILING_RE = re.compile(r"^[^\s{}\[\]\":,\\]{1,3}$")

# HTML entity heuristic: don't run unescape unless we see at least one
# obvious entity — html.unescape is permissive and would happily mutate
# strings that legitimately contain ``&``. The set covers the entities a
# model is most likely to emit when over-escaping JSON.
_HTML_ENTITY_MARKERS = ("&quot;", "&amp;", "&#x22;", "&#34;", "&apos;", "&lt;", "&gt;")


def repair_tool_call_arguments(raw: str) -> tuple[str, list[str]]:
    """Pre-process a tool-call argument string before ``json_repair.loads``.

    Returns ``(cleaned_string, repairs_applied)``. ``repairs_applied`` is
    a list of short tokens identifying which repairs ran (for telemetry
    & test assertions). The original string is returned unchanged when
    no repair was needed or when the input exceeded the buffer cap.

    Repairs are independent and order-stable: HTML unescape runs first
    (it can introduce ``{``/``}`` that the trim pass then anchors on),
    then leading-garbage strip, then trailing-garbage strip.
    """
    if not isinstance(raw, str) or not raw:
        return raw, []
    if len(raw) > MAX_REPAIR_BUFFER_CHARS:
        return raw, []

    out = raw
    repairs: list[str] = []

    # 1. HTML entity decoding.
    if any(marker in out for marker in _HTML_ENTITY_MARKERS):
        decoded = html.unescape(out)
        if decoded != out:
            out = decoded
            repairs.append("html_unescape")

    # 2. Leading garbage strip. Find the first JSON-opening character.
    first_brace = _earliest_json_open(out)
    if first_brace > 0:
        prefix = out[:first_brace]
        if (
            len(prefix) <= MAX_LEADING_GARBAGE_CHARS
            and _ALLOWED_LEADING_RE.match(prefix)
        ):
            out = out[first_brace:]
            repairs.append("strip_leading")

    # 3. Trailing garbage strip. Find the last JSON-closing character.
    last_close = _latest_json_close(out)
    if 0 <= last_close < len(out) - 1:
        suffix = out[last_close + 1:]
        if (
            len(suffix) <= MAX_TRAILING_GARBAGE_CHARS
            and _ALLOWED_TRAILING_RE.match(suffix)
        ):
            out = out[:last_close + 1]
            repairs.append("strip_trailing")

    return out, repairs


def parse_tool_call_arguments(raw: str | dict) -> dict:
    """Top-level helper used by every provider that decodes tool-call
    arguments. Applies :func:`repair_tool_call_arguments`, then
    ``json_repair.loads``, and emits a ``tool_call.argument_repair``
    telemetry event when any repair fired.

    Returns the parsed dict, or ``{}`` if parsing failed entirely. A
    non-dict result (e.g. ``json_repair`` returned a list because the
    model wrapped the args in a list) is also coerced to ``{}`` —
    callers always want a dict for ``ToolCallRequest.arguments``.
    """
    import json_repair  # local import keeps test isolation simple

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}

    cleaned, repairs = repair_tool_call_arguments(raw)
    try:
        parsed = json_repair.loads(cleaned)
    except Exception:
        parsed = {}

    if repairs:
        # Telemetry is best-effort: a missing logger or a bad event must
        # never break tool-call parsing.
        try:
            from contextlib import suppress

            from durin.telemetry.logger import current_telemetry
            logger_obj = current_telemetry()
            if logger_obj is not None:
                with suppress(Exception):
                    logger_obj.log("tool_call.argument_repair", {
                        "repairs": repairs,
                        "original_len": len(raw),
                        "cleaned_len": len(cleaned),
                        "parsed_ok": isinstance(parsed, dict),
                    })
        except Exception:
            pass

    return parsed if isinstance(parsed, dict) else {}


def _earliest_json_open(s: str) -> int:
    """Index of the first ``{`` or ``[``, or -1 if neither is present."""
    brace = s.find("{")
    bracket = s.find("[")
    if brace == -1:
        return bracket
    if bracket == -1:
        return brace
    return min(brace, bracket)


def _latest_json_close(s: str) -> int:
    """Index of the last ``}`` or ``]``, or -1 if neither is present."""
    return max(s.rfind("}"), s.rfind("]"))
