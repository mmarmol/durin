"""`memory.dream.parse_failure` carries enough to tell why an answer failed."""
from durin.memory.llm_invoke import emit_parse_failure


def _capture(monkeypatch) -> list:
    import durin.agent.tools._telemetry as tel
    events: list = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    return events


def test_the_event_carries_both_ends_the_length_and_the_reasons(monkeypatch):
    """A head alone cannot tell an answer cut at the output limit (its tail
    stops mid-value) from a malformed one; the length, the tail, the finish
    reason and the parser's error can."""
    events = _capture(monkeypatch)
    raw = '{"actions": [' + "x" * 1000 + '"old": "# Guía'
    emit_parse_failure("curation", raw=raw, finish_reason="length",
                       error="Expecting ',' delimiter: line 1 column 1030")
    [(name, data)] = events
    assert name == "memory.dream.parse_failure"
    assert data["stage"] == "curation"
    assert data["raw_len"] == len(raw)
    assert data["raw_head"] == raw[:200]
    assert data["raw_tail"] == raw[-200:]
    assert data["finish_reason"] == "length"
    assert data["error"] == "Expecting ',' delimiter: line 1 column 1030"


def test_reasons_the_caller_does_not_have_are_left_out(monkeypatch):
    events = _capture(monkeypatch)
    emit_parse_failure("extract", source="person:ana", raw="sorry")
    [(_, data)] = events
    assert data["raw_len"] == 5 and data["raw_tail"] == "sorry"
    assert "finish_reason" not in data and "error" not in data


def test_a_long_parser_error_is_bounded(monkeypatch):
    events = _capture(monkeypatch)
    emit_parse_failure("absorb_judge", raw="x", error="e" * 1000)
    [(_, data)] = events
    assert len(data["error"]) == 200
