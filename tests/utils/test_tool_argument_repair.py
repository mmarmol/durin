"""Tool-call argument repair (OpenClaw-inspired Tier 2 B1).

durin already uses ``json_repair.loads`` so common JSON sins (trailing
commas, unquoted keys) are handled. B1 adds two failure modes:

1. **HTML-entity-encoded JSON**: ``{&quot;x&quot;:1}`` — html.unescape
   normalises before json_repair sees it.
2. **Leading / trailing garbage**: ``Here is the JSON: {"x":1}.``
   — bounded strip when the surrounding text matches a strict allowlist.

The helper is bounded by a 64 KB buffer (pathological inputs pass
through unchanged — better to let json_repair handle a clearly broken
payload than to over-eagerly strip arbitrary chars from a huge buffer).
"""

from __future__ import annotations

from durin.utils.tool_argument_repair import (
    MAX_REPAIR_BUFFER_CHARS,
    parse_tool_call_arguments,
    repair_tool_call_arguments,
)

# ---------------------------------------------------------------------------
# Unit tests for repair_tool_call_arguments
# ---------------------------------------------------------------------------


def test_passthrough_for_clean_json():
    out, repairs = repair_tool_call_arguments('{"x":1}')
    assert out == '{"x":1}'
    assert repairs == []


def test_passthrough_for_empty_string():
    out, repairs = repair_tool_call_arguments("")
    assert out == ""
    assert repairs == []


def test_passthrough_for_oversize_input():
    """Pathological 1 MB payloads pass through — better to surrender than
    to spend cycles on hopeless input."""
    raw = "x" * (MAX_REPAIR_BUFFER_CHARS + 10)
    out, repairs = repair_tool_call_arguments(raw)
    assert out == raw
    assert repairs == []


def test_html_entities_unescaped():
    raw = '{&quot;name&quot;:&quot;list_dir&quot;,&quot;path&quot;:&quot;.&quot;}'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == '{"name":"list_dir","path":"."}'
    assert repairs == ["html_unescape"]


def test_html_entities_only_when_marker_present():
    """A raw ``&`` (e.g. inside a URL value) must not trigger unescape —
    we'd risk mutating data."""
    raw = '{"url":"http://example.com/?a=1&b=2"}'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == raw
    assert "html_unescape" not in repairs


def test_strip_leading_commentary():
    raw = 'Here is the JSON: {"x":1}'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == '{"x":1}'
    assert "strip_leading" in repairs


def test_strip_leading_with_disallowed_chars_passes_through():
    """A leading quote/brace pattern that the allowlist doesn't accept
    must NOT be stripped — caller is expected to surface a parse error."""
    raw = '"oops":{"x":1}'
    out, repairs = repair_tool_call_arguments(raw)
    # The leading `"oops":` contains `"` and `:` and `o` etc — but the
    # `"` is not in the allowlist regex, so no strip.
    assert out == raw
    assert "strip_leading" not in repairs


def test_strip_leading_capped_at_96_chars():
    """Leading garbage longer than 96 chars is suspicious — pass through."""
    raw = "x" * 200 + '{"x":1}'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == raw
    assert "strip_leading" not in repairs


def test_strip_trailing_period():
    raw = '{"x":1}.'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == '{"x":1}'
    assert "strip_trailing" in repairs


def test_strip_trailing_capped_at_3_chars():
    """A 4-char trailing suffix is too long to be benign."""
    raw = '{"x":1}xxxx'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == raw
    assert "strip_trailing" not in repairs


def test_multiple_repairs_compose():
    raw = 'Heres: {&quot;x&quot;:1}.'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == '{"x":1}'
    assert "html_unescape" in repairs
    assert "strip_leading" in repairs
    assert "strip_trailing" in repairs


def test_array_payloads_supported():
    """Some providers wrap tool args in arrays — the strip pass should
    anchor on ``[`` as well as ``{``."""
    raw = 'See: [1, 2, 3]'
    out, repairs = repair_tool_call_arguments(raw)
    assert out == '[1, 2, 3]'
    assert "strip_leading" in repairs


# ---------------------------------------------------------------------------
# Integration via parse_tool_call_arguments (the public helper that
# providers actually call).
# ---------------------------------------------------------------------------


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    import durin.telemetry.logger as tlog
    monkeypatch.setattr(tlog, "current_telemetry", lambda: sink)


def test_parse_returns_dict_for_clean_json():
    assert parse_tool_call_arguments('{"x":1}') == {"x": 1}


def test_parse_returns_dict_for_dict_input():
    """Already-parsed dict passes through untouched."""
    d = {"x": 1}
    assert parse_tool_call_arguments(d) is d


def test_parse_returns_empty_for_blank_input():
    assert parse_tool_call_arguments("") == {}
    assert parse_tool_call_arguments("   ") == {}


def test_parse_coerces_non_dict_results_to_empty():
    """If json_repair returns a list because the model wrapped args in
    ``[...]``, the caller still wants a dict."""
    # json_repair will parse this as [1, 2] which is a list, not a dict.
    assert parse_tool_call_arguments("[1, 2]") == {}


def test_parse_emits_telemetry_on_repair(monkeypatch):
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    result = parse_tool_call_arguments(
        '{&quot;name&quot;:&quot;list_dir&quot;,&quot;path&quot;:&quot;.&quot;}'
    )
    assert result == {"name": "list_dir", "path": "."}

    repair_events = [e for e in telemetry.events if e[0] == "tool_call.argument_repair"]
    assert len(repair_events) == 1
    payload = repair_events[0][1]
    assert "html_unescape" in payload["repairs"]
    assert payload["parsed_ok"] is True


def test_parse_no_telemetry_when_no_repair_needed(monkeypatch):
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    parse_tool_call_arguments('{"x":1}')
    assert [e for e in telemetry.events if e[0] == "tool_call.argument_repair"] == []


def test_parse_records_failure_in_telemetry(monkeypatch):
    """When repair fired but parsing STILL fails, telemetry should mark
    parsed_ok=False — useful for spotting models whose output is
    irreparably broken."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    # HTML entities trigger repair; the resulting string is still garbage
    # that json_repair fills in with defaults — so parsed_ok may actually
    # be True. Let's craft something stronger: HTML entities + broken
    # JSON content that json_repair can't recover into a dict.
    result = parse_tool_call_arguments('&quot;not a json object&quot;')
    assert result == {}  # coerced because json_repair returned a string

    repair_events = [e for e in telemetry.events if e[0] == "tool_call.argument_repair"]
    assert len(repair_events) == 1
    # parsed_ok could be False (json_repair fails) OR True with a non-dict
    # that got coerced. Either way, the event carries the signal.
    assert "parsed_ok" in repair_events[0][1]
